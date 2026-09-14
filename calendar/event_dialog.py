import datetime
from typing import Optional

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango
from xapp.util import l10n

_ = l10n("clockenstein")

from formatting import uses_12_hour_clock
from store import CalendarManager
from backends.google import google_event_fits_sync_range


class _DatePicker(Gtk.MenuButton):
    def __init__(self):
        super().__init__()
        self.label = Gtk.Label()
        self.add(self.label)
        self.calendar = Gtk.Calendar()
        popover = Gtk.Popover.new(self)
        popover.add(self.calendar)
        self.set_popover(popover)
        self.calendar.connect("day-selected", self._on_day_selected)
        self.set_date(datetime.date.today())
        popover.show_all()

    def set_date(self, date):
        self.calendar.select_month(date.month - 1, date.year)
        self.calendar.select_day(date.day)
        self.label.set_text(date.strftime("%x"))

    def get_date(self):
        year, month, day = self.calendar.get_date()
        return datetime.date(year, month + 1, day)

    def _on_day_selected(self, _calendar):
        self.label.set_text(self.get_date().strftime("%x"))
        popover = self.get_popover()
        if popover.get_visible():
            popover.popdown()


def _format_time_spin(spin):
    spin.set_text(f"{spin.get_value_as_int():02d}")
    return True


