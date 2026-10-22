import datetime
import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "calendar"))

from clockenstein.calendars import SCHEMA_VERSION, CalendarDatabase
from clockenstein.misc import get_event_last_day
from backends.google import GoogleBackend
from backends.caldav import CalDAVBackend, _get_event_ical
from store import LocalStore


UTC = ZoneInfo("UTC")
START = datetime.date(2026, 9, 1)
END = datetime.date(2026, 9, 30)


def event(uid="event", summary="Lunch"):
    return {"uid": uid, "summary": summary, "all_day": True,
            "date_start": START, "date_end": START}


def concurrent_writer(directory, index, ready):
    store = LocalStore(UTC, directory)
    ready.wait(timeout=10)
    for number in range(15):
        store.create_event(event(f"{index}-{number}"))


class CalendarDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.client = CalendarDatabase(self.path)
        self.client.connect_account("google", {"id": "account", "name": "Account"},
                                    [{"id": "work", "name": "Work"}, {"id": "home"}])
        self.daemon = CalendarDatabase(self.path)

    def test_existing_database_gains_modified_column(self):
        self.client.save_event("google", "account", "work", event("old"), UTC)
        connection = sqlite3.connect(self.client.path)
        connection.execute("ALTER TABLE events DROP COLUMN modified")
        connection.execute("PRAGMA user_version = 0")
        connection.close()
        database = CalendarDatabase(self.path)
        database.save_event("google", "account", "work", event("new"), UTC)
        database.save_event("google", "account", "work", {**event("synced"), "modified": 1234}, UTC)
        events = {e["uid"]: e for e in database.get_events("google", UTC)}
        self.assertEqual(events["old"]["modified"], 0)
        self.assertGreater(events["new"]["modified"], 0)
        self.assertEqual(events["synced"]["modified"], 1234)

    def test_newer_schema_version_is_kept(self):
        connection = sqlite3.connect(self.client.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        CalendarDatabase(self.path)
        connection = sqlite3.connect(self.client.path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)
        connection.close()

    def test_sync_preserves_preferences_and_other_accounts(self):
        snapshot = self.daemon.get_calendars("google", "account")[0]
        self.client.update_calendar("google", "account", "work", reminders=False)
        self.client.update_calendar("google", "account", "home", visible=False)
        self.client.connect_account("caldav", {"id": "other", "name": "Other"}, [{"id": "work"}])
        self.assertTrue(self.daemon.apply_sync(snapshot, [event()], START, END, UTC))
        work, home = self.client.get_calendars("google")
        self.assertFalse(work["reminders"])
        self.assertFalse(home["visible"])
        self.assertEqual(self.client.get_accounts("caldav")[0]["id"], "other")
        self.assertEqual(self.client.get_events("google", UTC)[0]["summary"], "Lunch")

    def test_hide_and_clear_during_sync_stays_empty_after_showing(self):
        self.client.save_event("google", "account", "work", event(), UTC)
        snapshot = self.daemon.get_calendars("google", "account")[0]
        self.client.update_calendar("google", "account", "work", visible=False)
        self.client.clear_events("google", "account", "work")
        self.assertFalse(self.daemon.apply_sync(snapshot, [event()], START, END, UTC))
        self.client.update_calendar("google", "account", "work", visible=True)
        self.assertEqual(self.client.get_events("google", UTC), [])
        fresh = self.daemon.get_calendars("google", "account")[0]
        self.assertTrue(self.daemon.apply_sync(fresh, [event()], START, END, UTC))

    def test_disconnect_and_reconnect_cannot_be_undone_by_old_sync(self):
        snapshot = self.daemon.get_calendars("google", "account")[0]
        self.client.disconnect_account("google", "account")
        self.assertFalse(self.daemon.apply_sync(snapshot, [event()], START, END, UTC))
        self.client.connect_account("google", {"id": "account", "name": "Reconnected"}, [{"id": "work"}])
        self.assertFalse(self.daemon.apply_sync(snapshot, [event()], START, END, UTC))
        self.assertEqual(self.client.get_events("google", UTC), [])

    def test_edit_and_delete_cannot_be_overwritten_by_earlier_download(self):
        snapshot = self.daemon.get_calendars("google", "account")[0]
        self.client.save_event("google", "account", "work", event(summary="Changed"), UTC)
        self.assertFalse(self.daemon.apply_sync(snapshot, [event()], START, END, UTC))
        self.assertEqual(self.client.get_events("google", UTC)[0]["summary"], "Changed")
        snapshot = self.daemon.get_calendars("google", "account")[0]
        self.client.delete_event("google", "account", "work", "event")
        self.assertFalse(self.daemon.apply_sync(snapshot, [event()], START, END, UTC))
        self.assertEqual(self.client.get_events("google", UTC), [])

    def test_refresh_is_scoped_to_calendar_and_range(self):
        future = {**event("future"), "date_start": datetime.date(2030, 1, 1),
                  "date_end": datetime.date(2030, 1, 1)}
        self.client.save_event("google", "account", "work", event(), UTC)
        self.client.save_event("google", "account", "work", future, UTC)
        self.client.save_event("google", "account", "home", event("home-event"), UTC)
        snapshot = self.daemon.get_calendars("google", "account")[0]
        self.daemon.apply_sync(snapshot, [], START, END, UTC)
        self.assertEqual({e["uid"] for e in self.client.get_events("google", UTC)}, {"future", "home-event"})

    def test_failed_sync_transaction_keeps_events_and_sync_status(self):
        self.client.save_event("google", "account", "work", event(), UTC)
        snapshot = self.daemon.get_calendars("google", "account")[0]
        invalid = {**event("bad"), "summary": None}
        with self.assertRaises(sqlite3.IntegrityError):
            self.daemon.apply_sync(snapshot, [event("new"), invalid], START, END, UTC)
        self.assertEqual([e["uid"] for e in self.client.get_events("google", UTC)], ["event"])
        self.assertEqual(self.client.get_calendars("google")[0]["revision"], snapshot["revision"])
        self.assertIsNone(self.client.get_calendars("google")[0]["last_sync"])

    def test_failed_local_move_keeps_source_event(self):
        local = LocalStore(UTC, self.path)
        created = local.create_event(event())
        with self.assertRaises(sqlite3.IntegrityError):
            local.update_event(created["uid"], {**created, "calendar_id": "missing",
                                                "original_calendar_id": "personal"})
        self.assertEqual(local.get_events()[0]["calendar_id"], "personal")

    def test_timezone_change_reinterprets_instants_but_not_all_day_dates(self):
        timed = {**event("timed"), "all_day": False,
                 "time_start": datetime.time(23, 30), "time_end": datetime.time(23, 59)}
        self.client.save_event("google", "account", "work", timed, UTC)
        self.client.save_event("google", "account", "work", event(), UTC)
        events = {e["uid"]: e for e in self.client.get_events("google", ZoneInfo("Europe/Dublin"))}
        self.assertEqual(events["timed"]["date_start"], START + datetime.timedelta(days=1))
        self.assertEqual(events["timed"]["time_start"], datetime.time(0, 30))
        self.assertEqual(events["event"]["date_start"], START)

    def test_timed_event_ending_at_midnight_does_not_occupy_next_day(self):
        next_day = START + datetime.timedelta(days=1)
        for uid, time_start in (("whole", datetime.time.min), ("late", datetime.time(22))):
            timed = {**event(uid), "all_day": False, "date_end": next_day,
                     "time_start": time_start, "time_end": datetime.time.min}
            self.client.save_event("google", "account", "work", timed, UTC)
        for saved in self.client.get_events("google", UTC):
            self.assertFalse(saved["all_day"])
            self.assertEqual(saved["date_end"], next_day)
            self.assertEqual(get_event_last_day(saved), START)
        self.assertEqual(self.client.get_events("google", UTC, next_day), [])
        shifted = {e["uid"]: e for e in self.client.get_events("google", ZoneInfo("Europe/Dublin"))}
        self.assertEqual(get_event_last_day(shifted["whole"]), next_day)

    def test_three_processes_create_events_without_losing_any(self):
        LocalStore(UTC, self.path)
        context = multiprocessing.get_context("spawn")
        ready = context.Barrier(3)
        processes = [context.Process(target=concurrent_writer, args=(self.path, i, ready)) for i in range(3)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
        self.assertEqual(len(LocalStore(UTC, self.path).get_events()), 45)

    def test_private_database_and_corruption_do_not_reset_data(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.client.path.stat().st_mode & 0o777, 0o600)
        self.client.path.write_bytes(b"corrupt database")
        with self.assertRaises(sqlite3.DatabaseError):
            CalendarDatabase(self.path)
        self.assertEqual(self.client.path.read_bytes(), b"corrupt database")


class RemoteSyncUseCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def google(self, response):
        backend = GoogleBackend(self.path, UTC)
        backend.database.connect_account("google", {"id": "account", "name": "Account"}, [{"id": "work"}])
        service = unittest.mock.Mock()
        service.events.return_value.list.return_value.execute.return_value = response
        backend._services["account"] = service
        return backend, service

    def test_google_raw_response_survives_parse_failure_and_next_network_failure(self):
        response = {"items": [{"id": "bad", "summary": "Unparseable"}], "timeZone": "UTC"}
        backend, service = self.google(response)
        self.assertTrue(backend.refresh(START, END))
        latest = next((self.path / "sync").rglob("latest.json"))
        saved = latest.read_bytes()
        self.assertEqual(json.loads(saved)["requests"][0]["response"], response)
        self.assertNotIn("_calendar_id", response["items"][0])
        self.assertEqual(latest.stat().st_mode & 0o777, 0o600)
        self.assertFalse(json.loads((latest.parent / "status.json").read_text())["success"])
        backend._services["account"] = service
        service.events.return_value.list.return_value.execute.side_effect = RuntimeError("Offline")
        self.assertTrue(backend.refresh(START, END))
        self.assertEqual(latest.read_bytes(), saved)
        self.assertEqual(json.loads((latest.parent / "status.json").read_text())["error"], "Offline")

    def test_google_crud_changes_remote_before_local_and_keeps_cache_on_failure(self):
        response = {"id": "event", "summary": "Remote", "start": {"date": START.isoformat()},
                    "end": {"date": (START + datetime.timedelta(days=1)).isoformat()}}
        backend, service = self.google({"items": [response]})
        self.assertEqual(backend.refresh(START, END), [])
        def remote_patch(**kwargs):
            self.assertEqual(backend.get_events()[0]["summary"], "Remote")
            return unittest.mock.Mock(execute=unittest.mock.Mock(return_value={**response, "summary": "Edited"}))
        service.events.return_value.patch.side_effect = remote_patch
        data = {**event(summary="Edited"), "calendar_id": "work", "account_id": "account"}
        with patch.object(backend, "_validate_event_range"):
            backend.update_event("event", data)
        self.assertEqual(backend.get_events()[0]["summary"], "Edited")
        service.events.return_value.delete.return_value.execute.side_effect = RuntimeError("Rejected")
        with self.assertRaises(RuntimeError):
            backend.delete_event("event", "work", "account")
        self.assertEqual(backend.get_events()[0]["summary"], "Edited")

    def test_new_download_replaces_snapshot_and_current_permissions_control_editing(self):
        response = {"items": [{"id": "event", "start": {"date": START.isoformat()},
                               "end": {"date": (START + datetime.timedelta(days=1)).isoformat()}}]}
        backend, service = self.google(response)
        backend.database.update_calendar_list("google", "account", [{"id": "work", "writable": False}])
        self.assertEqual(backend.refresh(START, END), [])
        self.assertFalse(backend.get_events()[0]["editable"])
        backend.database.update_calendar_list("google", "account", [{"id": "work", "writable": True}])
        self.assertTrue(backend.get_events()[0]["editable"])
        service.events.return_value.list.return_value.execute.return_value = {"items": []}
        self.assertEqual(backend.refresh(START, END), [])
        latest = list((self.path / "sync").rglob("latest.json"))
        self.assertEqual(len(latest), 1)
        self.assertEqual(json.loads(latest[0].read_text())["requests"][0]["response"], {"items": []})
        self.assertEqual(backend.get_events(), [])

    def test_auth_failure_records_attempt_without_removing_previous_download(self):
        backend, service = self.google({"items": []})
        self.assertEqual(backend.refresh(START, END), [])
        latest = next((self.path / "sync").rglob("latest.json"))
        saved = latest.read_bytes()
        with patch.object(backend, "_require_service", side_effect=RuntimeError("Authorization expired")):
            self.assertTrue(backend.refresh(START, END))
        self.assertEqual(latest.read_bytes(), saved)
        status = json.loads((latest.parent / "status.json").read_text())
        self.assertFalse(status["success"])
        self.assertEqual(status["start"], START.isoformat())
        self.assertEqual(status["error"], "Authorization expired")

    def test_caldav_raw_alarm_is_retained_but_not_used_and_failed_fetch_keeps_cache(self):
        backend = CalDAVBackend(self.path, UTC)
        payload = _get_event_ical(event(), UTC, "event").replace(
            "END:VEVENT", "BEGIN:VALARM\r\nACTION:AUDIO\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\nEND:VEVENT")
        remote = unittest.mock.Mock(url="https://example.test/work/event.ics", data=payload)
        calendar = unittest.mock.Mock(url="https://example.test/work/")
        calendar.name = "Work"
        calendar.date_search.return_value = [remote]
        with patch.object(backend, "_lookup_password", return_value="secret"), \
                patch.object(backend, "_store_password"), \
                patch.object(backend, "_open", return_value=(object(), [calendar])):
            account_id = backend.connect("https://example.test/", "me", "secret")
            self.assertEqual(backend.refresh(START, END), [])
            latest = next((self.path / "sync").rglob("latest.json"))
            self.assertEqual(json.loads(latest.read_text())["requests"][0]["response"][0]["ical"], payload)
            events = backend.get_events()
            self.assertEqual(events[0]["summary"], "Lunch")
            self.assertNotIn("notification_minutes", events[0])
            self.assertNotIn("secret", latest.read_text())
            calendar.date_search.side_effect = RuntimeError("Offline")
            self.assertTrue(backend.refresh(START, END))
            self.assertEqual(backend.get_events()[0]["uid"], "event")
            self.assertTrue(backend.get_events()[0]["cached"])
            self.assertFalse(backend.get_events()[0]["editable"])

    def test_caldav_expanded_instances_keep_separate_rows_and_are_read_only(self):
        backend = CalDAVBackend(self.path, UTC)
        backend.database.connect_account("caldav", {"id": "a", "name": "A"}, [{"id": "c"}])
        for day in (1, 2):
            date = datetime.date(2026, 9, day)
            payload = _get_event_ical({**event(), "date_start": date, "date_end": date}, UTC, "series")
            payload = payload.replace("END:VEVENT", f"RECURRENCE-ID;VALUE=DATE:2026090{day}\r\nEND:VEVENT")
            for parsed in backend._parse_events(payload, "https://example.test/series.ics"):
                backend.database.save_event("caldav", "a", "c", parsed, UTC)
        events = backend.database.get_events("caldav", UTC)
        self.assertEqual(len(events), 2)
        self.assertEqual({e["date_start"].day for e in events}, {1, 2})
        self.assertTrue(all(not e["editable"] for e in events))
