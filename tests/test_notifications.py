import datetime
import importlib.util
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clockenstein_daemon", ROOT / "daemon" / "daemon.py")
DAEMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DAEMON)
AGENT_SPEC = importlib.util.spec_from_file_location(
    "clockenstein_notification_agent", ROOT / "agent" / "agent.py"
)
AGENT = importlib.util.module_from_spec(AGENT_SPEC)
AGENT_SPEC.loader.exec_module(AGENT)


class NotificationSchedulerTests(unittest.TestCase):
    def test_refresh_setup_failure_releases_refresh_state(self):
        daemon = object.__new__(DAEMON.ClockensteinDaemon)
        daemon.timezone = ZoneInfo("UTC")
        daemon.logger = Mock()
        daemon.refreshing = True
        daemon.refresh_queue = []
        daemon._reload_reminder_events = Mock()
        daemon._emit_changed = Mock()
        with patch.object(DAEMON, "CalendarManager", side_effect=OSError("unreadable store")):
            worker = daemon._refresh_remote()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            context = DAEMON.GLib.MainContext.default()
            while context.pending():
                context.iteration(False)
        self.assertFalse(daemon.refreshing)
        daemon.logger.error.assert_called_once()

    def test_only_primary_event_presses_activate_and_are_consumed(self):
        from gi.repository import Gdk
        from views.month_view import _activate_event
        callback = Mock()
        event = {"uid": "event"}
        for button in (2, 3):
            self.assertFalse(_activate_event(None, SimpleNamespace(
                button=button, type=Gdk.EventType.BUTTON_PRESS), callback, event))
        callback.assert_not_called()
        self.assertTrue(_activate_event(None, SimpleNamespace(
            button=1, type=Gdk.EventType.BUTTON_PRESS), callback, event))
        self.assertTrue(_activate_event(None, SimpleNamespace(
            button=1, type=Gdk.EventType.DOUBLE_BUTTON_PRESS), callback, event))
        callback.assert_called_once_with(event)

    def test_reminder_uses_the_named_system_timezone(self):
        event = {"uid": "one", "date_start": datetime.date(2026, 9, 9),
                 "time_start": datetime.time(17, 45)}
        start = DAEMON._get_event_start(event, ZoneInfo("Europe/Dublin"))
        self.assertEqual(start.isoformat(), "2026-09-09T17:45:00+01:00")

    def test_midnight_to_midnight_event_is_exported_as_all_day(self):
        daemon = object.__new__(DAEMON.ClockensteinDaemon)
        daemon.timezone = ZoneInfo("UTC")
        event = {"uid": "one", "date_start": datetime.date(2026, 10, 13),
                 "date_end": datetime.date(2026, 10, 14), "all_day": False,
                 "time_start": datetime.time.min, "time_end": datetime.time.min, "modified": 1234}
        _uid, _color, _summary, all_day, start, end, modified = daemon._get_event_tuple(event)
        self.assertTrue(all_day)
        self.assertEqual(end - start, 24 * 60 * 60)
        self.assertEqual(modified, 1234)
        midnight = datetime.datetime.combine(event["date_start"], datetime.time.min, daemon.timezone)
        self.assertEqual(DAEMON._get_due_notifications(
            [event], midnight - datetime.timedelta(minutes=20), midnight, 15, daemon.timezone), [])

    def test_global_lead_time_applies_to_every_event(self):
        tz = datetime.datetime.now().astimezone().tzinfo
        event_start = datetime.datetime.now(tz).replace(second=0, microsecond=0) \
            + datetime.timedelta(hours=2)
        event = {
            "uid": "one",
            "summary": "Meeting",
            "date_start": event_start.date(),
            "time_start": event_start.time().replace(tzinfo=None),
        }
        due = DAEMON._get_due_notifications(
            [event], event_start - datetime.timedelta(minutes=11),
            event_start - datetime.timedelta(minutes=9), 10, tz
        )
        self.assertEqual(due, [event])

    def test_interval_boundary_prevents_duplicate_notifications(self):
        tz = datetime.datetime.now().astimezone().tzinfo
        event_start = datetime.datetime.now(tz).replace(second=0, microsecond=0) \
            + datetime.timedelta(hours=1)
        trigger = event_start - datetime.timedelta(minutes=15)
        event = {
            "uid": "one",
            "date_start": event_start.date(),
            "time_start": event_start.time().replace(tzinfo=None),
        }
        self.assertEqual(DAEMON._get_due_notifications(
            [event], trigger, trigger + datetime.timedelta(seconds=30), 15, tz), [])

    def test_relative_start_time_updates_after_event_begins(self):
        now = datetime.datetime.now().astimezone().replace(microsecond=0)
        start = int((now - datetime.timedelta(minutes=3)).timestamp())
        self.assertEqual(
            AGENT._get_relative_start_label(start, now),
            "This event started 3 minutes ago",
        )



if __name__ == "__main__":
    unittest.main()