class _TimePicker(Gtk.Box):
    """A time input that follows the system's 12/24-hour clock setting."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        self._is_12h = uses_12_hour_clock()
        self.hour = Gtk.SpinButton.new_with_range(1, 12, 1) if self._is_12h \
            else Gtk.SpinButton.new_with_range(0, 23, 1)
        self.minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        for spin in (self.hour, self.minute):
            spin.set_numeric(True)
            spin.set_wrap(True)
            spin.set_width_chars(2)
            spin.connect("output", _format_time_spin)
        self.pack_start(self.hour, False, False, 0)
        self.pack_start(Gtk.Label(label=":"), False, False, 0)
        self.pack_start(self.minute, False, False, 0)
        self.period = None
        if self._is_12h:
            self.period = Gtk.ComboBoxText()
            self.period.append("AM", _("AM"))
            self.period.append("PM", _("PM"))
            self.period.set_active_id("AM")
            self.pack_start(self.period, False, False, 0)

    def connect_changed(self, callback):
        self.hour.connect("value-changed", callback)
        self.minute.connect("value-changed", callback)
        if self.period is not None:
            self.period.connect("changed", callback)

    def get_time(self):
        hour = self.hour.get_value_as_int()
        minute = self.minute.get_value_as_int()
        if self.period is not None:
            hour %= 12
            if self.period.get_active_id() == "PM":
                hour += 12
        return datetime.time(hour, minute)

    def set_time(self, value):
        hour = value.hour
        if self.period is not None:
            is_pm = hour >= 12
            hour = hour % 12 or 12
            self.period.set_active_id("PM" if is_pm else "AM")
        self.hour.set_value(hour)
        self.minute.set_value(value.minute)


class EventDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        store: CalendarManager,
        event: Optional[dict] = None,
        default_date: Optional[datetime.date] = None,
        calendar_options: Optional[list] = None,
    ):
        is_new = event is None
        editable = is_new or bool(event.get("editable", True))
        super().__init__(
            title=_("New Event") if is_new else (_("Event Details")),
            transient_for=parent,
            modal=True,
        )
        self.store = store
        self.event = event or {}
        self.is_new = is_new
        self.editable = editable
        self._populating = True
        self._adjusting_end = False
        if is_new:
            self.calendar_options = (calendar_options if calendar_options is not None
                                     else store.writable_calendars())
        elif editable:
            self.calendar_options = [
                calendar for calendar in store.writable_calendars()
                if calendar.get("provider") == self.event.get("provider")
                and calendar.get("account_id") == self.event.get("account_id")
            ] or [self.event]
        else:
            self.calendar_options = [self.event]

        self.set_default_size(420, -1)
        self.add_button(_("Cancel") if editable else _("Close"), Gtk.ResponseType.CANCEL)
        if not is_new and editable:
            del_btn = self.add_button(_("Delete"), Gtk.ResponseType.REJECT)
            del_btn.get_style_context().add_class("destructive-action")
            del_btn.connect("clicked", self._on_delete)
        if editable:
            save_btn = self.add_button(_("Save"), Gtk.ResponseType.OK)
            save_btn.get_style_context().add_class("suggested-action")
            self.set_default_response(Gtk.ResponseType.OK)

        self._build_form()
        self._populate(default_date)
        self._populating = False
        self.connect("response", self._on_response)
        if not editable:
            self._set_form_sensitive(False)
            if (self.event.get("provider") == "google"
                    and self.event.get("event_type") != "default"):
                event_type = self.event.get("event_type", "")
                event_type_name = {
                    "birthday": _("Birthday"),
                    "focusTime": _("Focus time"),
                    "fromGmail": _("From Gmail"),
                    "outOfOffice": _("Out of office"),
                    "workingLocation": _("Working location"),
                }.get(event_type, event_type)
                self.status_label.get_style_context().remove_class("error")
                self.status_label.get_style_context().add_class("dim-label")
                self.status_label.set_text(
                    _("This type of event (%s) cannot be edited.")
                    % event_type_name
                )

    def _build_form(self):
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(12)
        box.set_margin_bottom(4)
        box.set_margin_start(16)
        box.set_margin_end(16)

        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(8)
        box.pack_start(grid, True, True, 0)

        def lbl(text):
            l = Gtk.Label(label=text)
            l.set_xalign(1.0)
            l.get_style_context().add_class("dim-label")
            return l

        grid.attach(lbl(_("Title")), 0, 0, 1, 1)
        self.title_entry = Gtk.Entry()
        self.title_entry.set_hexpand(True)
        self.title_entry.set_activates_default(True)
        grid.attach(self.title_entry, 1, 0, 2, 1)

        grid.attach(lbl(_("Calendar")), 0, 1, 1, 1)
        self.calendar_model = Gtk.ListStore(str, str)
        for cal in self.calendar_options:
            provider = cal.get("provider", "local")
            owner = _("Local") if provider == "local" else cal.get("account_name", cal.get("account_id", "Google"))
            self.calendar_model.append([
                cal.get("color", cal.get("calendar_color", "#2aa198")),
                f"{cal.get('name', cal.get('calendar_name', _('Calendar')))} — {owner}",
            ])
        self.calendar_combo = Gtk.ComboBox.new_with_model(self.calendar_model)
        color_cell = Gtk.CellRendererText()
        color_cell.set_property("text", "●")
        color_cell.set_property("scale", 1.25)
        self.calendar_combo.pack_start(color_cell, False)
        self.calendar_combo.add_attribute(color_cell, "foreground", 0)
        text_cell = Gtk.CellRendererText()
        self.calendar_combo.pack_start(text_cell, True)
        self.calendar_combo.add_attribute(text_cell, "text", 1)
        active_calendar = next(
            (index for index, calendar in enumerate(self.calendar_options)
             if calendar.get("id", calendar.get("calendar_id"))
             == self.event.get("calendar_id")),
            0,
        )
        self.calendar_combo.set_active(active_calendar)
        self.calendar_combo.set_sensitive(self.editable and len(self.calendar_options) > 1)
        grid.attach(self.calendar_combo, 1, 1, 2, 1)

        grid.attach(lbl(_("All day")), 0, 2, 1, 1)
        self.allday_switch = Gtk.Switch()
        self.allday_switch.set_halign(Gtk.Align.START)
        self.allday_switch.connect("notify::active", self._on_allday_toggled)
        grid.attach(self.allday_switch, 1, 2, 1, 1)

        grid.attach(lbl(_("Start")), 0, 3, 1, 1)
        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.date_picker = _DatePicker()
        start_box.pack_start(self.date_picker, False, False, 0)
        self.start_time = _TimePicker()
        start_box.pack_start(self.start_time, False, False, 0)
        grid.attach(start_box, 1, 3, 2, 1)

        grid.attach(lbl(_("End")), 0, 4, 1, 1)
        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.end_date_picker = _DatePicker()
        end_box.pack_start(self.end_date_picker, False, False, 0)
        self.end_time = _TimePicker()
        end_box.pack_start(self.end_time, False, False, 0)
        grid.attach(end_box, 1, 4, 2, 1)

        self.date_picker.calendar.connect("day-selected", self._on_start_changed)
        self.start_time.connect_changed(self._on_start_changed)
        self.end_date_picker.calendar.connect("day-selected", self._on_end_changed)
        self.end_time.connect_changed(self._on_end_changed)

        grid.attach(lbl(_("Location")), 0, 5, 1, 1)
        self.location_entry = Gtk.Entry()
        self.location_entry.set_hexpand(True)
        self.location_entry.set_placeholder_text(_("Optional"))
        grid.attach(self.location_entry, 1, 5, 2, 1)

        grid.attach(lbl(_("Notes")), 0, 6, 1, 1)
        self.desc_view = Gtk.TextView()
        self.desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.desc_view.set_left_margin(8)
        self.desc_view.set_right_margin(8)
        self.desc_view.set_top_margin(6)
        self.desc_view.set_bottom_margin(6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(-1, 72)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(self.desc_view)
        grid.attach(scroll, 1, 6, 2, 1)

        self.status_label = Gtk.Label(label="")
        self.status_label.set_xalign(0)
        self.status_label.set_selectable(True)
        self.status_label.set_line_wrap(True)
        self.status_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.status_label.get_style_context().add_class("error")
        box.pack_start(self.status_label, False, False, 0)

        box.show_all()

    def _populate(self, default_date):
        ev = self.event
        self.title_entry.set_text(ev.get("summary", ""))
        self.location_entry.set_text(ev.get("location", ""))
        self.desc_view.get_buffer().set_text(ev.get("description", ""))

        all_day = ev.get("all_day", True)
        self.allday_switch.set_active(all_day)

        date = ev.get("date_start") or default_date or datetime.date.today()
        start_time = ev.get("time_start") or datetime.datetime.now().replace(
            minute=0, second=0, microsecond=0).time()
        default_end = datetime.datetime.combine(date, start_time) + datetime.timedelta(hours=1)
        end_time = ev.get("time_end") or default_end.time()
        end_date = ev.get("date_end") or (date if all_day else default_end.date())
        self.date_picker.set_date(date)
        self.end_date_picker.set_date(end_date)
        self.start_time.set_time(start_time)
        self.end_time.set_time(end_time)

        self._on_allday_toggled(self.allday_switch, None)

    def _on_allday_toggled(self, switch, _param):
        timed = self.editable and not switch.get_active()
        self.start_time.set_sensitive(timed)
        self.end_time.set_sensitive(timed)
        if timed:
            self._ensure_valid_end()

    def _on_start_changed(self, _widget):
        self._ensure_valid_end()

    def _on_end_changed(self, _widget):
        self._ensure_valid_end()

    def _ensure_valid_end(self):
        if self._populating or self._adjusting_end:
            return
        start_date = self.date_picker.get_date()
        end_date = self.end_date_picker.get_date()
        if self.allday_switch.get_active():
            if end_date < start_date:
                self._adjusting_end = True
                self.end_date_picker.set_date(start_date)
                self._adjusting_end = False
            return
        start = datetime.datetime.combine(start_date, self.start_time.get_time())
        end = datetime.datetime.combine(end_date, self.end_time.get_time())
        if end <= start:
            self._set_end_datetime(start + datetime.timedelta(hours=1))

    def _set_end_datetime(self, value):
        self._adjusting_end = True
        self.end_date_picker.set_date(value.date())
        self.end_time.set_time(value.time())
        self._adjusting_end = False

    def _set_form_sensitive(self, sensitive):
        for widget in (self.title_entry, self.allday_switch, self.date_picker, self.end_date_picker,
                       self.start_time, self.end_time, self.location_entry, self.desc_view):
            widget.set_sensitive(sensitive)

    def _on_response(self, _dialog, response):
        if response == Gtk.ResponseType.OK:
            if not self._save():
                _dialog.stop_emission_by_name("response")

    def _save(self) -> bool:
        summary = self.title_entry.get_text().strip() or _("Untitled")

        self._ensure_valid_end()
        date = self.date_picker.get_date()
        end_date = self.end_date_picker.get_date()

        all_day = self.allday_switch.get_active()
        time_start = time_end = None

        if not all_day:
            time_start = self.start_time.get_time()
            time_end = self.end_time.get_time()

        buf = self.desc_view.get_buffer()
        data = {
            "summary":     summary,
            "location":    self.location_entry.get_text().strip(),
            "description": buf.get_text(*buf.get_bounds(), True),
            "all_day":     all_day,
            "date_start":  date,
            "date_end":    end_date,
            "time_start":  time_start,
            "time_end":    time_end,
            "original_calendar_id": self.event.get("calendar_id"),
        }
        calendar = self.calendar_options[self.calendar_combo.get_active()]
        if (calendar.get("provider") == "google"
                and not google_event_fits_sync_range(calendar, date, end_date)):
            self.status_label.set_text(
                _("The event dates are outside the sync range for %s.")
                % calendar.get("name", calendar.get("calendar_name", _("this calendar")))
            )
            return False
        data.update({"calendar_id": calendar.get("id", calendar.get("calendar_id")),
                     "provider": calendar.get("provider", "local"),
                     "account_id": calendar.get("account_id", "local")})

        try:
            if self.is_new:
                self.store.create_event(data)
            else:
                self.store.update_event(self.event["uid"], data)
        except Exception as ex:
            self.status_label.set_text(_("Error: %s") % ex)
            return False

        return True

    def _on_delete(self, _btn):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Delete '%s'?") % self.event.get("summary", _("Untitled")),
        )
        dlg.format_secondary_text(_("This event will be permanently deleted."))
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.YES:
            try:
                self.store.delete_event(self.event["uid"], self.event.get("calendar_id"),
                                        self.event.get("provider", "local"), self.event.get("account_id"))
                self.response(Gtk.ResponseType.REJECT)
            except Exception as ex:
                self.status_label.set_text(_("Error: %s") % ex)
