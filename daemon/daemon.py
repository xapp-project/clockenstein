#!/usr/bin/python3
import datetime
import os
import signal
import sys

import gi
from setproctitle import setproctitle

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
from xapp.threading import run_async, run_idle

# Calendar modules remain shared with the graphical calendar application.
CALENDAR_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "calendar")
sys.path.insert(0, CALENDAR_DIR)
sys.path.insert(0, os.path.dirname(__file__))

from clockenstein import BUS_INTERFACE, BUS_NAME, BUS_PATH, DEFAULT_COLOR, SETTINGS_SCHEMA
from clockenstein.alarms import AlarmStore, due_alarms
from clockenstein.logging import Logger
from backends.google import LIMITED_RANGE, NORMAL_RANGE, RESTRICTED_RANGE
from store import CalendarManager, local_timezone, watch_timezone_changes

CALDAV_REFRESH_INTERVAL_SECONDS = 15 * 60
GOOGLE_REFRESH_INTERVAL_SECONDS = 2 * 60 * 60
GOOGLE_REFRESH_EVERY = GOOGLE_REFRESH_INTERVAL_SECONDS // CALDAV_REFRESH_INTERVAL_SECONDS
REMINDER_CHECK_INTERVAL_SECONDS = 30
REMINDER_MINUTES_KEY = "reminder-minutes"
VERSION = "__PROJECT_VERSION__"

INTERFACE_XML = f"""
<node>
  <interface name="{BUS_INTERFACE}">
    <method name="GetEvents">
      <arg type="x" name="since" direction="in"/>
      <arg type="x" name="until" direction="in"/>
      <arg type="a(sssbxxx)" name="events" direction="out"/>
    </method>
    <method name="NotifyChanged"/>
    <method name="GetSyncState">
      <arg type="b" name="refreshing" direction="out"/>
    </method>
    <method name="RefreshCalendar">
      <arg type="s" name="provider" direction="in"/>
      <arg type="s" name="account_id" direction="in"/>
      <arg type="s" name="calendar_id" direction="in"/>
    </method>
    <method name="RefreshAccount">
      <arg type="s" name="provider" direction="in"/>
      <arg type="s" name="account_id" direction="in"/>
    </method>
    <method name="RefreshRange">
      <arg type="s" name="provider" direction="in"/>
      <arg type="x" name="since" direction="in"/>
      <arg type="x" name="until" direction="in"/>
    </method>
    <method name="NotifyAlarmsChanged"/>
    <signal name="Changed"/>
    <signal name="SyncStateChanged">
      <arg type="b" name="refreshing"/>
    </signal>
    <signal name="AlarmsChanged"/>
    <signal name="Reminder">
      <arg type="s" name="uid"/>
      <arg type="s" name="summary"/>
      <arg type="s" name="location"/>
      <arg type="s" name="description"/>
      <arg type="s" name="calendar_name"/>
      <arg type="s" name="calendar_color"/>
      <arg type="x" name="start"/>
      <arg type="b" name="all_day"/>
    </signal>
    <signal name="Alarm">
      <arg type="s" name="id"/>
      <arg type="s" name="label"/>
      <arg type="x" name="trigger"/>
      <arg type="s" name="sound"/>
      <arg type="u" name="sound_interval"/>
    </signal>
  </interface>
</node>
"""


