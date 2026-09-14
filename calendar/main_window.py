import datetime

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango
from xapp.threading import run_async, run_idle
from xapp.util import l10n

_ = l10n("clockenstein")

from event_dialog import EventDialog
from backends.google import LIMITED_RANGE, NORMAL_RANGE, RESTRICTED_RANGE
from dbus import BUS_INTERFACE, BUS_NAME, BUS_PATH, notify_changed
from formatting import capitalize_first, format_time, on_clock_format_changed
from store import CalendarManager
from views.colors import apply_tinted_event_color
from views.month_view import MonthView
from views.week_view import WeekView
from views.day_view import DayView
from widgets.mini_calendar import MiniCalendar


class MainWindow(Gtk.Window):
    def __init__(self, store: CalendarManager):
        super().__init__(title=_("Calendar"))
        self.store = store
        self.settings = Gio.Settings.new("org.x.clockenstein.calendar")
        width = self.settings.get_int("window-width")
        height = self.settings.get_int("window-height")
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
        saved_view = self.settings.get_string("default-view")
        view_names = {"month": "Month", "week": "Week", "day": "Day"}
        self._active_view = view_names.get(saved_view, "Month")
        self._refreshing = False
        self._build_ui()
        self._subscribe_to_daemon()
        on_clock_format_changed(self._update_views)
        geometry = Gdk.Geometry()
        geometry.min_width = 640
        geometry.min_height = 460
        self.set_geometry_hints(None, geometry, Gdk.WindowHints.MIN_SIZE)
        self._refresh(refresh_remote=False)

    def _subscribe_to_daemon(self):
        try:
            self._daemon_connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._daemon_subscription = self._daemon_connection.signal_subscribe(
                BUS_NAME, BUS_INTERFACE, "Changed", BUS_PATH, None,
                Gio.DBusSignalFlags.NONE, self._daemon_changed
            )
            self.connect("destroy", self._unsubscribe_from_daemon)
        except GLib.Error:
            self._daemon_connection = None
            self._daemon_subscription = 0

    def _unsubscribe_from_daemon(self, _window):
        if self._daemon_connection and self._daemon_subscription:
            self._daemon_connection.signal_unsubscribe(self._daemon_subscription)
            self._daemon_subscription = 0

    def _daemon_changed(self, _connection, _sender, _path, _interface,
                        _signal, _parameters):
        self.store = CalendarManager()
        self._refresh(refresh_remote=False)
        if self._calendar_dialog_box is not None:
            self._fill_calendar_box(self._calendar_dialog_box)

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox.connect("size-allocate", self._on_content_size_allocate)
        self.add(vbox)
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(_("Calendar"))
        self.set_titlebar(header)

        menu = Gtk.Menu()
        calendars_item = Gtk.MenuItem(label=_("Calendars…"))
        calendars_item.connect("activate", self._manage_calendars)
        menu.append(calendars_item)
        menu.append(Gtk.SeparatorMenuItem())
        about_item = Gtk.MenuItem(label=_("About"))
        about_item.connect("activate", self._show_about)
        menu.append(about_item)
        menu.show_all()
        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_button.set_tooltip_text(_("Main menu"))
        menu_button.set_popup(menu)
        header.pack_start(menu_button)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav.get_style_context().add_class("linked")
        for icon, callback in (("go-previous-symbolic", lambda _: self._navigate(-1)),
                               (None, lambda _: self._go_today()),
                               ("go-next-symbolic", lambda _: self._navigate(1))):
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

        new_button = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        new_button.set_tooltip_text(_("New event (Ctrl+N)"))
        new_button.connect("clicked", lambda _: self._new_event())
        header.pack_end(new_button)
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
        self.month_view = MonthView(self.today, self._on_event_activated, self._new_event,
                                    self._scroll_month, self._select_month_date)
        self.week_view = WeekView(self.today, self._on_event_activated, self._new_event,
                                  self._select_week_date)
        self.day_view = DayView(self.today, self._on_event_activated, self._new_event)
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
        self.mini_cal = MiniCalendar(self.current_date, self._on_mini_date_selected)
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

    def _populate_calendar_list(self):
        for child in self.visible_calendar_box.get_children():
            self.visible_calendar_box.remove(child)
        for cal in self._sorted_calendars():
            if cal.get("visible", True):
                label = self._calendar_label(cal)
                if not self._calendar_available(cal):
                    label.set_opacity(0.5)
                self.visible_calendar_box.pack_start(label, False, False, 0)
        self.visible_calendar_box.show_all()
        self._populate_upcoming()

        states = self.store.google.account_states() + self.store.caldav.account_states()
        offline = [s for s in states if not s.get("online")]
        if offline:
            names = ", ".join(s["name"] for s in offline)
            self._set_status(
                _("Some online calendars are disconnected (read-only).") + " " + names
            )
        else:
            self._set_status("")
        self._update_range_infobar()

    def _google_calendars_out_of_range(self):
        start, end = self._date_range()
        today = datetime.date.today()
        ranges = {
            "normal": NORMAL_RANGE,
            "limited": LIMITED_RANGE,
            "restricted": RESTRICTED_RANGE,
        }
        calendars = []
        for cal in self.store.google.list_calendars():
            if not cal.get("visible", True) or cal.get("sync_range") == "too-big":
                continue
            past_days, future_days = ranges.get(cal.get("sync_range", "normal"),
                                                NORMAL_RANGE)
            synced_start = today - datetime.timedelta(days=past_days)
            synced_end = today + datetime.timedelta(days=future_days)
            if start < synced_start or end > synced_end:
                calendars.append(cal["name"])
        return calendars

    def _update_range_infobar(self):
        calendars = self._google_calendars_out_of_range()
        if not calendars:
            self.range_infobar.hide()
            return
        names = ", ".join(calendars)
        self.range_infobar_label.set_text(
            _("This date is outside the sync range for: %s. Events may be missing.") % names
        )
        self.range_infobar.get_content_area().show_all()
        self.range_infobar.show()

    def _populate_upcoming(self):
        for child in self.upcoming_box.get_children():
            self.upcoming_box.remove(child)

        now = datetime.datetime.now()
        today = now.date()
        upcoming = []
        for event in self._available_events():
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
            rgba.parse(event.get("calendar_color", "#2aa198"))
            swatch.connect("draw", _draw_calendar_swatch, rgba)
            content.pack_start(swatch, False, False, 0)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label=event.get("summary") or _("Untitled"), xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("clockenstein-upcoming-title")
        parts = [capitalize_first(event["date_start"].strftime("%A %-d %b"))] if show_date else []
        if not event.get("all_day") and event.get("time_start"):
            parts.append(format_time(event["time_start"]))
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
        groups = [(_("Local"), "local", [c for c in calendars if c["provider"] == "local"])]
        account_keys = []
        for cal in calendars:
            key = (cal["provider"], cal.get("account_id"))
            if cal["provider"] != "local" and key not in account_keys:
                account_keys.append(key)
        states = {
            **{("google", s["id"]): s for s in self.store.google.account_states()},
            **{("caldav", s["id"]): s for s in self.store.caldav.account_states()},
        }
        for provider, account_id in account_keys:
            state = states.get((provider, account_id), {})
            label = state.get("name", account_id)
            status = _("Online") if state.get("online") else _("Offline, read only")
            frequency = (_("Sync every 2 hours") if provider == "google"
                         else _("Sync every 15 minutes"))
            label += f" — {status} — {frequency}"
            groups.append((label, (provider, account_id),
                           [c for c in calendars if c["provider"] == provider
                            and c.get("account_id") == account_id]))

        for heading, group_id, items in groups:
            group_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            label = Gtk.Label(label=heading)
            label.set_xalign(0)
            label.set_hexpand(True)
            label.get_style_context().add_class("clockenstein-section-label")
            group_header.pack_start(label, True, True, 0)
            if group_id != "local":
                disconnect = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
                disconnect.set_relief(Gtk.ReliefStyle.NONE)
                disconnect.set_tooltip_text(_("Disconnect %s") % group_id[1])
                disconnect.connect("clicked", self._disconnect_remote, group_id[0], group_id[1])
                group_header.pack_end(disconnect, False, False, 0)
            box.pack_start(group_header, False, False, 4)
            if group_id != "local":
                items = sorted(items, key=self._google_calendar_sort_key)
            previous_section = None
            calendar_grid = None
            grid_row = 0
            for cal in items:
                if group_id != "local":
                    section = _("My Calendars") if cal.get("writable", False) else _("Other Calendars")
                    if section != previous_section:
                        if cal["provider"] != "local":
                            calendar_grid = Gtk.Grid(column_spacing=16, row_spacing=4)
                            calendar_grid.set_hexpand(True)
                            calendar_grid.set_margin_start(16)
                            calendar_heading = Gtk.Label(label=section, xalign=0)
                            status_heading = Gtk.Label(label=_("Status"), xalign=0)
                            reminders_heading = Gtk.Label(label=_("Reminders"), xalign=0.5)
                            visible_heading = Gtk.Label(label=_("Visible"), xalign=0.5)
                            refresh_heading = Gtk.Label(label="", xalign=0.5)
                            calendar_heading.set_hexpand(True)
                            headings = [calendar_heading]
                            if cal["provider"] == "google":
                                range_heading = Gtk.Label(label=_("Sync range"), xalign=0)
                                headings.append(range_heading)
                            headings.extend((status_heading, reminders_heading,
                                             visible_heading, refresh_heading))
                            for heading_widget in headings:
                                heading_widget.get_style_context().add_class("dim-label")
                            calendar_grid.attach(calendar_heading, 0, 0, 1, 1)
                            column = 1
                            if cal["provider"] == "google":
                                calendar_grid.attach(range_heading, column, 0, 1, 1)
                                column += 1
                            calendar_grid.attach(status_heading, column, 0, 1, 1)
                            calendar_grid.attach(reminders_heading, column + 1, 0, 1, 1)
                            calendar_grid.attach(visible_heading, column + 2, 0, 1, 1)
                            calendar_grid.attach(refresh_heading, column + 3, 0, 1, 1)
                            box.pack_start(calendar_grid, False, False, 0)
                            grid_row = 1
                        else:
                            section_label = Gtk.Label(label=section)
                            section_label.set_xalign(0)
                            section_label.get_style_context().add_class(
                                "clockenstein-calendar-subsection"
                            )
                            section_label.set_margin_start(16)
                            box.pack_start(section_label, False, False, 2)
                        previous_section = section
                if cal["provider"] != "local":
                    calendar_label = self._calendar_label(cal)
                    status = Gtk.Label(label=self._calendar_sync_status_label(cal), xalign=0)
                    status.set_ellipsize(Pango.EllipsizeMode.END)
                    status.set_max_width_chars(32)
                    status.set_tooltip_text(self._calendar_sync_status_label(cal))
                    status.get_style_context().add_class("dim-label")
                    visibility = Gtk.Switch()
                    visibility.set_active(cal.get("visible", True))
                    visibility.set_halign(Gtk.Align.CENTER)
                    visibility.set_valign(Gtk.Align.CENTER)
                    visibility.set_tooltip_text(_("Show this calendar"))
                    visibility.connect("notify::active", self._calendar_switch_toggled, cal)
                    reminders = self._calendar_reminders_toggle(cal)
                    refresh = Gtk.Button.new_from_icon_name(
                        "view-refresh-symbolic", Gtk.IconSize.MENU
                    )
                    refresh.set_relief(Gtk.ReliefStyle.NONE)
                    elapsed = (datetime.datetime.now().timestamp() - int(cal["last_sync"])
                               if cal.get("last_sync") else None)
                    recently_synced = (elapsed is not None and not cal.get("sync_error")
                                       and elapsed < 5 * 60)
                    if recently_synced:
                        refresh.set_sensitive(False)
                        refresh.set_tooltip_text(
                            _("Already synchronized less than 5 minutes ago")
                        )
                        if cal.get("sync_range") != "too-big":
                            GLib.timeout_add_seconds(
                                max(1, int(5 * 60 - elapsed) + 1),
                                self._refresh_cooldown_finished, refresh, cal,
                            )
                    else:
                        refresh.set_tooltip_text(_("Refresh this calendar"))
                    refresh.connect("clicked", self._refresh_calendar, cal)
                    row_widgets = [calendar_label, status, reminders, visibility, refresh]
                    if cal["provider"] == "google":
                        sync_range = Gtk.Label(label=self._google_sync_range_label(cal), xalign=0)
                        sync_range.get_style_context().add_class("dim-label")
                        row_widgets.append(sync_range)
                    if not self._calendar_available(cal):
                        for widget in row_widgets:
                            widget.set_opacity(0.5)
                    if self._refreshing or cal.get("sync_range") == "too-big":
                        for widget in row_widgets:
                            widget.set_sensitive(False)
                    calendar_grid.attach(calendar_label, 0, grid_row, 1, 1)
                    column = 1
                    if cal["provider"] == "google":
                        calendar_grid.attach(sync_range, column, grid_row, 1, 1)
                        column += 1
                    calendar_grid.attach(status, column, grid_row, 1, 1)
                    calendar_grid.attach(reminders, column + 1, grid_row, 1, 1)
                    calendar_grid.attach(visibility, column + 2, grid_row, 1, 1)
                    calendar_grid.attach(refresh, column + 3, grid_row, 1, 1)
                    grid_row += 1
                    continue
                row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                row_box.set_margin_start(16)
                if cal["provider"] != "local" and not self._calendar_available(cal):
                    row_box.set_opacity(0.5)
                if cal["provider"] != "local" and self._refreshing:
                    row_box.set_sensitive(False)
                row_box.pack_start(self._calendar_label(cal), True, True, 0)
                visibility = Gtk.Switch()
                visibility.set_active(cal.get("visible", True))
                visibility.set_valign(Gtk.Align.CENTER)
                visibility.set_tooltip_text(_("Show this calendar"))
                visibility.connect("notify::active", self._calendar_switch_toggled, cal)
                row_box.pack_end(visibility, False, False, 0)
                reminders = self._calendar_reminders_toggle(cal)
                row_box.pack_end(reminders, False, False, 8)
                reminders_label = Gtk.Label(label=_("Reminders"))
                reminders_label.get_style_context().add_class("dim-label")
                row_box.pack_end(reminders_label, False, False, 0)
                if cal["provider"] == "local":
                    edit = Gtk.Button.new_from_icon_name("document-edit-symbolic", Gtk.IconSize.MENU)
                    edit.set_relief(Gtk.ReliefStyle.NONE)
                    edit.set_tooltip_text(_("Edit"))
                    edit.connect("clicked", self._edit_local_calendar, cal, box,
                                 box.get_toplevel())
                    row_box.pack_end(edit, False, False, 0)
                    remove = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.MENU)
                    remove.set_relief(Gtk.ReliefStyle.NONE)
                    remove.set_tooltip_text(_("Remove"))
                    remove.connect("clicked", self._remove_local_calendar, cal, box,
                                   box.get_toplevel())
                    row_box.pack_end(remove, False, False, 0)
                box.pack_start(row_box, False, False, 0)
        box.show_all()

    def _refresh_cooldown_finished(self, button, cal):
        if button.get_parent() is not None and not self._refreshing:
            button.set_sensitive(True)
            button.set_tooltip_text(_("Refresh this calendar"))
        return GLib.SOURCE_REMOVE

    def _sorted_calendars(self):
        calendars = self.store.list_calendars()
        local = [cal for cal in calendars if cal["provider"] == "local"]
        google = [cal for cal in calendars if cal["provider"] == "google"]
        account_order = {state["id"]: index for index, state in enumerate(
                         self.store.google.account_states())}
        google.sort(key=lambda cal: (
            account_order.get(cal.get("account_id"), len(account_order)),
            *self._google_calendar_sort_key(cal),
        ))
        caldav = [cal for cal in calendars if cal["provider"] == "caldav"]
        return local + google + caldav

    @staticmethod
    def _google_calendar_sort_key(cal):
        return (not cal.get("writable", False),
                not cal.get("primary", cal.get("id") == cal.get("account_id")),
                cal["name"].casefold())

    @staticmethod
    def _google_sync_range_label(cal):
        return {
            "normal": _("2 years ahead"),
            "limited": _("1 year ahead"),
            "restricted": _("3 months ahead"),
            "too-big": _("Too many events"),
        }.get(cal.get("sync_range", "normal"), _("2 years ahead"))

    @staticmethod
    def _calendar_sync_status_label(cal):
        error = cal.get("sync_error")
        if error:
            return _("Error: %s") % error
        if cal.get("sync_range") == "too-big":
            return _("Not synchronized")
        last_sync = cal.get("last_sync")
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
    def _calendar_label(cal):
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        swatch = Gtk.DrawingArea()
        swatch.set_size_request(12, 12)
        rgba = Gdk.RGBA()
        rgba.parse(cal.get("color", "#2aa198"))
        swatch.connect("draw", _draw_calendar_swatch, rgba)
        content.pack_start(swatch, False, False, 0)
        name = Gtk.Label(label=cal["name"])
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
            if response == 1:
                self._add_local_calendar(None, dialog)
                self._fill_calendar_box(calendar_box)
            elif response == 2:
                self._calendar_dialog_box = None
                dialog.destroy()
                self._connect_google(None)
                return
            elif response == 3:
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
            (_("Local"), 1),
            ("Google", 2),
            (_("CalDAV (Nextcloud, Memotoo, etc.)"), 3),
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

    def _edit_local_calendar(self, _button, cal, calendar_box, parent):
        dialog = Gtk.Dialog(title=_("Edit Calendar"), transient_for=parent, modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        name = Gtk.Entry()
        name.set_text(cal["name"])
        color = Gtk.ColorButton()
        rgba = Gdk.RGBA()
        rgba.parse(cal.get("color", "#2aa198"))
        color.set_rgba(rgba)
        box.pack_start(Gtk.Label(label=_("Name"), xalign=0), False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(Gtk.Label(label=_("Color"), xalign=0), False, False, 0)
        box.pack_start(color, False, False, 0)
        box.show_all()
        if dialog.run() == Gtk.ResponseType.OK and name.get_text().strip():
            self.store.update_local_calendar(
                cal["id"], name.get_text().strip(), color.get_rgba().to_string()
            )
            self._fill_calendar_box(calendar_box)
            self._populate_calendar_list()
            self._update_views()
            notify_changed()
        dialog.destroy()

    def _remove_local_calendar(self, _button, cal, calendar_box, parent):
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
            buttons=Gtk.ButtonsType.CANCEL, text=_("Remove %s?") % cal['name'],
        )
        confirm.add_button(_("Remove"), Gtk.ResponseType.OK)
        confirm.format_secondary_text(_("All the events from this calendar will be permanently removed."))
        response = confirm.run()
        confirm.destroy()
        if response == Gtk.ResponseType.OK:
            self.store.delete_local_calendar(cal["id"])
            self._fill_calendar_box(calendar_box)
            self._populate_calendar_list()
            self._update_views()
            notify_changed()

    def _set_status(self, message=""):
        self.status_label.set_text(message)
        self.status_label.set_visible(bool(message))

    def _calendar_switch_toggled(self, switch, _property, cal):
        self.store.set_visible(cal["provider"], cal["id"], switch.get_active(), cal.get("account_id"))
        self._populate_calendar_list()
        self._update_views()
        notify_changed()

    def _calendar_reminders_toggle(self, cal):
        button = Gtk.ToggleButton()
        button.set_active(cal.get("reminders", True))
        button.set_halign(Gtk.Align.CENTER)
        button.set_valign(Gtk.Align.CENTER)
        button.set_image(Gtk.Image.new_from_icon_name(
            "audio-volume-high-symbolic" if button.get_active()
            else "audio-volume-muted-symbolic",
            Gtk.IconSize.MENU,
        ))
        button.set_tooltip_text(_("Remind me about events in this calendar"))
        button.connect("toggled", self._calendar_reminders_toggled, cal)
        return button

    def _calendar_reminders_toggled(self, button, cal):
        enabled = button.get_active()
        button.set_image(Gtk.Image.new_from_icon_name(
            "audio-volume-high-symbolic" if enabled else "audio-volume-muted-symbolic",
            Gtk.IconSize.MENU,
        ))
        self.store.set_reminders(
            cal["provider"], cal["id"], enabled, cal.get("account_id")
        )
        notify_changed()

    def _refresh_calendar(self, button, cal):
        button.set_sensitive(False)
        button.set_tooltip_text(_("Refresh requested"))
        self._set_status(_("Refresh requested for %s…") % cal["name"])
        self._refresh_calendar_worker(cal, button)

    @run_async
    def _refresh_calendar_worker(self, cal, button):
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            connection.call_sync(
                BUS_NAME, BUS_PATH, BUS_INTERFACE, "RefreshCalendar",
                GLib.Variant("(sss)", (
                    cal["provider"], cal.get("account_id", ""), cal["id"]
                )),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            self._refresh_request_failed(str(exc), button)

    @run_idle
    def _refresh_request_failed(self, error, button):
        if button.get_parent() is not None:
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
        rgba.parse("#2aa198")
        color.set_rgba(rgba)
        box.pack_start(Gtk.Label(label=_("Name"), xalign=0), False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(Gtk.Label(label=_("Color"), xalign=0), False, False, 0)
        box.pack_start(color, False, False, 0)
        box.show_all()
        if dialog.run() == Gtk.ResponseType.OK and name.get_text().strip():
            self.store.create_calendar(name.get_text().strip(), color.get_rgba().to_string())
            self._populate_calendar_list()
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
    def _connection_progress(self, message):
        print(f"Clockenstein: {message}", flush=True)
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
            self.current_date = d.replace(year=year, month=month, day=min(d.day, _month_days(year, month)))
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
        start, _end = self._month_date_range()
        start += datetime.timedelta(weeks=steps)
        self._month_selected_date += datetime.timedelta(weeks=steps)
        self.current_date = self._month_selected_date
        self._set_month_grid_start(start)
        self._week_selected_date = self.current_date
        self.mini_cal.set_date(self._month_selected_date)
        self._refresh(refresh_caldav=self._caldav_navigation_needs_refresh())

    def _select_month_date(self, date):
        start, _end = self._month_date_range()
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
        self.settings.set_string("default-view", name.lower())
        self.stack.set_visible_child_name(name)
        self._refresh(refresh_remote=False)

    def _on_content_size_allocate(self, _widget, allocation):
        window = self.get_window()
        if window and not (window.get_state() & Gdk.WindowState.MAXIMIZED):
            self.settings.set_int("window-width", allocation.width)
            self.settings.set_int("window-height", allocation.height)

    def _on_event_activated(self, event):
        dialog = EventDialog(self, store=self.store, event=event)
        if dialog.run() in (Gtk.ResponseType.OK, Gtk.ResponseType.REJECT):
            notify_changed()
            self._refresh(refresh_remote=False)
        dialog.destroy()

    def _new_event(self, default_date=None):
        calendars = self.store.writable_calendars()
        if self._refreshing:
            calendars = [cal for cal in calendars if cal["provider"] == "local"]
        selected_date = (self._month_selected_date if self._active_view == "Month" else
                         self._week_selected_date if self._active_view == "Week" else
                         self.current_date)
        dialog = EventDialog(self, store=self.store, default_date=default_date or selected_date,
                             calendar_options=calendars)
        if dialog.run() == Gtk.ResponseType.OK:
            notify_changed()
            self._refresh(refresh_remote=False)
        dialog.destroy()

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_n and event.state & Gdk.ModifierType.CONTROL_MASK: self._new_event()
        elif event.keyval == Gdk.KEY_t: self._go_today()
        elif event.keyval == Gdk.KEY_Left: self._navigate(-1)
        elif event.keyval == Gdk.KEY_Right: self._navigate(1)

    def _date_range(self):
        d = self.current_date
        if self._active_view == "Month":
            return self._month_date_range()
        if self._active_view == "Week":
            start = d - datetime.timedelta(days=d.weekday())
            return start, start + datetime.timedelta(days=6)
        return d, d

    def _month_date_range(self):
        first = datetime.date(self.current_date.year, self.current_date.month, 1)
        start = (first - datetime.timedelta(days=first.weekday()) +
                 datetime.timedelta(weeks=self._month_week_offset))
        return start, start + datetime.timedelta(days=41)

    def _set_month_grid_start(self, start):
        first = datetime.date(self.current_date.year, self.current_date.month, 1)
        base = first - datetime.timedelta(days=first.weekday())
        self._month_week_offset = (start - base).days // 7

    def _caldav_navigation_needs_refresh(self):
        if not self.store.caldav.has_accounts:
            return False
        start, end = self._date_range()
        today = datetime.date.today()
        synced_start = today - datetime.timedelta(days=NORMAL_RANGE[0])
        synced_end = today + datetime.timedelta(days=NORMAL_RANGE[1])
        return start < synced_start or end > synced_end

    def _refresh(self, refresh_remote=False, refresh_caldav=False):
        self._update_views()
        self._populate_calendar_list()
        if (refresh_remote or refresh_caldav) and not self._refreshing:
            self._set_refreshing(True)
            self._set_status(_("Connecting to online calendars…"))
            start, end = self._date_range()
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
                    start, datetime.time.min
                ).astimezone().timestamp())
                until = int(datetime.datetime.combine(
                    end, datetime.time.max
                ).astimezone().timestamp())
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
        self._set_status(_("Synchronization requested"))
        self.store = CalendarManager()
        self._update_views()
        self._populate_calendar_list()

    @run_idle
    def _remote_done(self, errors):
        self._set_refreshing(False)
        self._update_views()
        self._populate_calendar_list()
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
        changed = active != self._refreshing
        self._refreshing = active
        if active:
            self.spinner.show()
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.hide()

        if active and changed:
            self._update_views()
            self._populate_calendar_list()

    def _calendar_available(self, calendar):
        if calendar.get("provider") == "local":
            return True
        provider = calendar.get("provider")
        states = (self.store.google.account_states() if provider == "google" else
                  self.store.caldav.account_states())
        return (not self._refreshing and
                any(state["id"] == calendar.get("account_id") and state.get("online")
                    for state in states))

    def _available_events(self, start=None, end=None):
        events = self.store.get_events(start, end)
        if self._refreshing:
            return [event if event.get("provider") == "local" else
                    {**event, "editable": False} for event in events]
        return events

    def _update_views(self):
        start, end = self._date_range()
        events = self._available_events(start, end)
        self.month_view.update(self.current_date, events, self._month_week_offset,
                               self._month_selected_date)
        self.week_view.update(self.current_date, events, self._week_selected_date)
        self.day_view.update(self.current_date, events)
        self.mini_cal.set_events(self._available_events())


def _month_days(year, month):
    import calendar
    return calendar.monthrange(year, month)[1]


def _draw_calendar_swatch(widget, cr, rgba):
    width = widget.get_allocated_width()
    height = widget.get_allocated_height()
    radius = min(width, height) / 2
    cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)
    cr.arc(width / 2, height / 2, radius, 0, 2 * 3.14159265)
    cr.fill()
    return False
