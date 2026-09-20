import calendar
import datetime

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango
from xapp.threading import run_async, run_idle
from xapp.util import l10n

_ = l10n("clockenstein")

from event_dialog import EventDialog
from preferences import PreferencesDialog
from backends.google import LIMITED_RANGE, NORMAL_RANGE, RESTRICTED_RANGE
from clockenstein import (AGENT_BUS_NAME, BUS_INTERFACE, BUS_NAME, BUS_PATH,
                          DEFAULT_COLOR, SETTINGS_SCHEMA)
from clockenstein.drawing import draw_centered_circle
from clockenstein.formatting import (capitalize_first, format_time, resolve_first_weekday,
                        start_of_week)
from dbus import notify_changed
from store import CalendarManager, local_timezone, watch_timezone_changes
from views.colors import apply_tinted_event_color
from views.month_view import MonthView
from views.week_view import WeekView
from views.day_view import DayView
from widgets.mini_calendar import MiniCalendar

ADD_LOCAL_CALENDAR_RESPONSE = 1
CONNECT_GOOGLE_RESPONSE = 2
CONNECT_CALDAV_RESPONSE = 3


class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=_("Calendar"))
        self.refresh_timezone()
        self.store = CalendarManager(self.timezone)
        self.settings = Gio.Settings.new(SETTINGS_SCHEMA)
        self.first_weekday = resolve_first_weekday(
            self.settings.get_string("first-day-of-week")
        )
        self.time_format = self.settings.get_string("time-format")
        self.settings.connect(
            "changed::first-day-of-week", self._first_weekday_changed
        )
        self.settings.connect("changed::time-format", self._time_format_changed)
        self.settings.connect("changed::calendar-show-week-numbers", self._show_week_numbers_changed)
        width = self.settings.get_int("calendar-window-width")
        height = self.settings.get_int("calendar-window-height")
        self.set_default_size(width, height)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("clockenstein-calendar")
        self.today = datetime.date.today()
        self.current_date = self.today
        self._month_selected_date = self.today
        self._week_selected_date = self.today
        self._month_week_offset = 0
        self._month_scroll_delta = 0
        self._calendar_dialog_box = None
        saved_view = self.settings.get_string("calendar-default-view")
        view_names = {"month": "Month", "week": "Week", "day": "Day"}
        self._active_view = view_names.get(saved_view, "Month")
        self._refreshing = False
        self._refresh_pending = False
        self._daemon_refreshing = False
        self._service_running = {BUS_NAME: False, AGENT_BUS_NAME: False}
        self.connect("destroy", self._save_window_size)
        self._build_ui()
        # Keep the monitor alive; otherwise it may be garbage-collected.
        self.timezone_monitor = watch_timezone_changes(self.timezone_changed)
        self._subscribe_to_daemon()
        self._watch_services()
        geometry = Gdk.Geometry()
        geometry.min_width = 640
        geometry.min_height = 460
        self.set_geometry_hints(None, geometry, Gdk.WindowHints.MIN_SIZE)
        self._refresh(refresh_remote=False)

    def _subscribe_to_daemon(self):
        try:
            self._daemon_connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._daemon_subscription = self._daemon_connection.signal_subscribe(
                BUS_NAME, BUS_INTERFACE, None, BUS_PATH, None,
                Gio.DBusSignalFlags.NONE, self._daemon_signal
            )
            self.connect("destroy", self._unsubscribe_from_daemon)
        except GLib.Error:
            self._daemon_connection = None
            self._daemon_subscription = 0

    def _unsubscribe_from_daemon(self, _window):
        if self._daemon_connection and self._daemon_subscription:
            self._daemon_connection.signal_unsubscribe(self._daemon_subscription)
            self._daemon_subscription = 0

    def _watch_services(self):
        self._service_watches = [
            Gio.bus_watch_name(Gio.BusType.SESSION, name,
                               Gio.BusNameWatcherFlags.NONE,
                               self._service_appeared, self._service_vanished)
            for name in self._service_running
        ]
        self.connect("destroy", self._unwatch_services)

    def _unwatch_services(self, _window):
        for watch in self._service_watches:
            Gio.bus_unwatch_name(watch)

    def _service_appeared(self, connection, name, owner):
        self._service_running[name] = True
        if name == BUS_NAME:
            connection.call(
                owner, BUS_PATH, BUS_INTERFACE, "GetSyncState", None,
                GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, -1,
                None, self._sync_state_received,
            )
        self._update_range_infobar()

    def _service_vanished(self, _connection, name):
        self._service_running[name] = False
        if name == BUS_NAME:
            self._daemon_refreshing = False
            self._set_refreshing(False)
            self._set_status("")
        self._update_range_infobar()

    def _sync_state_received(self, connection, result):
        try:
            refreshing, = connection.call_finish(result).unpack()
        except GLib.Error:
            return
        self._sync_state_changed(refreshing)

    def _daemon_signal(self, connection, sender, path, interface, signal, parameters):
        if signal == "Changed":
            self._daemon_changed(connection, sender, path, interface, signal, parameters)
        elif signal == "SyncStateChanged":
            refreshing, = parameters.unpack()
            self._sync_state_changed(refreshing)

    def _sync_state_changed(self, refreshing):
        was_refreshing = self._daemon_refreshing
        self._daemon_refreshing = refreshing
        self._update_refreshing()
        if refreshing:
            self._set_status(_("Synchronizing online calendars…"))
        elif was_refreshing and not self._refresh_pending:
            self._set_status(_("Synchronization finished"))

    def _get_service_warning(self):
        daemon_running = self._service_running[BUS_NAME]
        agent_running = self._service_running[AGENT_BUS_NAME]
        if not daemon_running:
            message = _("The daemon is not running.")
            message + "\n" + _("Synchronization and reminders may be unavailable.")
            return message
        if not agent_running:
            message = _("The notification agent is not running.")
            message + "\n" + _("Reminders may be unavailable.")
            return message
        return ""

    def _daemon_changed(self, _connection, _sender, _path, _interface,
                        _signal, _parameters):
        self.store = CalendarManager(self.timezone)
        self._refresh(refresh_remote=False)
        if self._calendar_dialog_box is not None:
            self._fill_calendar_box(self._calendar_dialog_box)

    def timezone_changed(self):
        self.refresh_timezone()
        was_showing_today = self.current_date == self.today
        self.today = datetime.datetime.now(self.timezone).date()
        self.month_view.set_today(self.today)
        self.week_view.set_timezone(self.timezone, self.today)
        self.day_view.set_timezone(self.timezone, self.today)
        if was_showing_today:
            self.current_date = self.today
            self._month_selected_date = self.today
            self._week_selected_date = self.today
            self._sync_mini_cal()
        self.store = CalendarManager(self.timezone)
        self._refresh(refresh_remote=False)

    def refresh_timezone(self):
        self.timezone = local_timezone()

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(_("Calendar"))
        self.set_titlebar(header)

        menu = Gtk.Menu()
        calendars_item = Gtk.MenuItem(label=_("Calendars"))
        calendars_item.connect("activate", self._manage_calendars)
        menu.append(calendars_item)
        preferences_item = Gtk.MenuItem(label=_("Preferences"))
        preferences_item.connect("activate", self._show_preferences)
        menu.append(preferences_item)
        menu.append(Gtk.SeparatorMenuItem())
        about_item = Gtk.MenuItem(label=_("About"))
        about_item.connect("activate", self._show_about)
        menu.append(about_item)
        menu.show_all()
        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name("xsi-open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_button.set_tooltip_text(_("Main menu"))
        menu_button.set_popup(menu)
        header.pack_start(menu_button)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav.get_style_context().add_class("linked")
        for icon, callback in (("xsi-go-previous-symbolic", lambda _: self._navigate(-1)),
                               (None, lambda _: self._go_today()),
                               ("xsi-go-next-symbolic", lambda _: self._navigate(1))):
            button = (Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
                      if icon else Gtk.Button(label=_("Today")))
            button.connect("clicked", callback)
            nav.pack_start(button, False, False, 0)
        header.pack_start(nav)

        view_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        view_box.set_homogeneous(False)
        view_box.get_style_context().add_class("linked")
        view_box.get_style_context().add_class("path-bar")
        self.view_buttons = {}
        for name, label in (("Month", _("Month")), ("Week", _("Week")),
                            ("Day", _("Day"))):
            button = Gtk.ToggleButton(label=label)
            button.connect("toggled", self._on_view_toggle, name)
            view_box.pack_start(button, False, False, 0)
            self.view_buttons[name] = button
        header.set_custom_title(view_box)

        self.new_button = Gtk.Button.new_from_icon_name("xsi-list-add-symbolic", Gtk.IconSize.BUTTON)
        self.new_button.set_tooltip_text(_("New event (Ctrl+N)"))
        self.new_button.connect("clicked", lambda _: self._new_event())
        header.pack_end(self.new_button)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.spinner.hide()
        header.pack_end(self.spinner)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        vbox.pack_start(body, True, True, 0)
        body.pack_start(self._build_sidebar(), False, False, 0)
        body.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        calendar_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        calendar_area.set_hexpand(True)
        calendar_area.set_vexpand(True)
        body.pack_start(calendar_area, True, True, 0)
        self.range_infobar = Gtk.InfoBar()
        self.range_infobar.set_message_type(Gtk.MessageType.INFO)
        self.range_infobar.set_show_close_button(True)
        self.range_infobar.set_no_show_all(True)
        self.range_infobar.connect("response", lambda bar, _response: bar.hide())
        self.range_infobar_label = Gtk.Label(xalign=0)
        self.range_infobar_label.set_line_wrap(True)
        self.range_infobar.get_content_area().pack_start(
            self.range_infobar_label, True, True, 0
        )
        calendar_area.pack_start(self.range_infobar, False, False, 0)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=100)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        calendar_area.pack_start(self.stack, True, True, 0)
        self.month_view = MonthView(
            self.today, self._on_event_activated, self._new_event,
            self._scroll_month, self._select_month_date, self.first_weekday, self.time_format
        )
        self.week_view = WeekView(
            self.today, self.timezone, self._on_event_activated, self._new_event,
            self._select_week_date, self.first_weekday, self.time_format
        )
        self.day_view = DayView(self.today, self.timezone, self._on_event_activated,
                                self._new_event, self.time_format)
        for name, view in (("Month", self.month_view), ("Week", self.week_view),
                           ("Day", self.day_view)):
            self.stack.add_named(view, name)
        self.view_buttons[self._active_view].set_active(True)
        self.stack.set_visible_child_name(self._active_view)
        self.connect("key-press-event", self._on_key)

    def _build_sidebar(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_size_request(225, -1)
        outer.set_hexpand(False)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(8)
        self.mini_cal = MiniCalendar(
            self.current_date, self._on_mini_date_selected, self.first_weekday,
            self.settings.get_boolean("calendar-show-week-numbers")
        )
        outer.pack_start(self.mini_cal, False, False, 0)
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        calendars_label = Gtk.Label(label=_("Calendars"), xalign=0)
        calendars_label.get_style_context().add_class("clockenstein-section-label")
        outer.pack_start(calendars_label, False, False, 0)

        visible_scroll = Gtk.ScrolledWindow()
        visible_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        visible_scroll.set_propagate_natural_height(True)
        visible_scroll.set_max_content_height(180)
        self.visible_calendar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        visible_scroll.add(self.visible_calendar_box)
        outer.pack_start(visible_scroll, False, False, 0)
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        upcoming_scroll = Gtk.ScrolledWindow()
        upcoming_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.upcoming_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        upcoming_scroll.add(self.upcoming_box)
        outer.pack_start(upcoming_scroll, True, True, 0)
        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        self.status_label.set_line_wrap(True)
        self.status_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.status_label.set_max_width_chars(28)
        self.status_label.set_no_show_all(True)
        self.status_label.get_style_context().add_class("clockenstein-status")
        outer.pack_start(self.status_label, False, False, 0)
        return outer

    def _show_preferences(self, _item):
        dialog = PreferencesDialog(self, self.settings)
        dialog.run()
        dialog.destroy()

    def _first_weekday_changed(self, settings, _key):
        self.first_weekday = resolve_first_weekday(
            settings.get_string("first-day-of-week")
        )
        self.month_view.set_first_weekday(self.first_weekday)
        self.week_view.set_first_weekday(self.first_weekday)
        self.mini_cal.set_first_weekday(self.first_weekday)
        self._month_week_offset = 0
        self._refresh(refresh_remote=False)

    def _time_format_changed(self, settings, _key):
        self.time_format = settings.get_string("time-format")
        self.month_view.set_time_format(self.time_format)
        self.week_view.set_time_format(self.time_format)
        self.day_view.set_time_format(self.time_format)
        self._refresh(refresh_remote=False)

    def _show_week_numbers_changed(self, settings, _key):
        self.mini_cal.set_show_week_numbers(settings.get_boolean("calendar-show-week-numbers"))

    def _show_about(self, _item):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("Clockenstein")
        dialog.set_version("__PROJECT_VERSION__")
        dialog.set_comments(_("A calendar application for Linux desktops"))
        dialog.set_logo_icon_name("clockenstein-calendar")
        dialog.set_website("https://github.com/xapp-project/clockenstein")
        dialog.set_license_type(Gtk.License.GPL_3_0)
        dialog.run()
        dialog.destroy()

    def _populate_calendar_list(self, events):
        for child in self.visible_calendar_box.get_children():
            self.visible_calendar_box.remove(child)
        for calendar_info in self._get_sorted_calendars():
            if calendar_info.get("visible", True):
                label = self._calendar_label(calendar_info)
                if not self._calendar_available(calendar_info):
                    label.set_opacity(0.5)
                self.visible_calendar_box.pack_start(label, False, False, 0)
        self.visible_calendar_box.show_all()
        self._populate_upcoming(events)

        states = self.store.google.get_account_states() + self.store.caldav.get_account_states()
        offline = [s for s in states if not s.get("online")]
        if offline:
            names = ", ".join(s["name"] for s in offline)
            self._set_status(
                _("Some online calendars are disconnected (read-only).") + " " + names
            )
        else:
            self._set_status("")
        self._update_range_infobar()

    def _get_google_calendars_out_of_range(self):
        start, end = self._get_date_range()
        today = datetime.date.today()
        ranges = {
            "normal": NORMAL_RANGE,
            "limited": LIMITED_RANGE,
            "restricted": RESTRICTED_RANGE,
        }
        calendars = []
        for calendar_info in self.store.google.list_calendars():
            if not calendar_info.get("visible", True) or calendar_info.get("sync_range") == "too-big":
                continue
            past_days, future_days = ranges.get(calendar_info.get("sync_range", "normal"),
                                                NORMAL_RANGE)
            synced_start = today - datetime.timedelta(days=past_days)
            synced_end = today + datetime.timedelta(days=future_days)
            if start < synced_start or end > synced_end:
                calendars.append(calendar_info["name"])
        return calendars

    def _update_range_infobar(self):
        service_warning = self._get_service_warning()
        calendars = self._get_google_calendars_out_of_range() if not service_warning else []
        if not service_warning and not calendars:
            self.range_infobar.hide()
            return
        if service_warning:
            message = service_warning
        else:
            names = ", ".join(calendars)
            message = _("This date is outside the sync range for: %s. Events may be missing.") % names
        self.range_infobar.set_message_type(
            Gtk.MessageType.WARNING if service_warning else Gtk.MessageType.INFO
        )
        self.range_infobar_label.set_text(message)
        self.range_infobar.get_content_area().show_all()
        self.range_infobar.show()

    def _populate_upcoming(self, events):
        for child in self.upcoming_box.get_children():
            self.upcoming_box.remove(child)

        now = datetime.datetime.now()
        today = now.date()
        upcoming = []
        for event in events:
            start_date = event["date_start"]
            start_time = event.get("time_start")
            if start_date < today:
                continue
            if start_date == today and not event.get("all_day"):
                if start_time is None or start_time < now.time():
                    continue
            upcoming.append(event)

        upcoming.sort(key=lambda event: (event["date_start"],
                                         event.get("time_start") or datetime.time.min))
        upcoming = upcoming[:6]
        tomorrow = today + datetime.timedelta(days=1)
        groups = ((_("Today"), [event for event in upcoming if event["date_start"] == today], False),
                  (_("Tomorrow"), [event for event in upcoming if event["date_start"] == tomorrow], False),
                  (_("Coming Up"), [event for event in upcoming if event["date_start"] > tomorrow], True))
        for heading, events, show_date in groups:
            if not events:
                continue
            label = Gtk.Label(label=heading, xalign=0)
            label.get_style_context().add_class("clockenstein-section-label")
            self.upcoming_box.pack_start(label, False, False, 0)
            for event in events:
                self.upcoming_box.pack_start(self._upcoming_row(event, show_date), False, False, 0)
        if not upcoming:
            empty = Gtk.Label(label=_("No upcoming events"), xalign=0)
            empty.get_style_context().add_class("clockenstein-status")
            self.upcoming_box.pack_start(empty, False, False, 4)
        self.upcoming_box.show_all()

    def _upcoming_row(self, event, show_date):
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("clockenstein-upcoming-row")
        if event.get("all_day"):
            apply_tinted_event_color(button, event)
        button.connect("clicked", lambda _button: self._on_event_activated(event))
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if not event.get("all_day"):
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(10, 10)
            swatch.set_valign(Gtk.Align.START)
            swatch.set_margin_top(4)
            rgba = Gdk.RGBA()
            rgba.parse(event.get("calendar_color", DEFAULT_COLOR))
            swatch.connect("draw", draw_centered_circle, rgba)
            content.pack_start(swatch, False, False, 0)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label=event.get("summary") or _("Untitled"), xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("clockenstein-upcoming-title")
        parts = [capitalize_first(event["date_start"].strftime("%A %-d %b"))] if show_date else []
        if not event.get("all_day") and event.get("time_start"):
            parts.append(format_time(event["time_start"], self.time_format))
        when = " · ".join(parts)
        if when:
            detail = Gtk.Label(label=when, xalign=0)
            detail.get_style_context().add_class("clockenstein-upcoming-detail")
            labels.pack_start(detail, False, False, 0)
        labels.pack_start(title, False, False, 0)
        content.pack_start(labels, True, True, 0)
        button.add(content)
        return button

    def _fill_calendar_box(self, box):
        for child in box.get_children():
            box.remove(child)

        calendars = self.store.list_calendars()
        local_calendars = []
        for calendar_info in calendars:
            if calendar_info["provider"] == "local":
                local_calendars.append(calendar_info)
        accounts = [(_("Local"), "local", local_calendars, None, None)]
        account_keys = []
        for calendar_info in calendars:
            key = (calendar_info["provider"], calendar_info.get("account_id"))
            if calendar_info["provider"] != "local" and key not in account_keys:
                account_keys.append(key)
        states = {}
        for backend in (self.store.google, self.store.caldav):
            for state in backend.get_account_states():
                states[(backend.provider, state["id"])] = state
        for provider, account_id in account_keys:
            state = states.get((provider, account_id), {})
            label = state.get("name", account_id)
            status = _("Online") if state.get("online") else _("Offline, read only")
            frequency = (_("Sync every 2 hours") if provider == "google"
                         else _("Sync every 15 minutes"))
            account_calendars = []
            for calendar_info in calendars:
                if (calendar_info["provider"] == provider
                        and calendar_info.get("account_id") == account_id):
                    account_calendars.append(calendar_info)
            accounts.append((label, (provider, account_id), account_calendars, status, frequency))

        table = Gtk.Grid(column_spacing=16, row_spacing=4)
        table.set_hexpand(True)
        table.set_margin_top(4)
        table.set_margin_bottom(4)
        row = 0

        headings = ("", _("Sync range"), _("Status"),
                    _("Reminders"), _("Visible"), _("Actions"))
        for column, heading in enumerate(headings):
            label = Gtk.Label(label=heading, xalign=0.5)
            label.get_style_context().add_class("dim-label")
            label.get_style_context().add_class("clockenstein-calendar-heading")
            if column == 0:
                label.set_hexpand(True)
            table.attach(label, column, row, 1, 1)
        row += 1

        for account_name, account_id, items, account_status, frequency in accounts:
            if account_id != "local":
                items = sorted(items, key=self._get_google_calendar_sort_key)

            account_label = Gtk.Label(label=account_name, xalign=0)
            account_label.set_hexpand(True)
            account_label.get_style_context().add_class("clockenstein-calendar-account")
            table.attach(account_label, 0, row, 1, 1)
            if account_status:
                status_label = Gtk.Label(label=f"{account_status} · {frequency}", xalign=0)
                status_label.get_style_context().add_class("clockenstein-calendar-account-status")
                table.attach(status_label, 2, row, 1, 1)
            if account_id != "local":
                disconnect = Gtk.Button.new_from_icon_name(
                    "xsi-window-close-symbolic", Gtk.IconSize.MENU
                )
                disconnect.set_relief(Gtk.ReliefStyle.NONE)
                disconnect.set_tooltip_text(_("Disconnect %s") % account_id[1])
                disconnect.connect("clicked", self._disconnect_remote,
                                   account_id[0], account_id[1])
                disconnect.set_halign(Gtk.Align.START)
                table.attach(disconnect, 5, row, 1, 1)
            row += 1

            if account_id == "local":
                calendar_groups = [(_("Local calendars"), items)]
            else:
                writable_calendars = []
                other_calendars = []
                for calendar_info in items:
                    if calendar_info.get("writable", False):
                        writable_calendars.append(calendar_info)
                    else:
                        other_calendars.append(calendar_info)
                calendar_groups = [
                    (_("My Calendars"), writable_calendars),
                    (_("Other Calendars"), other_calendars),
                ]
            for group_name, group_items in calendar_groups:
                if not group_items:
                    continue
                group_label = Gtk.Label(label=group_name, xalign=0)
                group_label.get_style_context().add_class("clockenstein-calendar-group")
                table.attach(group_label, 0, row, 6, 1)
                row += 1

                for calendar_info in group_items:
                    calendar_label = self._calendar_label(calendar_info)
                    calendar_label.set_margin_start(32)
                    status = Gtk.Label(
                        label=(self._get_calendar_sync_status_label(calendar_info)
                               if calendar_info["provider"] != "local" else ""), xalign=0
                    )
                    status.set_ellipsize(Pango.EllipsizeMode.END)
                    status.set_max_width_chars(32)
                    status.set_tooltip_text(self._get_calendar_sync_status_label(calendar_info))
                    status.get_style_context().add_class("dim-label")
                    visibility = Gtk.Switch()
                    visibility.set_active(calendar_info.get("visible", True))
                    visibility.set_halign(Gtk.Align.CENTER)
                    visibility.set_valign(Gtk.Align.CENTER)
                    visibility.set_tooltip_text(_("Show this calendar"))
                    visibility.connect("notify::active", self._calendar_switch_toggled, calendar_info)
                    reminders = self._calendar_reminders_toggle(calendar_info)
                    refresh = Gtk.Button.new_from_icon_name(
                        "xsi-view-refresh-symbolic", Gtk.IconSize.MENU
                    )
                    refresh.set_relief(Gtk.ReliefStyle.NONE)
                    elapsed = (datetime.datetime.now().timestamp() - int(calendar_info["last_sync"])
                               if calendar_info.get("last_sync") else None)
                    recently_synced = (elapsed is not None and not calendar_info.get("sync_error")
                                       and elapsed < 5 * 60)
                    if recently_synced:
                        refresh.set_sensitive(False)
                        refresh.set_tooltip_text(
                            _("Already synchronized less than 5 minutes ago")
                        )
                        if calendar_info.get("sync_range") != "too-big":
                            GLib.timeout_add_seconds(
                                max(1, int(5 * 60 - elapsed) + 1),
                                self._refresh_cooldown_finished, refresh, calendar_info,
                            )
                    else:
                        refresh.set_tooltip_text(_("Refresh this calendar"))
                    refresh.connect("clicked", self._refresh_calendar, calendar_info)
                    row_widgets = [calendar_label, status, reminders, visibility, refresh]
                    sync_range = Gtk.Label(
                        label=(self._get_google_sync_range_label(calendar_info)
                               if calendar_info["provider"] == "google" else ""), xalign=0
                    )
                    sync_range.get_style_context().add_class("dim-label")
                    row_widgets.append(sync_range)
                    if not self._calendar_available(calendar_info):
                        for widget in row_widgets:
                            widget.set_opacity(0.5)
                    if self._refreshing or calendar_info.get("sync_range") == "too-big":
                        for widget in row_widgets:
                            widget.set_sensitive(False)
                    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
                    actions.set_halign(Gtk.Align.START)
                    edit = Gtk.Button.new_from_icon_name(
                        "xsi-document-edit-symbolic", Gtk.IconSize.MENU
                    )
                    edit.set_relief(Gtk.ReliefStyle.NONE)
                    edit.set_tooltip_text(_("Edit"))
                    edit.connect("clicked", self._edit_calendar, calendar_info, box,
                                 box.get_toplevel())
                    actions.pack_start(edit, False, False, 0)
                    if calendar_info["provider"] == "local":
                        remove = Gtk.Button.new_from_icon_name(
                            "xsi-edit-delete-symbolic", Gtk.IconSize.MENU
                        )
                        remove.set_relief(Gtk.ReliefStyle.NONE)
                        remove.set_tooltip_text(_("Remove"))
                        remove.connect("clicked", self._remove_local_calendar, calendar_info, box,
                                       box.get_toplevel())
                        actions.pack_start(remove, False, False, 0)
                    else:
                        actions.pack_start(refresh, False, False, 0)

                    table.attach(calendar_label, 0, row, 1, 1)
                    table.attach(sync_range, 1, row, 1, 1)
                    table.attach(status, 2, row, 1, 1)
                    table.attach(reminders, 3, row, 1, 1)
                    table.attach(visibility, 4, row, 1, 1)
                    table.attach(actions, 5, row, 1, 1)
                    row += 1
        box.pack_start(table, False, False, 0)
        box.show_all()

    def _refresh_cooldown_finished(self, button, calendar_info):
        if button.get_parent() is not None and not self._refreshing:
            button.set_sensitive(True)
            button.set_tooltip_text(_("Refresh this calendar"))
        return GLib.SOURCE_REMOVE

    def _get_sorted_calendars(self):
        calendars = self.store.list_calendars()
        local = [calendar_info for calendar_info in calendars if calendar_info["provider"] == "local"]
        google = [calendar_info for calendar_info in calendars if calendar_info["provider"] == "google"]
        account_order = {state["id"]: index for index, state in enumerate(
                         self.store.google.get_account_states())}
        google.sort(key=lambda calendar_info: (
            account_order.get(calendar_info.get("account_id"), len(account_order)),
            *self._get_google_calendar_sort_key(calendar_info),
        ))
        caldav = [calendar_info for calendar_info in calendars if calendar_info["provider"] == "caldav"]
        return local + google + caldav

    @staticmethod
    def _get_google_calendar_sort_key(calendar_info):
        return (not calendar_info.get("writable", False),
                not calendar_info.get("primary", calendar_info.get("id") == calendar_info.get("account_id")),
                calendar_info["name"].casefold())

    @staticmethod
    def _get_google_sync_range_label(calendar_info):
        return {
            "normal": _("2 years ahead"),
            "limited": _("1 year ahead"),
            "restricted": _("3 months ahead"),
            "too-big": _("Too many events"),
        }.get(calendar_info.get("sync_range", "normal"), _("2 years ahead"))

    @staticmethod
    def _get_calendar_sync_status_label(calendar_info):
        error = calendar_info.get("sync_error")
        if error:
            return _("Error: %s") % error
        if calendar_info.get("sync_range") == "too-big":
            return _("Not synchronized")
        last_sync = calendar_info.get("last_sync")
        if last_sync:
            elapsed = max(0, int(datetime.datetime.now().timestamp()) - int(last_sync))
            if elapsed < 60:
                relative = _("just now")
            elif elapsed < 60 * 60:
                minutes = elapsed // 60
                relative = (_("1 minute ago") if minutes == 1
                            else _("%d minutes ago") % minutes)
            elif elapsed < 24 * 60 * 60:
                hours = elapsed // (60 * 60)
                relative = (_("1 hour ago") if hours == 1
                            else _("%d hours ago") % hours)
            else:
                days = elapsed // (24 * 60 * 60)
                relative = (_("1 day ago") if days == 1
                            else _("%d days ago") % days)
            return _("Last sync: %s") % relative
        return _("Not synced yet")

    @staticmethod
    def _calendar_label(calendar_info):
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        swatch = Gtk.DrawingArea()
        swatch.set_size_request(12, 12)
        rgba = Gdk.RGBA()
        rgba.parse(calendar_info.get("color", DEFAULT_COLOR))
        swatch.connect("draw", draw_centered_circle, rgba)
        content.pack_start(swatch, False, False, 0)
        name = Gtk.Label(label=calendar_info["name"])
        name.set_xalign(0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        content.pack_start(name, True, True, 0)
        return content

    def _manage_calendars(self, _button):
        dialog = Gtk.Dialog(title=_("Calendars"), transient_for=self, modal=True)
        add_button = Gtk.MenuButton(label=_("Add a New Calendar…"))
        add_button.set_popover(self._calendar_type_popover(add_button, dialog))
        dialog.get_action_area().pack_start(add_button, False, False, 0)
        dialog.set_default_size(780, 560)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_border_width(12)
        calendar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._calendar_dialog_box = calendar_box
        scroll.add(calendar_box)
        dialog.get_content_area().pack_start(scroll, True, True, 0)
        self._fill_calendar_box(calendar_box)
        dialog.show_all()
        while True:
            response = dialog.run()
            if response == ADD_LOCAL_CALENDAR_RESPONSE:
                self._add_local_calendar(None, dialog)
                self._fill_calendar_box(calendar_box)
            elif response == CONNECT_GOOGLE_RESPONSE:
                self._calendar_dialog_box = None
                dialog.destroy()
                self._connect_google(None)
                return
            elif response == CONNECT_CALDAV_RESPONSE:
                self._calendar_dialog_box = None
                dialog.destroy()
                self._connect_caldav()
                return
            else:
                break
        self._calendar_dialog_box = None
        dialog.destroy()

    def _calendar_type_popover(self, relative_to, dialog):
        popover = Gtk.Popover.new(relative_to)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(6)
        choices = (
            (_("Local"), ADD_LOCAL_CALENDAR_RESPONSE),
            ("Google", CONNECT_GOOGLE_RESPONSE),
            (_("CalDAV (Nextcloud, Memotoo, etc.)"), CONNECT_CALDAV_RESPONSE),
        )
        for label, response in choices:
            button = Gtk.ModelButton(text=label)
            button.connect("clicked", self._calendar_type_selected,
                           popover, dialog, response)
            box.pack_start(button, False, False, 0)
        popover.add(box)
        box.show_all()
        return popover

    def _calendar_type_selected(self, _button, popover, dialog, response):
        popover.popdown()
        dialog.response(response)

    def _edit_calendar(self, _button, calendar_info, calendar_box, parent):
        is_local = calendar_info["provider"] == "local"
        dialog = Gtk.Dialog(title=_("Edit Calendar"), transient_for=parent, modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK)
        dialog.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("suggested-action")
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        name = Gtk.Entry()
        name.set_text(calendar_info["name"])
        name.set_sensitive(is_local)
        box.pack_start(Gtk.Label(label=_("Name"), xalign=0), False, False, 0)
        box.pack_start(name, False, False, 0)
        color = Gtk.ColorButton()
        rgba = Gdk.RGBA()
        rgba.parse(calendar_info.get("color", DEFAULT_COLOR))
        color.set_rgba(rgba)
        box.pack_start(Gtk.Label(label=_("Color"), xalign=0), False, False, 0)
        box.pack_start(color, False, False, 0)
        error = Gtk.Label(xalign=0, wrap=True, max_width_chars=40)
        error.set_no_show_all(True)
        box.pack_start(error, False, False, 0)
        box.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            if is_local and not name.get_text().strip():
                continue
            selected_color = color.get_rgba().to_string()
            try:
                if is_local:
                    self.store.update_local_calendar(
                        calendar_info["id"], name.get_text().strip(), selected_color,
                    )
                else:
                    self.store.set_remote_calendar_color(
                        calendar_info["provider"], calendar_info["id"], selected_color,
                        calendar_info["account_id"],
                    )
            except Exception as exc:
                error.set_text(str(exc))
                error.show()
                continue
            self._fill_calendar_box(calendar_box)
            self._update_views()
            notify_changed()
            break
        dialog.destroy()

    def _remove_local_calendar(self, _button, calendar_info, calendar_box, parent):
        if len(self.store.local.list_calendars()) <= 1:
            warning = Gtk.MessageDialog(
                transient_for=parent, message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK, text=_("At least one local calendar is required"),
            )
            warning.run()
            warning.destroy()
            return
        confirm = Gtk.MessageDialog(
            transient_for=parent, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL, text=_("Remove %s?") % calendar_info['name'],
        )
        confirm.add_button(_("Remove"), Gtk.ResponseType.OK)
        confirm.format_secondary_text(_("All the events from this calendar will be permanently removed."))
        response = confirm.run()
        confirm.destroy()
        if response == Gtk.ResponseType.OK:
            self.store.delete_local_calendar(calendar_info["id"])
            self._fill_calendar_box(calendar_box)
            self._update_views()
            notify_changed()

    def _set_status(self, message=""):
        self.status_label.set_text(message)
        self.status_label.set_visible(bool(message))

    def _calendar_switch_toggled(self, switch, _property, calendar_info):
        visible = switch.get_active()
        self.store.set_visible(calendar_info["provider"], calendar_info["id"], visible, calendar_info.get("account_id"))
        if calendar_info["provider"] != "local":
            # Remote calendars: Empty cache when hidden, refresh when shown
            if visible:
                self._set_status(_("Refresh requested for %s…") % calendar_info["name"])
                self._refresh_calendar_worker(calendar_info)
            else:
                self.store.clear_calendar_events(calendar_info["provider"], calendar_info["id"], calendar_info["account_id"])
        self._update_views()
        notify_changed()

    def _calendar_reminders_toggle(self, calendar_info):
        button = Gtk.ToggleButton()
        button.set_active(calendar_info.get("reminders", True))
        button.set_halign(Gtk.Align.CENTER)
        button.set_valign(Gtk.Align.CENTER)
        button.set_image(Gtk.Image.new_from_icon_name(
            "xsi-audio-volume-high-symbolic" if button.get_active()
            else "xsi-audio-volume-muted-symbolic",
            Gtk.IconSize.MENU,
        ))
        button.set_tooltip_text(_("Remind me about events in this calendar"))
        button.connect("toggled", self._calendar_reminders_toggled, calendar_info)
        return button

    def _calendar_reminders_toggled(self, button, calendar_info):
        enabled = button.get_active()
        button.set_image(Gtk.Image.new_from_icon_name(
            "xsi-audio-volume-high-symbolic" if enabled else "xsi-audio-volume-muted-symbolic",
            Gtk.IconSize.MENU,
        ))
        self.store.set_reminders(
            calendar_info["provider"], calendar_info["id"], enabled, calendar_info.get("account_id")
        )
        notify_changed()

    def _refresh_calendar(self, button, calendar_info):
        button.set_sensitive(False)
        button.set_tooltip_text(_("Refresh requested"))
        self._set_status(_("Refresh requested for %s…") % calendar_info["name"])
        self._refresh_calendar_worker(calendar_info, button)

    @run_async
    def _refresh_calendar_worker(self, calendar_info, button=None):
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            connection.call_sync(
                BUS_NAME, BUS_PATH, BUS_INTERFACE, "RefreshCalendar",
                GLib.Variant("(sss)", (
                    calendar_info["provider"], calendar_info.get("account_id", ""), calendar_info["id"]
                )),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            self._refresh_request_failed(str(exc), button)

    @run_idle
    def _refresh_request_failed(self, error, button):
        if button is not None and button.get_parent() is not None:
            button.set_sensitive(True)
            button.set_tooltip_text(_("Refresh this calendar"))
        self._set_status(_("Could not request refresh: %s") % error)

    def _add_local_calendar(self, _button, parent=None):
        dialog = Gtk.Dialog(title=_("New Calendar"), transient_for=parent or self, modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Create"), Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        name = Gtk.Entry()
        color = Gtk.ColorButton()
        rgba = Gdk.RGBA()
        rgba.parse(DEFAULT_COLOR)
        color.set_rgba(rgba)
        box.pack_start(Gtk.Label(label=_("Name"), xalign=0), False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(Gtk.Label(label=_("Color"), xalign=0), False, False, 0)
        box.pack_start(color, False, False, 0)
        box.show_all()
        if dialog.run() == Gtk.ResponseType.OK and name.get_text().strip():
            self.store.create_calendar(name.get_text().strip(), color.get_rgba().to_string())
            self._populate_calendar_list(self._get_available_events())
            notify_changed()
        dialog.destroy()

    def _connect_google(self, _button=None):
        auth_provider = self.settings.get_string("google-oauth-provider")
        if auth_provider == "goa":
            try:
                accounts = self.store.google.list_goa_accounts()
            except Exception as exc:
                self._google_connection_failed(str(exc))
                return
            if not accounts:
                self._google_connection_failed(
                    _("No Google accounts were found in Online Accounts.")
                )
                return
            account_id = accounts[0]["id"]
            if len(accounts) > 1:
                dialog = Gtk.Dialog(title=_("Google Account"), transient_for=self, modal=True)
                dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL,
                                   _("Connect"), Gtk.ResponseType.OK)
                box = dialog.get_content_area()
                box.set_spacing(8)
                box.set_border_width(12)
                box.pack_start(Gtk.Label(
                    label=_("Choose an account"), xalign=0
                ), False, False, 0)
                combo = Gtk.ComboBoxText()
                for account in accounts:
                    combo.append(account["id"], account["name"])
                combo.set_active(0)
                box.pack_start(combo, False, False, 0)
                dialog.show_all()
                response = dialog.run()
                account_id = combo.get_active_id()
                dialog.destroy()
                if response != Gtk.ResponseType.OK:
                    return
            self._set_refreshing(True)
            self._set_status(_("Connecting…"))
            self._connect_worker("goa", account_id)
            return
        self._set_refreshing(True)
        self._set_status(_("Connecting…"))
        self._connect_worker("clockenstein", None)

    @run_async
    def _connect_worker(self, auth_provider, goa_account_id):
        try:
            if auth_provider == "goa":
                account_id = self.store.google.connect_goa(
                    goa_account_id, self._connection_progress
                )
            else:
                account_id = self.store.google.connect(self._connection_progress)
            self._connection_progress(_("Requesting initial synchronization…"))
            self._call_daemon("RefreshAccount", GLib.Variant(
                "(ss)", ("google", account_id)
            ))
            self._sync_request_accepted()
        except Exception as exc:
            self._google_connection_failed(str(exc))

    @run_idle
    def _google_connection_failed(self, error):
        self._set_refreshing(False)
        self._set_status(error)
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=_("Could not connect the Google account"),
        )
        dialog.format_secondary_text(error)
        dialog.run()
        dialog.destroy()
        return False

    @run_idle
    def _caldav_connection_failed(self, error):
        self._set_refreshing(False)
        self._set_status(error)
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=_("Could not connect to the CalDAV server"),
        )
        dialog.format_secondary_text(error)
        dialog.run()
        dialog.destroy()
        return False

    @run_idle
    def _connection_progress(self, message):
        self._set_status(message)

    def _connect_caldav(self):
        dialog = Gtk.Dialog(title="CalDAV", transient_for=self, modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Connect"), Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        url = Gtk.Entry(placeholder_text="https://example.com/remote.php/dav/")
        username = Gtk.Entry()
        password = Gtk.Entry()
        password.set_visibility(False)
        password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        for label, entry in ((_("Server URL"), url), (_("Username"), username), (_("Password"), password)):
            box.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
            box.pack_start(entry, False, False, 0)
        box.show_all()
        response = dialog.run()
        values = (url.get_text().strip(), username.get_text().strip(), password.get_text())
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self._set_refreshing(True)
        self._set_status(_("Connecting…"))
        self._connect_caldav_worker(*values)

    @run_async
    def _connect_caldav_worker(self, url, username, password):
        try:
            account_id = self.store.caldav.connect(
                url, username, password, self._connection_progress
            )
        except Exception as exc:
            self._caldav_connection_failed(str(exc))
            return
        try:
            self._connection_progress(_("Requesting initial synchronization…"))
            self._call_daemon("RefreshAccount", GLib.Variant(
                "(ss)", ("caldav", account_id)
            ))
            self._sync_request_accepted()
        except Exception as exc:
            self._remote_done([str(exc)])

    def _disconnect_remote(self, button, provider, account_id):
        dialog = Gtk.MessageDialog(
            transient_for=self, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text=_("Disconnect %s?") % account_id,
        )
        dialog.format_secondary_text(
            _("This online calendar will be disconnected and removed from the application.")
        )
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            if provider == "google":
                self.store.google.disconnect(account_id)
            else:
                self.store.caldav.disconnect(account_id)
            notify_changed()
            self._refresh(refresh_remote=False)

    def _navigate(self, direction):
        d = self.current_date
        if self._active_view == "Month":
            self._month_week_offset = 0
            self._month_scroll_delta = 0
            month, year = d.month + direction, d.year
            if month < 1: month, year = 12, year - 1
            if month > 12: month, year = 1, year + 1
            self.current_date = d.replace(year=year, month=month, day=min(d.day, _get_month_days(year, month)))
        elif self._active_view == "Week":
            self.current_date += datetime.timedelta(weeks=direction)
        else:
            self.current_date += datetime.timedelta(days=direction)
        self._month_selected_date = self.current_date
        self._week_selected_date = self.current_date
        self._sync_mini_cal()
        self._refresh(refresh_caldav=self._caldav_navigation_needs_refresh())

    def _go_today(self):
        self.focus_date(self.today, refresh_caldav=False)

    def focus_date(self, date, refresh_remote=False, refresh_caldav=False):
        self.current_date = date
        self._month_selected_date = date
        self._week_selected_date = date
        self._month_week_offset = 0
        self._month_scroll_delta = 0
        self._sync_mini_cal()
        self._refresh(refresh_remote=refresh_remote, refresh_caldav=refresh_caldav)

    def _sync_mini_cal(self):
        self.mini_cal.set_date(self.current_date)

    def _on_mini_date_selected(self, date):
        self.current_date = date
        self._month_selected_date = date
        self._week_selected_date = date
        self._month_week_offset = 0
        self._month_scroll_delta = 0
        self._refresh(refresh_caldav=self._caldav_navigation_needs_refresh())

    def _scroll_month(self, direction):
        if self._active_view != "Month":
            return
        self._month_scroll_delta += direction
        steps = int(self._month_scroll_delta)
        if not steps:
            return
        self._month_scroll_delta -= steps
        start, _end = self._get_month_date_range()
        start += datetime.timedelta(weeks=steps)
        self._month_selected_date += datetime.timedelta(weeks=steps)
        self.current_date = self._month_selected_date
        self._set_month_grid_start(start)
        self._week_selected_date = self.current_date
        self.mini_cal.set_date(self._month_selected_date)
        self._refresh(refresh_caldav=self._caldav_navigation_needs_refresh())

    def _select_month_date(self, date):
        start, _end = self._get_month_date_range()
        self.current_date = date
        self._month_selected_date = date
        self._week_selected_date = date
        self._set_month_grid_start(start)
        self.mini_cal.set_date(date)
        self._refresh(refresh_remote=False)

    def _select_week_date(self, date):
        self.current_date = date
        self._month_selected_date = date
        self._week_selected_date = date
        self.mini_cal.set_date(date)
        self._refresh(refresh_remote=False)

    def _on_view_toggle(self, button, name):
        if not button.get_active():
            return
        for other_name, other in self.view_buttons.items():
            if other_name != name:
                other.handler_block_by_func(self._on_view_toggle)
                other.set_active(False)
                other.handler_unblock_by_func(self._on_view_toggle)
        self._active_view = name
        self.settings.set_string("calendar-default-view", name.lower())
        self.stack.set_visible_child_name(name)
        self._refresh(refresh_remote=False)

    def _save_window_size(self, _window):
        window = self.get_window()
        if not window or window.get_state() & Gdk.WindowState.MAXIMIZED:
            return
        width, height = self.get_size()
        if self.settings.get_int("calendar-window-width") != width:
            self.settings.set_int("calendar-window-width", width)
        if self.settings.get_int("calendar-window-height") != height:
            self.settings.set_int("calendar-window-height", height)

    def _on_event_activated(self, event):
        calendars = self._get_editable_calendars(event)
        editable = event.get("editable", True) and any(
            calendar_info["id"] == event["calendar_id"] for calendar_info in calendars
        )
        event = {**event, "editable": editable}
        if not editable:
            calendars = [event]
        dialog = EventDialog(self, store=self.store, event=event,
                             calendar_options=calendars, time_format=self.time_format)
        if dialog.run() in (Gtk.ResponseType.OK, Gtk.ResponseType.REJECT):
            notify_changed()
            self._refresh(refresh_remote=False)
        dialog.destroy()

    def _get_editable_calendars(self, event=None):
        calendars = [calendar_info for calendar_info in self.store.get_writable_calendars()
                     if calendar_info.get("visible", True)]
        if self._refreshing:
            calendars = [calendar_info for calendar_info in calendars if calendar_info["provider"] == "local"]
        if event is not None:
            calendars = [calendar_info for calendar_info in calendars
                         if calendar_info.get("provider") == event.get("provider")
                         and calendar_info.get("account_id") == event.get("account_id")]
        return calendars

    def _new_event(self, default_date=None):
        calendars = self._get_editable_calendars()
        if not calendars:
            return
        selected_date = (self._month_selected_date if self._active_view == "Month" else
                         self._week_selected_date if self._active_view == "Week" else
                         self.current_date)
        dialog = EventDialog(self, store=self.store, default_date=default_date or selected_date,
                             calendar_options=calendars, time_format=self.time_format)
        if dialog.run() == Gtk.ResponseType.OK:
            notify_changed()
            self._refresh(refresh_remote=False)
        dialog.destroy()

    def _on_key(self, _widget, event):
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval == Gdk.KEY_n and modifiers == Gdk.ModifierType.CONTROL_MASK:
            self._new_event()
        elif modifiers:
            return False
        elif event.keyval == Gdk.KEY_t:
            self._go_today()
        elif event.keyval == Gdk.KEY_Left:
            self._navigate(-1)
        elif event.keyval == Gdk.KEY_Right:
            self._navigate(1)
        else:
            return False
        return True

    def _get_date_range(self):
        d = self.current_date
        if self._active_view == "Month":
            return self._get_month_date_range()
        if self._active_view == "Week":
            start = start_of_week(d, self.first_weekday)
            return start, start + datetime.timedelta(days=6)
        return d, d

    def _get_month_date_range(self):
        first = datetime.date(self.current_date.year, self.current_date.month, 1)
        start = (start_of_week(first, self.first_weekday) +
                 datetime.timedelta(weeks=self._month_week_offset))
        return start, start + datetime.timedelta(days=41)

    def _set_month_grid_start(self, start):
        first = datetime.date(self.current_date.year, self.current_date.month, 1)
        base = start_of_week(first, self.first_weekday)
        self._month_week_offset = (start - base).days // 7

    def _caldav_navigation_needs_refresh(self):
        if not self.store.caldav.has_accounts:
            return False
        start, end = self._get_date_range()
        today = datetime.date.today()
        synced_start = today - datetime.timedelta(days=NORMAL_RANGE[0])
        synced_end = today + datetime.timedelta(days=NORMAL_RANGE[1])
        return start < synced_start or end > synced_end

    def _refresh(self, refresh_remote=False, refresh_caldav=False):
        self._update_views()
        if (refresh_remote or refresh_caldav) and not self._refreshing:
            self._set_refreshing(True)
            self._set_status(_("Connecting to online calendars…"))
            start, end = self._get_date_range()
            self._refresh_worker(start, end, refresh_remote)

    @run_async
    def _refresh_worker(self, start, end, refresh_all=True):
        try:
            if refresh_all:
                for provider in ("google", "caldav"):
                    self._call_daemon("RefreshAccount", GLib.Variant(
                        "(ss)", (provider, "")
                    ))
            else:
                since = int(datetime.datetime.combine(
                    start, datetime.time.min, self.timezone
                ).timestamp())
                until = int(datetime.datetime.combine(
                    end, datetime.time.max, self.timezone
                ).timestamp())
                self._call_daemon("RefreshRange", GLib.Variant(
                    "(sxx)", ("caldav", since, until)
                ))
            self._sync_request_accepted()
        except GLib.Error as exc:
            self._remote_done([str(exc)])

    @staticmethod
    def _call_daemon(method, parameters):
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync(
            BUS_NAME, BUS_PATH, BUS_INTERFACE, method, parameters,
            None, Gio.DBusCallFlags.NONE, -1, None,
        )

    @run_idle
    def _sync_request_accepted(self):
        self._set_refreshing(False)
        if self._daemon_refreshing:
            self._set_status(_("Synchronizing online calendars…"))
        else:
            self._set_status(_("Synchronization finished"))
        self.store = CalendarManager(self.timezone)
        self._update_views()

    @run_idle
    def _remote_done(self, errors):
        self._set_refreshing(False)
        self._update_views()
        if self._calendar_dialog_box is not None:
            self._fill_calendar_box(self._calendar_dialog_box)
        notify_changed()
        if errors:
            self._set_status(
                _("Some online calendars are disconnected (read-only).")
                + " " + "; ".join(errors)
            )
        return False

    def _set_refreshing(self, active):
        self._refresh_pending = active
        self._update_refreshing()

    def _update_refreshing(self):
        active = self._refresh_pending or self._daemon_refreshing
        changed = active != self._refreshing
        self._refreshing = active
        self.new_button.set_sensitive(bool(self._get_editable_calendars()))
        if active:
            self.spinner.show()
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.hide()

        if changed:
            self._update_views()
            if self._calendar_dialog_box is not None:
                self._fill_calendar_box(self._calendar_dialog_box)

    def _calendar_available(self, calendar):
        if calendar.get("provider") == "local":
            return True
        provider = calendar.get("provider")
        states = (self.store.google.get_account_states() if provider == "google" else
                  self.store.caldav.get_account_states())
        return (not self._refreshing and
                any(state["id"] == calendar.get("account_id") and state.get("online")
                    for state in states))

    def _get_available_events(self):
        events = self.store.get_events()
        if self._refreshing:
            return [event if event.get("provider") == "local" else
                    {**event, "editable": False} for event in events]
        return events

    def _update_views(self):
        self.new_button.set_sensitive(bool(self._get_editable_calendars()))
        start, end = self._get_date_range()
        all_events = self._get_available_events()
        events = [event for event in all_events
                  if event["date_end"] >= start and event["date_start"] <= end]
        self.month_view.update(self.current_date, events, self._month_week_offset,
                               self._month_selected_date)
        self.week_view.update(self.current_date, events, self._week_selected_date)
        self.day_view.update(self.current_date, events)
        self.mini_cal.set_events(all_events)
        self._populate_calendar_list(all_events)


def _get_month_days(year, month):
    return calendar.monthrange(year, month)[1]