class ClockensteinDaemon:
    def __init__(self):
        self.refresh_timezone()
        self.settings = Gio.Settings.new(SETTINGS_SCHEMA)
        self.logger = Logger(self.settings, "clockenstein-daemon")
        self.connection = None
        self.registration_id = 0
        self.refreshing = False
        self.refresh_ticks = 0
        self.google_refresh_due = False
        self.refresh_queue = []
        self.last_reminder_check = None
        self.reminder_events = []
        self.alarms = AlarmStore()
        self.alarm_records = self.alarms.list()
        self.loop = GLib.MainLoop()
        self.node_info = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        # Keep the monitor alive; otherwise it may be garbage-collected.
        self.timezone_monitor = watch_timezone_changes(self._timezone_changed)

    def _timezone_changed(self):
        self.refresh_timezone()
        self.last_reminder_check = datetime.datetime.now(self.timezone)
        self._reload_reminder_events()
        self._emit_changed()

    def refresh_timezone(self):
        self.timezone = local_timezone()

    def run(self):
        print(f"clockenstein-daemon: Starting version {VERSION}", flush=True)
        self.logger.log(f"Requesting {BUS_NAME}")
        Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._bus_acquired,
            self._name_acquired,
            self._name_lost,
        )
        signal.signal(signal.SIGINT, lambda _signum, _frame: self.loop.quit())
        signal.signal(signal.SIGTERM, lambda _signum, _frame: self.loop.quit())
        self.loop.run()
        self.logger.log("Stopped")

    def _bus_acquired(self, connection, _name):
        self.logger.log("Connected to the session bus")
        self.connection = connection
        register_object = getattr(connection, "register_object_with_closures2",
                                  connection.register_object)
        self.registration_id = register_object(
            BUS_PATH,
            self.node_info.interfaces[0],
            self._handle_method_call,
            None,
            None,
        )

    def _name_acquired(self, _connection, _name):
        self.logger.log(f"Acquired {BUS_NAME}")
        self.last_reminder_check = datetime.datetime.now(self.timezone)
        self._reload_reminder_events()
        GLib.timeout_add_seconds(
            REMINDER_CHECK_INTERVAL_SECONDS, self._reminder_timeout
        )
        GLib.timeout_add_seconds(CALDAV_REFRESH_INTERVAL_SECONDS, self._refresh_timeout)
        self._request_refresh(refresh_google=True, refresh_caldav=True)

    def _name_lost(self, _connection, _name):
        self.logger.warning(
            f"Could not own {BUS_NAME}; another instance may be running"
        )
        self.loop.quit()

    def _handle_method_call(self, _connection, _sender, _path, _interface,
                            method, parameters, invocation):
        if method == "GetEvents":
            since, until = parameters.unpack()
            events = self._get_events_for_range(since, until)
            self.logger.log(f"GetEvents({since}, {until}) -> {len(events)} event(s)")
            invocation.return_value(GLib.Variant("(a(sssbxxx))", (events,)))
        elif method == "GetSyncState":
            invocation.return_value(GLib.Variant("(b)", (self.refreshing,)))
        elif method == "NotifyChanged":
            self.logger.log("NotifyChanged()")
            self._reload_reminder_events()
            self._emit_changed()
            invocation.return_value(None)
        elif method == "RefreshCalendar":
            provider, account_id, calendar_id = parameters.unpack()
            if provider not in ("google", "caldav"):
                invocation.return_dbus_error(
                    f"{BUS_INTERFACE}.InvalidProvider", "Unsupported calendar provider"
                )
                return
            self.logger.log(f"RefreshCalendar({provider}, {account_id}, {calendar_id})")
            self._request_refresh(
                refresh_google=provider == "google",
                refresh_caldav=provider == "caldav",
                target=(provider, account_id, calendar_id),
            )
            invocation.return_value(None)
        elif method == "RefreshAccount":
            provider, account_id = parameters.unpack()
            if provider not in ("google", "caldav"):
                invocation.return_dbus_error(
                    f"{BUS_INTERFACE}.InvalidProvider", "Unsupported calendar provider"
                )
                return
            self.logger.log(f"RefreshAccount({provider}, {account_id})")
            self._request_refresh(
                refresh_google=provider == "google",
                refresh_caldav=provider == "caldav",
                target=(provider, account_id, None),
            )
            invocation.return_value(None)
        elif method == "RefreshRange":
            provider, since, until = parameters.unpack()
            if provider != "caldav":
                invocation.return_dbus_error(
                    f"{BUS_INTERFACE}.InvalidProvider",
                    "Date-range refreshes are only supported for CalDAV",
                )
                return
            date_range = (datetime.datetime.fromtimestamp(since, self.timezone).date(),
                          datetime.datetime.fromtimestamp(until, self.timezone).date())
            self.logger.log(f"RefreshRange({provider}, {date_range[0]}, {date_range[1]})")
            self._request_refresh(refresh_google=False, refresh_caldav=True,
                                  date_range=date_range)
            invocation.return_value(None)
        elif method == "NotifyAlarmsChanged":
            self._reload_alarms()
            self._emit_alarms_changed()
            invocation.return_value(None)

    def _get_events_for_range(self, since, until):
        start = datetime.datetime.fromtimestamp(since, self.timezone).date()
        end = datetime.datetime.fromtimestamp(until, self.timezone).date()
        store = CalendarManager(self.timezone)
        return [self._get_event_tuple(event) for event in store.get_events(start, end)]

    def _reload_alarms(self):
        self.alarm_records = self.alarms.list()

    def _get_event_tuple(self, event):
        all_day = bool(event.get("all_day"))
        start_time = event.get("time_start") or datetime.time.min
        start = datetime.datetime.combine(event["date_start"], start_time, self.timezone)
        if all_day:
            end_date = event.get("date_end", event["date_start"]) + datetime.timedelta(days=1)
            end = datetime.datetime.combine(end_date, datetime.time.min, self.timezone)
        else:
            end_time = event.get("time_end") or start_time
            end = datetime.datetime.combine(
                event.get("date_end", event["date_start"]), end_time, self.timezone
            )
        uid = ":".join((event.get("provider", "local"),
                        event.get("account_id", "local"),
                        event.get("calendar_id", ""), event["uid"]))
        return (uid, event.get("calendar_color", DEFAULT_COLOR),
                event.get("summary", ""), all_day,
                int(start.timestamp()), int(end.timestamp()), 0)

    def _refresh_timeout(self):
        self.refresh_ticks += 1
        if self.refresh_ticks % GOOGLE_REFRESH_EVERY == 0:
            self.google_refresh_due = True
        self._request_refresh(refresh_google=self.google_refresh_due,
                              refresh_caldav=True)
        return GLib.SOURCE_CONTINUE

    def _reminder_timeout(self):
        now = datetime.datetime.now(self.timezone)
        since = self.last_reminder_check or now
        self.last_reminder_check = now
        try:
            minutes = self.settings.get_uint(REMINDER_MINUTES_KEY)
            events = [event for event in self.reminder_events
                      if event.get("reminders", True)]
            for event in _get_due_notifications(events, since, now, minutes, self.timezone):
                self._emit_reminder(event)
            for alarm, trigger in due_alarms(self.alarm_records, since, now, self.timezone):
                if self.alarms.mark_fired(alarm) is None:
                    continue  # The alarm was deleted since the last reload.
                self._reload_alarms()
                self._emit_alarm(alarm, trigger)
                self._emit_alarms_changed()
        except Exception as exc:
            self.logger.error(f"Could not check reminders and alarms: {exc}")
        return GLib.SOURCE_CONTINUE

    def _reload_reminder_events(self):
        try:
            self.reminder_events = CalendarManager(self.timezone).get_events(include_hidden=True)
        except Exception as exc:
            self.logger.error(f"Could not reload reminders: {exc}")

    def _emit_reminder(self, event):
        if not self.connection:
            return
        uid = ":".join((event.get("provider", "local"),
                        event.get("account_id", "local"),
                        event.get("calendar_id", ""), event["uid"]))
        parameters = GLib.Variant(
            "(ssssssxb)",
            (uid, event.get("summary", ""), event.get("location", ""),
             event.get("description", ""),
             event.get("calendar_name", ""),
             event.get("calendar_color", DEFAULT_COLOR),
             int(_get_event_start(event, self.timezone).timestamp()), bool(event.get("all_day"))),
        )
        self.logger.log(f"Emitting Reminder for {uid}")
        self.connection.emit_signal(
            None, BUS_PATH, BUS_INTERFACE, "Reminder", parameters
        )

    def _emit_alarm(self, alarm, trigger):
        if not self.connection:
            return
        label = alarm.get("label") or "Alarm"
        self.logger.log(f"Emitting Alarm for {alarm['id']}")
        self.connection.emit_signal(
            None, BUS_PATH, BUS_INTERFACE, "Alarm",
            GLib.Variant(
                "(ssxsu)",
                (alarm["id"], label, int(trigger.timestamp()),
                 alarm.get("sound", "") if alarm.get("sound_enabled", True) else "",
                 alarm.get("sound_interval", 3)),
            ),
        )

    def _request_refresh(self, refresh_google=False, refresh_caldav=True,
                         target=None, date_range=None):
        if self.refreshing:
            request = (refresh_google, refresh_caldav, target, date_range)
            if request not in self.refresh_queue:
                self.refresh_queue.append(request)
            self.logger.log("Queued refresh because one is already running")
            return
        self.refreshing = True
        self._emit_sync_state()
        if refresh_google:
            self.google_refresh_due = False
        self._refresh_remote(refresh_google, refresh_caldav, target, date_range)

    @run_async
    def _refresh_remote(self, refresh_google=False, refresh_caldav=True,
                        target=None, date_range=None):
        try:
            store = CalendarManager(self.timezone)
            if not store.has_remote_accounts:
                self.logger.log("No remote accounts to refresh")
                return
            today = datetime.date.today()
            start = (date_range[0] if date_range else
                     today - datetime.timedelta(days=NORMAL_RANGE[0]))
            end = (date_range[1] if date_range else
                   today + datetime.timedelta(days=NORMAL_RANGE[1]))
            limited_start = today - datetime.timedelta(days=LIMITED_RANGE[0])
            limited_end = today + datetime.timedelta(days=LIMITED_RANGE[1])
            restricted_start = today - datetime.timedelta(days=RESTRICTED_RANGE[0])
            restricted_end = today + datetime.timedelta(days=RESTRICTED_RANGE[1])
            providers = (target[0] if target else
                         "Google and CalDAV" if refresh_google and refresh_caldav
                         else "Google" if refresh_google else "CalDAV")
            self.logger.log(f"Refreshing {providers} calendars from {start} through {end}")
            errors = []
            if refresh_google:
                errors.extend(store.google.refresh(
                    start, end,
                    limited_range=(limited_start, limited_end),
                    restricted_range=(restricted_start, restricted_end),
                    target_account_id=target[1] if target else None,
                    target_calendar_id=target[2] if target else None,
                ))
            if refresh_caldav:
                errors.extend(store.caldav.refresh(
                    start, end,
                    target_account_id=target[1] if target else None,
                    target_calendar_id=target[2] if target else None,
                ))
            stats = store.google.last_refresh_stats
            if refresh_google and stats.get("accounts"):
                self.logger.log(
                    "Google refresh: "
                    f"page size {stats['page_size']}, "
                    f"{stats['calendars']} calendar(s), "
                    f"{stats['limited_calendars']} limited, "
                    f"{stats['restricted_calendars']} restricted, "
                    f"{stats['too_big_calendars']} too big, "
                    f"{stats['calendar_list_requests']} calendar-list request(s), "
                    f"{stats['event_list_requests']} event-list request(s), "
                    f"{stats['events']} event(s)"
                )
            if errors:
                self.logger.warning("Refresh completed with errors: " + "; ".join(errors))
            else:
                self.logger.log("Refresh completed")
        except Exception as exc:
            self.logger.error(f"Could not refresh calendars: {exc}")
        finally:
            self._refresh_finished()

    @run_idle
    def _refresh_finished(self):
        self.refreshing = False
        self._reload_reminder_events()
        self._emit_changed()
        if self.refresh_queue:
            refresh_google, refresh_caldav, target, date_range = self.refresh_queue.pop(0)
            self._request_refresh(refresh_google, refresh_caldav, target, date_range)
        else:
            self._emit_sync_state()

    def _emit_sync_state(self):
        if self.connection:
            self.connection.emit_signal(
                None, BUS_PATH, BUS_INTERFACE, "SyncStateChanged",
                GLib.Variant("(b)", (self.refreshing,)),
            )

    def _emit_changed(self):
        if self.connection:
            self.logger.log("Emitting Changed")
            self.connection.emit_signal(None, BUS_PATH, BUS_INTERFACE, "Changed", None)

    def _emit_alarms_changed(self):
        if self.connection:
            self.connection.emit_signal(None, BUS_PATH, BUS_INTERFACE, "AlarmsChanged", None)

def _get_event_start(event, timezone):
    return datetime.datetime.combine(
        event["date_start"], event.get("time_start") or datetime.time.min, timezone
    )


def _get_due_notifications(events, since, until, minutes, timezone):
    """Return events whose universal notification became due in the interval."""
    if until < since:
        return []
    due = []
    for event in events:
        if event.get("all_day"):
            continue
        start = _get_event_start(event, timezone)
        trigger = start - datetime.timedelta(minutes=minutes)
        if since < trigger <= until:
            due.append(event)
    return sorted(due, key=lambda event: _get_event_start(event, timezone))


if __name__ == "__main__":
    setproctitle("clockenstein-daemon")
    ClockensteinDaemon().run()
