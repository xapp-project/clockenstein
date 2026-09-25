import datetime
import locale
from typing import Optional

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango
from xapp.threading import run_async, run_idle
from xapp.util import l10n
from clockenstein import DEFAULT_COLOR
from clockenstein.misc import get_calendar_key

_ = l10n("clockenstein")

from store import CalendarManager
from backends.google import google_event_fits_sync_range

LAST_CALENDAR_KEY = "last-calendar-used"


def _uses_12_hour_clock(time_format):
    if time_format == "12-hour":
        return True
    elif time_format == "24-hour":
        return False
    else:
        time_pattern = locale.nl_langinfo(locale.T_FMT)
        return "%I" in time_pattern or "%r" in time_pattern


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
    value = spin.get_value_as_int()
    spin.set_text(str(value) if spin.get_adjustment().get_lower() == 1 else f"{value:02d}")
    return True


def _time_picker(use_12_hour):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
    hour = Gtk.SpinButton.new_with_range(1 if use_12_hour else 0,
                                         12 if use_12_hour else 23, 1)
    minute = Gtk.SpinButton.new_with_range(0, 59, 1)
    for spin in (hour, minute):
        spin.set_numeric(True)
        spin.set_wrap(True)
        spin.set_width_chars(2)
        spin.connect("output", _format_time_spin)
    box.pack_start(hour, False, False, 0)
    box.pack_start(Gtk.Label(label=":"), False, False, 0)
    box.pack_start(minute, False, False, 0)
    period = None
    if use_12_hour:
        period = Gtk.ComboBoxText()
        period.append("am", _("AM"))
        period.append("pm", _("PM"))
        period.set_active_id("am")
        box.pack_start(period, False, False, 0)
    return box, hour, minute, period


class EventDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        store: CalendarManager,
        calendar_options: list,
        event: Optional[dict] = None,
        default_date: Optional[datetime.date] = None,
        time_format="locale",
        settings=None,
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
        self.use_12_hour = _uses_12_hour_clock(time_format)
        self._populating = True
        self._adjusting_end = False
        self._saving = False
        self._saved = False
        self._destroyed = False
        self.calendar_options = calendar_options
        self.settings = settings

        self.set_default_size(420, -1)
        self.add_button(_("Cancel") if editable else _("Close"), Gtk.ResponseType.CANCEL)
        if not is_new and editable:
            del_btn = Gtk.Button.new_with_label(_("Delete"))
            del_btn.get_style_context().add_class("destructive-action")
            del_btn.connect("clicked", self._on_delete)
            self.get_action_area().pack_start(del_btn, False, False, 0)
        if editable:
            save_btn = self.add_button(_("Save"), Gtk.ResponseType.OK)
            save_btn.get_style_context().add_class("suggested-action")
            self.set_default_response(Gtk.ResponseType.OK)

        self._build_form()
        self._populate(default_date)
        self._populating = False
        self.connect("response", self._on_response)
        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)
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
        self.form_grid = grid
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
        for calendar_info in self.calendar_options:
            provider = calendar_info.get("provider", "local")
            owner = _("Local") if provider == "local" else calendar_info.get("account_name", calendar_info.get("account_id", "Google"))
            self.calendar_model.append([
                calendar_info.get("color", calendar_info.get("calendar_color", DEFAULT_COLOR)),
                f"{calendar_info.get('name', calendar_info.get('calendar_name', _('Calendar')))} — {owner}",
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
        if self.is_new and self.settings is not None:
            wanted = self.settings.get_string(LAST_CALENDAR_KEY)
        else:
            wanted = get_calendar_key(self.event)
        active_calendar = 0
        for index, calendar in enumerate(self.calendar_options):
            if get_calendar_key(calendar) == wanted:
                active_calendar = index
                break
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
        self.start_time, self.start_hour, self.start_minute, self.start_period = _time_picker(
            self.use_12_hour
        )
        start_box.pack_start(self.start_time, False, False, 0)
        grid.attach(start_box, 1, 3, 2, 1)

        grid.attach(lbl(_("End")), 0, 4, 1, 1)
        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.end_date_picker = _DatePicker()
        end_box.pack_start(self.end_date_picker, False, False, 0)
        self.end_time, self.end_hour, self.end_minute, self.end_period = _time_picker(
            self.use_12_hour
        )
        end_box.pack_start(self.end_time, False, False, 0)
        grid.attach(end_box, 1, 4, 2, 1)

        self.date_picker.calendar.connect("day-selected", self._on_start_changed)
        self.start_hour.connect("value-changed", self._on_start_changed)
        self.start_minute.connect("value-changed", self._on_start_changed)
        if self.start_period:
            self.start_period.connect("changed", self._on_start_changed)
        self.end_date_picker.calendar.connect("day-selected", self._on_end_changed)
        self.end_hour.connect("value-changed", self._on_end_changed)
        self.end_minute.connect("value-changed", self._on_end_changed)
        if self.end_period:
            self.end_period.connect("changed", self._on_end_changed)

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
        event = self.event
        self.title_entry.set_text(event.get("summary", ""))
        self.location_entry.set_text(event.get("location", ""))
        self.desc_view.get_buffer().set_text(event.get("description", ""))

        all_day = event.get("all_day", True)
        self.allday_switch.set_active(all_day)

        date = event.get("date_start") or default_date or datetime.date.today()
        start_time = event.get("time_start") or datetime.datetime.now().replace(
            minute=0, second=0, microsecond=0).time()
        default_end = datetime.datetime.combine(date, start_time) + datetime.timedelta(hours=1)
        end_time = event.get("time_end") or default_end.time()
        end_date = event.get("date_end") or (date if all_day else default_end.date())
        self.date_picker.set_date(date)
        self.end_date_picker.set_date(end_date)
        self._set_picker_time(self.start_hour, self.start_minute, self.start_period, start_time)
        self._set_picker_time(self.end_hour, self.end_minute, self.end_period, end_time)

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
        start = datetime.datetime.combine(start_date, self._get_picker_time(
            self.start_hour, self.start_minute, self.start_period))
        end = datetime.datetime.combine(end_date, self._get_picker_time(
            self.end_hour, self.end_minute, self.end_period))
        if end <= start:
            self._set_end_datetime(start + datetime.timedelta(hours=1))

    def _set_end_datetime(self, value):
        self._adjusting_end = True
        self.end_date_picker.set_date(value.date())
        self._set_picker_time(self.end_hour, self.end_minute, self.end_period, value.time())
        self._adjusting_end = False

    def _set_picker_time(self, hour, minute, period, value):
        if period:
            period.set_active_id("am" if value.hour < 12 else "pm")
            hour.set_value(value.hour % 12 or 12)
        else:
            hour.set_value(value.hour)
        minute.set_value(value.minute)

    @staticmethod
    def _get_picker_time(hour, minute, period):
        hour_value = hour.get_value_as_int()
        if period:
            hour_value %= 12
            if period.get_active_id() == "pm":
                hour_value += 12
        return datetime.time(hour_value, minute.get_value_as_int())

    def _set_form_sensitive(self, sensitive):
        for widget in (self.title_entry, self.allday_switch, self.date_picker, self.end_date_picker,
                       self.start_time, self.end_time, self.location_entry, self.desc_view):
            widget.set_sensitive(sensitive)

    def _on_response(self, _dialog, response):
        if self._saving:
            _dialog.stop_emission_by_name("response")
        elif response == Gtk.ResponseType.OK and not self._saved:
            if self._save():
                self._remember_calendar()
            else:
                _dialog.stop_emission_by_name("response")

    def _remember_calendar(self):
        if self.is_new and self.settings is not None:
            calendar = self.calendar_options[self.calendar_combo.get_active()]
            self.settings.set_string(LAST_CALENDAR_KEY, get_calendar_key(calendar))

    def _on_delete_event(self, _dialog, _event):
        # Closing the dialog cannot cancel a request already sent to the server.
        return self._saving

    def _on_destroy(self, _dialog):
        self._destroyed = True

    def _save(self) -> bool:
        summary = self.title_entry.get_text().strip() or _("Untitled")

        self._ensure_valid_end()
        date = self.date_picker.get_date()
        end_date = self.end_date_picker.get_date()

        all_day = self.allday_switch.get_active()
        time_start = time_end = None

        if not all_day:
            time_start = self._get_picker_time(
                self.start_hour, self.start_minute, self.start_period
            )
            time_end = self._get_picker_time(self.end_hour, self.end_minute, self.end_period)

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

        if data["provider"] != "local":
            self._set_saving(True)
            self.status_label.set_text(_("Saving…"))
            self._save_remote(data)
            return False

        try:
            self._write_event(data)
        except Exception as ex:
            self.status_label.set_text(_("Error: %s") % ex)
            return False

        return True

    def _write_event(self, data):
        if self.is_new:
            self.store.create_event(data)
        else:
            self.store.update_event(self.event["uid"], data)

    def _set_saving(self, saving):
        self._saving = saving
        self.form_grid.set_sensitive(not saving)
        self.get_action_area().set_sensitive(not saving)
        self.set_deletable(not saving)
        style = self.status_label.get_style_context()
        if saving:
            style.remove_class("error")
        else:
            style.add_class("error")

    @run_async
    def _save_remote(self, data):
        try:
            self._write_event(data)
        except Exception as exc:
            self._remote_save_finished(str(exc))
        else:
            self._remote_save_finished(None)

    @run_idle
    def _remote_save_finished(self, error):
        if self._destroyed:
            return
        self._set_saving(False)
        if error is not None:
            self.status_label.set_text(_("Error: %s") % error)
            return
        self._saved = True
        self._remember_calendar()
        self.response(Gtk.ResponseType.OK)

    def _on_delete(self, _btn):
        if self._saving:
            return
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
