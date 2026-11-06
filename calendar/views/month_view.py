import datetime
from copy import deepcopy
from typing import Callable

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
from xapp.util import l10n

_ = l10n("clockenstein")

from clockenstein.formatting import format_time, ordered_weekday_names, start_of_week
from clockenstein.drawing import draw_centered_circle
from clockenstein.misc import get_event_last_day, is_event_all_day
from clockenstein import DEFAULT_COLOR
from views.colors import apply_tinted_event_color

EVENT_HEIGHT = 22
EVENT_GAP = 3
DAY_HEADER_HEIGHT = 23
OVERFLOW_HEIGHT = 16


class MonthView(Gtk.Box):
    def __init__(self, today: datetime.date, on_event: Callable, on_day: Callable,
                 on_scroll=None, on_select=None, first_weekday=0, time_format="locale",
                 on_overflow=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.today    = today
        self.on_event = on_event
        self.on_day   = on_day
        self.on_scroll = on_scroll
        self.on_select = on_select
        self.on_overflow = on_overflow
        self.selected_date = today
        self.first_weekday = first_weekday
        self.time_format = time_format
        self.max_lanes = 1
        self.event_height = EVENT_HEIGHT
        self._last_update = None
        self._lane_reflow_source = None
        self._build()
        self.connect("size-allocate", self._on_size_allocate)

    def _on_scroll(self, _widget, event):
        if not self.on_scroll:
            return False
        if event.direction == Gdk.ScrollDirection.UP:
            self.on_scroll(-1)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.on_scroll(1)
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            self.on_scroll(event.delta_y)
        else:
            return False
        return True

    def do_get_preferred_height(self):
        _minimum, natural = Gtk.Box.do_get_preferred_height(self)
        return 390, max(390, natural)

    def do_get_preferred_height_for_width(self, width):
        _minimum, natural = Gtk.Box.do_get_preferred_height_for_width(self, width)
        return 390, max(390, natural)

    def _build(self):
        scroll_area = Gtk.EventBox()
        scroll_area.add_events(Gdk.EventMask.SCROLL_MASK)
        scroll_area.connect("scroll-event", self._on_scroll)
        self.pack_start(scroll_area, True, True, 0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll_area.add(content)

        dow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        dow.get_style_context().add_class("clockenstein-dow-header")
        self.weekday_labels = []
        for name in ordered_weekday_names(self.first_weekday):
            lbl = Gtk.Label(label=name)
            lbl.set_hexpand(True)
            lbl.set_xalign(1)
            lbl.set_margin_end(7)
            lbl.get_style_context().add_class("clockenstein-dow-label")
            dow.pack_start(lbl, True, True, 0)
            self.weekday_labels.append(lbl)
        content.pack_start(dow, False, False, 0)

        self.grid = Gtk.Grid()
        self.grid.set_row_homogeneous(True)
        self.grid.set_column_homogeneous(True)
        self.grid.set_hexpand(True)
        self.grid.set_vexpand(True)
        content.pack_start(self.grid, True, True, 0)
        self.event_widgets = []
        self._event_render_state = None

        self.cells: list[_DayCell] = []
        for row in range(6):
            for col in range(7):
                cell = _DayCell(self.today, self.on_event, self.on_day,
                                self.on_select, self.on_overflow)
                self.grid.attach(cell, col, row, 1, 1)
                self.cells.append(cell)

    def update(self, current_date: datetime.date, events: list[dict], week_offset=0,
               selected_date=None):
        if selected_date is not None:
            self.selected_date = selected_date
        self._last_update = (current_date, events, week_offset, self.selected_date)
        first = current_date.replace(day=1)
        grid_start = (start_of_week(first, self.first_weekday) +
                      datetime.timedelta(weeks=week_offset))

        for index, cell in enumerate(self.cells):
            day = grid_start + datetime.timedelta(days=index)
            cell.set_day(day, day.month == current_date.month)
            cell.set_selected(day == self.selected_date)

        self._render_events(grid_start, events)
        self.show_all()

    def set_first_weekday(self, first_weekday):
        self.first_weekday = first_weekday
        for label, name in zip(
                self.weekday_labels, ordered_weekday_names(first_weekday)):
            label.set_text(name)

    def set_today(self, today: datetime.date):
        self.today = today
        for cell in self.cells:
            cell.today = today

    def set_time_format(self, time_format):
        self.time_format = time_format
        if self._last_update:
            self.update(*self._last_update)

    def _render_events(self, grid_start, events):
        render_state = (grid_start, self.max_lanes, self.event_height,
                        self.time_format, events,
                        [_event_has_ended(event) for event in events])
        if render_state == self._event_render_state:
            return
        for widget in self.event_widgets:
            self.grid.remove(widget)
        self.event_widgets = []

        grid_end = grid_start + datetime.timedelta(days=41)
        occupied = [[set() for _lane in range(self.max_lanes)] for _row in range(6)]
        used_lanes = [0] * 6
        hidden_by_date = {}
        def get_sort_key(event):
            duration_days = (get_event_last_day(event) - event["date_start"]).days
            # Place longer events first when they start on the same day.
            return event["date_start"], -duration_days

        for event in sorted(events, key=get_sort_key):
            segment_start = max(event["date_start"], grid_start)
            visible_end = min(get_event_last_day(event), grid_end)
            while segment_start <= visible_end:
                offset = (segment_start - grid_start).days
                row, col = divmod(offset, 7)
                segment_end = min(visible_end, grid_start + datetime.timedelta(days=row * 7 + 6))
                end_col = (segment_end - (grid_start + datetime.timedelta(days=row * 7))).days
                columns = set(range(col, end_col + 1))
                lane = None
                for index, taken in enumerate(occupied[row]):
                    if not taken.intersection(columns):
                        lane = index
                        break
                if lane is not None:
                    occupied[row][lane].update(columns)
                    used_lanes[row] = max(used_lanes[row], lane + 1)
                    pill = _SpanPill(event, self.on_event, segment_start == event["date_start"],
                                     self.time_format)
                    pill.set_size_request(-1, self.event_height)
                    pill.set_valign(Gtk.Align.START)
                    pill.set_margin_top(DAY_HEADER_HEIGHT + lane * (self.event_height + EVENT_GAP))
                    pill.set_margin_start(3)
                    pill.set_margin_end(3)
                    self.grid.attach(pill, col, row, end_col - col + 1, 1)
                    self.event_widgets.append(pill)
                else:
                    day = segment_start
                    while day <= segment_end:
                        hidden_by_date.setdefault(day, []).append(
                            str(event.get("summary") or _("Untitled"))
                        )
                        day += datetime.timedelta(days=1)
                segment_start = segment_end + datetime.timedelta(days=1)

        for row in range(6):
            reserved = used_lanes[row] * (self.event_height + EVENT_GAP)
            for col in range(7):
                index = row * 7 + col
                day = grid_start + datetime.timedelta(days=index)
                self.cells[index].set_event_space(reserved, hidden_by_date.get(day, []))
        self._event_render_state = deepcopy(render_state)

    def _on_size_allocate(self, _widget, _allocation):
        if self._lane_reflow_source is None:
            self._lane_reflow_source = GLib.idle_add(self._reflow_for_allocation)

    def _reflow_for_allocation(self):
        self._lane_reflow_source = None
        row_height = self.grid.get_allocated_height() // 6
        lane_height = self.event_height + EVENT_GAP
        lanes = max(1, (row_height - DAY_HEADER_HEIGHT - OVERFLOW_HEIGHT) // lane_height)
        if lanes == self.max_lanes:
            return False
        self.max_lanes = lanes
        if self._last_update:
            self.update(*self._last_update)
        return False


class _DayCell(Gtk.EventBox):
    def __init__(self, today, on_event, on_day, on_select=None, on_overflow=None):
        super().__init__()
        self.today    = today
        self.on_event = on_event
        self.on_day   = on_day
        self.on_select = on_select
        self.on_overflow = on_overflow
        self._date    = None
        self._hidden  = []
        self.get_style_context().add_class("clockenstein-day-cell")
        self.connect("button-press-event", self._on_click)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        outer.set_margin_top(2)
        outer.set_margin_bottom(2)
        outer.set_margin_start(3)
        outer.set_margin_end(3)
        self.add(outer)

        header = Gtk.Overlay()
        header.set_hexpand(True)
        outer.pack_start(header, False, False, 0)

        self.month_lbl = Gtk.Label()
        self.month_lbl.set_xalign(0)
        self.month_lbl.set_hexpand(True)
        self.month_lbl.set_opacity(0)
        self.month_lbl.get_style_context().add_class("clockenstein-month-label")
        header.add(self.month_lbl)

        self.day_lbl = Gtk.Label()
        self.day_lbl.set_xalign(1)
        self.day_lbl.set_halign(Gtk.Align.END)
        self.day_lbl.get_style_context().add_class("clockenstein-day-number")
        header.add_overlay(self.day_lbl)

        # Multi-day bars are drawn across the grid by MonthView.  Reserve their
        # rows with an actual widget so GTK always lays the per-day events below
        # them (a margin is not reliable when a cell is vertically constrained).
        self.span_space = Gtk.Box()
        self.span_space.set_size_request(-1, 0)
        outer.pack_start(self.span_space, False, False, 0)

        self.ev_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        outer.pack_start(self.ev_box, True, True, 0)

    def set_event_space(self, pixels, hidden_events):
        self._hidden = list(hidden_events)
        self.span_space.set_size_request(-1, pixels)
        for child in self.ev_box.get_children():
            self.ev_box.remove(child)
        if hidden_events:
            more = Gtk.Label(label=_("+%d more") % len(hidden_events))
            more.set_xalign(0)
            more.set_tooltip_text("\n".join(hidden_events))
            more.get_style_context().add_class("clockenstein-more-label")
            self.ev_box.pack_start(more, False, False, 0)

    def set_day(self, date, in_month):
        self._date = date
        style_context = self.get_style_context()
        for style_class in ("clockenstein-today", "clockenstein-other-month", "clockenstein-month-start"):
            style_context.remove_class(style_class)
        if date == self.today:
            style_context.add_class("clockenstein-today")
        if not in_month:
            style_context.add_class("clockenstein-other-month")
        if date.day == 1:
            style_context.add_class("clockenstein-month-start")

        show_month = date.day == 1
        self.month_lbl.set_opacity(1 if show_month else 0)
        self.month_lbl.set_text(date.strftime("%B").upper() if show_month else "")
        self.day_lbl.set_text(str(date.day))

    def set_selected(self, selected):
        context = self.get_style_context()
        if selected:
            context.add_class("clockenstein-selected-day")
        else:
            context.remove_class("clockenstein-selected-day")

    def _on_click(self, _w, event):
        if event.button == 1 and self._date and self.on_select:
            self.on_select(self._date)
        if event.button == 1 and self._date and self._hidden and self.on_overflow:
            # Click on the cell, not a pill, and the day hides events.
            self.on_overflow(self._date)
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and self._date:
            self.on_day(self._date)

class _SpanPill(Gtk.EventBox):
    def __init__(self, event, on_event, show_accent, time_format):
        super().__init__()
        single_timed = (not is_event_all_day(event) and
                        get_event_last_day(event) == event["date_start"])
        self.get_style_context().add_class("clockenstein-event-pill")
        self.get_style_context().add_class("clockenstein-event-span")
        if single_timed:
            self.get_style_context().add_class("clockenstein-event-dot-row")
        else:
            apply_tinted_event_color(self, event, show_accent)
        if _event_has_ended(event):
            self.set_opacity(0.5)
        self.connect("button-press-event", _activate_event, on_event, event)
        self.set_tooltip_markup(_get_event_tooltip(event, time_format))

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        if single_timed:
            content.set_margin_start(4)
            dot = Gtk.DrawingArea()
            dot.set_size_request(12, 12)
            dot.set_valign(Gtk.Align.CENTER)
            rgba = Gdk.RGBA()
            rgba.parse(event.get("calendar_color", DEFAULT_COLOR))
            dot.connect("draw", draw_centered_circle, rgba)
            content.pack_start(dot, False, False, 0)

        label = Gtk.Label()
        label.set_xalign(0)
        label.set_ellipsize(3)
        summary = event.get("summary") or _("Untitled")
        escaped_summary = GLib.markup_escape_text(summary)
        label.set_markup(f"<b>{escaped_summary}</b>")
        if single_timed:
            label.set_margin_start(0)
        elif show_accent:
            label.set_margin_start(9)
        else:
            label.set_margin_start(4)
        label.set_margin_end(4)
        content.pack_start(label, True, True, 0)
        self.add(content)


def _get_event_tooltip(event, time_format="locale"):
    title = GLib.markup_escape_text(event.get("summary") or _("Untitled"))
    properties = []
    if not is_event_all_day(event):
        properties.append(format_time(event["time_start"], time_format))
    if event.get("location"):
        properties.append(str(event["location"]))
    if not properties:
        return f"<b>{title}</b>"
    details = GLib.markup_escape_text("\n".join(properties))
    return f"<b>{title}</b>\n{details}"


def _activate_event(_widget, click, on_event, event):
    if click.button != 1:
        return False
    if click.type == Gdk.EventType.BUTTON_PRESS:
        on_event(event)
    return True


def _event_has_ended(event):
    now = datetime.datetime.now()
    end_date = event.get("date_end", event["date_start"])
    if event.get("all_day"):
        return end_date < now.date()
    end_time = event.get("time_end") or datetime.time.max
    return datetime.datetime.combine(end_date, end_time) < now
