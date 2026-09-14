import datetime
from typing import Callable

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango
from xapp.util import l10n

_ = l10n("clockenstein")

from formatting import capitalize_first, format_time, on_clock_format_changed
from views.colors import apply_tinted_event_color
from views.month_view import _event_has_ended

HOUR_HEIGHT = 48
DAY_START_MINUTE = 0
DAY_END_MINUTE = 24 * 60
DAY_HEIGHT = 24 * HOUR_HEIGHT
ALL_DAY_HEIGHT = 2 * HOUR_HEIGHT
ALL_DAY_EVENT_MARGIN = 4
TIMELINE_HEIGHT = ALL_DAY_HEIGHT + DAY_HEIGHT
EVENT_WIDTH = 260
EVENT_COLUMN_GAP = 4


class DayView(Gtk.Box):
    def __init__(self, today: datetime.date, on_event: Callable, on_new_event: Callable):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.today    = today
        self.current_date = today
        self.on_event = on_event
        self.on_new_event = on_new_event
        self._positioned_events = []
        self._build()

    def _build(self):
        self.date_label = Gtk.Label()
        self.date_label.set_xalign(0)
        self.date_label.set_margin_start(60)
        self.date_label.set_margin_end(12)
        self.date_label.set_margin_top(8)
        self.date_label.set_margin_bottom(8)
        self.date_label.get_style_context().add_class("clockenstein-day-date")
        self.pack_start(self.date_label, False, False, 0)

        all_day_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.pack_start(all_day_row, False, False, 0)
        all_day_label = Gtk.Label(label=_("All day"))
        all_day_label.set_size_request(52, ALL_DAY_HEIGHT)
        all_day_label.set_xalign(1)
        all_day_label.get_style_context().add_class("clockenstein-time-label")
        all_day_row.pack_start(all_day_label, False, False, 0)
        self.all_day_overlay = Gtk.Overlay()
        self.all_day_overlay.set_hexpand(True)
        self.all_day_overlay.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.all_day_overlay.connect("button-press-event", self._on_background_click)
        all_day_row.pack_start(self.all_day_overlay, True, True, 0)
        all_day_background = Gtk.DrawingArea()
        all_day_background.set_size_request(-1, ALL_DAY_HEIGHT)
        all_day_background.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        all_day_background.connect("draw", self._draw_all_day_background)
        all_day_background.connect("button-press-event", self._on_background_click)
        self.all_day_overlay.add(all_day_background)
        self.all_day_event_layer = Gtk.Fixed()
        self.all_day_event_layer.set_hexpand(True)
        self.all_day_event_layer.set_size_request(-1, ALL_DAY_HEIGHT)
        self.all_day_event_layer.connect("size-allocate", self._position_event_widgets)
        self.all_day_overlay.add_overlay(self.all_day_event_layer)

        scroll = Gtk.ScrolledWindow()
        self.timeline_scroll = scroll
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.pack_start(scroll, True, True, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        scroll.add(body)

        self.gutter = Gtk.Fixed()
        self.gutter.set_size_request(52, DAY_HEIGHT)
        body.pack_start(self.gutter, False, False, 0)
        self._hour_labels = []
        for h in range(24):
            lbl = Gtk.Label(label=format_time(datetime.time(h, 0)))
            lbl.set_size_request(52, 20)
            lbl.set_xalign(1)
            lbl.set_yalign(0.5)
            lbl.get_style_context().add_class("clockenstein-time-label")
            # Centre the label on the same coordinate used by the grid line and
            # by events starting exactly on the hour.
            self.gutter.put(lbl, 0, max(0, _minute_to_y(h * 60) - 10))
            self._hour_labels.append((lbl, h))
        on_clock_format_changed(self._refresh_time_format)
        self.now_label = Gtk.Label()
        self.now_label.set_size_request(52, 20)
        self.now_label.set_xalign(1)
        self.now_label.set_no_show_all(True)
        self.now_label.get_style_context().add_class("clockenstein-now-label")
        self.gutter.put(self.now_label, 0, 0)

        self.overlay = Gtk.Overlay()
        self.overlay.set_hexpand(True)
        self.overlay.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.overlay.connect("button-press-event", self._on_background_click)
        body.pack_start(self.overlay, True, True, 0)

        self.background = Gtk.DrawingArea()
        self.background.set_size_request(-1, DAY_HEIGHT)
        self.background.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.background.connect("draw", self._draw_background)
        self.background.connect("button-press-event", self._on_background_click)
        self.overlay.add(self.background)

        self.event_layer = Gtk.Fixed()
        self.event_layer.set_hexpand(True)
        self.event_layer.set_size_request(-1, DAY_HEIGHT)
        self.event_layer.connect("size-allocate", self._position_event_widgets)
        self.overlay.add_overlay(self.event_layer)

        GLib.timeout_add_seconds(30, self._update_now_line)

    def update(self, current_date: datetime.date, events: list[dict]):
        self.current_date = current_date
        self.date_label.set_text(capitalize_first(current_date.strftime("%A %-d %B %Y")))
        day_events = [e for e in events
                      if e["date_start"] <= current_date <= e.get("date_end", e["date_start"])]

        for layer in (self.all_day_event_layer, self.event_layer):
            for child in layer.get_children():
                layer.remove(child)
        self._positioned_events = []
        for ev in day_events:
            full_day_column = ev["all_day"] or ev["time_start"] is None
            if full_day_column:
                start_minutes = DAY_START_MINUTE
                end_minutes = DAY_END_MINUTE
            else:
                start_minutes, end_minutes = _timed_segment_minutes(ev, current_date)
                if end_minutes <= start_minutes:
                    continue

            btn = _DayEventButton()
            btn.get_style_context().add_class("clockenstein-week-event")
            btn.get_style_context().add_class("clockenstein-day-event")
            apply_tinted_event_color(btn, ev)
            if _event_has_ended(ev):
                btn.set_opacity(0.5)
            btn.connect("clicked", lambda _, e=ev: self.on_event(e))
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            content.set_valign(Gtk.Align.CENTER if full_day_column else Gtk.Align.START)
            title, when, location = _event_label_parts(
                ev, start_minutes, end_minutes, full_day_column
            )
            if full_day_column:
                when, location = "", ""
            for value, bold in ((title, True), (when, False), (location, False)):
                if not value:
                    continue
                text = Gtk.Label()
                text.set_xalign(0)
                text.set_line_wrap(True)
                text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                if bold:
                    text.set_markup(f"<b>{GLib.markup_escape_text(value)}</b>")
                else:
                    text.set_text(value)
                content.pack_start(text, False, False, 0)
            btn.add(content)
            top = ALL_DAY_EVENT_MARGIN if full_day_column else _minute_to_y(start_minutes)
            height = (ALL_DAY_HEIGHT - 2 * ALL_DAY_EVENT_MARGIN if full_day_column else
                      max(1, _minute_to_y(end_minutes) - _minute_to_y(start_minutes)))
            layer = self.all_day_event_layer if full_day_column else self.event_layer
            layer.put(btn, 0, top)
            self._positioned_events.append({
                "widget": btn, "start": start_minutes, "end": end_minutes,
                "top": top, "height": height, "all_day": full_day_column, "layer": layer,
            })

        _assign_event_columns([item for item in self._positioned_events if item["all_day"]])
        _assign_event_columns([item for item in self._positioned_events if not item["all_day"]])
        columns = max((item["columns"] for item in self._positioned_events), default=1)
        timeline_width = 8 + columns * (EVENT_WIDTH + EVENT_COLUMN_GAP)
        self.all_day_event_layer.set_size_request(timeline_width, ALL_DAY_HEIGHT)
        self.event_layer.set_size_request(timeline_width, DAY_HEIGHT)
        self.background.set_size_request(timeline_width, DAY_HEIGHT)

        self.show_all()
        scroll_minute = _initial_scroll_minute(
            day_events, (current_date,), current_date == datetime.date.today()
        )
        GLib.idle_add(self.timeline_scroll.get_vadjustment().set_value,
                      _minute_to_y(scroll_minute))
        self._position_event_widgets(self.event_layer, self.event_layer.get_allocation())
        self._update_now_line()

    def _on_background_click(self, _widget, event):
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            self.on_new_event(self.current_date)
            return True
        return False

    def _draw_background(self, widget, cr):
        cr.save()
        cr.translate(0, -ALL_DAY_HEIGHT)
        _draw_day_grid(widget, cr)
        if self.current_date == datetime.date.today():
            _draw_now_line(widget, cr)
        cr.restore()
        return False

    def _draw_all_day_background(self, widget, cr):
        cr.set_source_rgba(0.35, 0.35, 0.35, 0.75)
        cr.set_line_width(2)
        cr.move_to(0, ALL_DAY_HEIGHT - 1)
        cr.line_to(widget.get_allocated_width(), ALL_DAY_HEIGHT - 1)
        cr.stroke()
        return False

    def _update_now_line(self):
        visible = self.current_date == datetime.date.today()
        self.now_label.set_visible(visible)
        if visible:
            now = datetime.datetime.now()
            minutes = now.hour * 60 + now.minute
            self.now_label.set_text(format_time(now))
            self.gutter.move(self.now_label, 0, _minute_to_y(minutes) - 10)
        self.background.queue_draw()
        return True

    def _refresh_time_format(self):
        for lbl, h in self._hour_labels:
            lbl.set_text(format_time(datetime.time(h, 0)))
        self._update_now_line()

    def _position_event_widgets(self, _layer, allocation):
        for item in self._positioned_events:
            x = 4 + item["column"] * (EVENT_WIDTH + EVENT_COLUMN_GAP)
            item["widget"].set_timeline_height(item["height"])
            item["layer"].move(item["widget"], x, item["top"])


class _DayEventButton(Gtk.Button):
    def __init__(self):
        super().__init__()
        self._timeline_height = 1

    def set_timeline_height(self, height):
        height = max(1, height)
        if height != self._timeline_height:
            self._timeline_height = height
            self.queue_resize()

    def do_get_preferred_width(self):
        return EVENT_WIDTH, EVENT_WIDTH

    def do_get_preferred_height(self):
        return self._timeline_height, self._timeline_height

    def do_get_preferred_height_for_width(self, _width):
        return self._timeline_height, self._timeline_height

    def do_draw(self, cr):
        allocation = self.get_allocation()
        cr.save()
        cr.rectangle(0, 0, allocation.width, allocation.height)
        cr.clip()
        result = Gtk.Button.do_draw(self, cr)
        cr.restore()
        return result


def _time_minutes(value):
    return value.hour * 60 + value.minute


def _minute_to_y(minutes):
    return round(minutes / 60 * HOUR_HEIGHT)


def _timeline_y(minutes):
    return ALL_DAY_HEIGHT + _minute_to_y(minutes)


def _timed_segment_minutes(event, day):
    start = (_time_minutes(event["time_start"])
             if day == event["date_start"] else DAY_START_MINUTE)
    end = (_time_minutes(event["time_end"])
           if day == event.get("date_end", event["date_start"]) else DAY_END_MINUTE)
    return start, end


def _initial_scroll_minute(events, days, include_now):
    relevant = []
    for event in events:
        if event.get("all_day") or event.get("time_start") is None:
            continue
        for day in days:
            if event["date_start"] <= day <= event.get("date_end", event["date_start"]):
                start, _end = _timed_segment_minutes(event, day)
                relevant.append(start)
    if include_now:
        now = datetime.datetime.now()
        relevant.append(now.hour * 60 + now.minute)
    return max(0, min(relevant) - 60) if relevant else 8 * 60


def _event_label_parts(event, start_minutes, end_minutes, all_day):
    title = event.get("summary") or _("Untitled")
    when = _("All day") if all_day else (
        f"{_format_minutes(start_minutes)}–{_format_minutes(end_minutes)}"
    )
    return title, when, event.get("location", "")


def _format_minutes(minutes):
    if minutes == DAY_END_MINUTE:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _draw_day_grid(widget, cr):
    width = widget.get_allocated_width()
    cr.set_source_rgba(0.45, 0.45, 0.45, 0.32)
    cr.set_line_width(1)
    for hour in range(25):
        y = _timeline_y(hour * 60) + 0.5
        cr.move_to(0, y)
        cr.line_to(width, y)
        cr.stroke()
    cr.set_source_rgba(0.35, 0.35, 0.35, 0.75)
    cr.set_line_width(2)
    cr.move_to(0, ALL_DAY_HEIGHT)
    cr.line_to(width, ALL_DAY_HEIGHT)
    cr.stroke()


def _draw_now_line(widget, cr):
    now = datetime.datetime.now()
    y = _timeline_y(now.hour * 60 + now.minute) + 0.5
    cr.set_source_rgba(0.88, 0.10, 0.14, 0.9)
    cr.set_line_width(1)
    cr.move_to(0, y)
    cr.line_to(widget.get_allocated_width(), y)
    cr.stroke()


def _assign_event_columns(events):
    """Assign equal-width columns to each transitive group of overlaps."""
    ordered = sorted(events, key=lambda item: (item["start"], item["end"]))
    index = 0
    while index < len(ordered):
        group = [ordered[index]]
        group_end = ordered[index]["end"]
        index += 1
        while index < len(ordered) and ordered[index]["start"] < group_end:
            group.append(ordered[index])
            group_end = max(group_end, ordered[index]["end"])
            index += 1

        active = []
        column_count = 0
        for item in group:
            active = [(end, column) for end, column in active if end > item["start"]]
            occupied = {column for _end, column in active}
            column = next(candidate for candidate in range(len(group) + 1)
                          if candidate not in occupied)
            item["column"] = column
            active.append((item["end"], column))
            column_count = max(column_count, column + 1)
        for item in group:
            item["columns"] = column_count
