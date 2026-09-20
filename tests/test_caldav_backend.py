import datetime
import json
import tempfile
import unittest
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "calendar"))
from unittest.mock import patch
import requests
from caldav.lib import error as caldav_error
from icalendar import Event

from backends.caldav import CalDAVBackend, CalDAVUnavailable, _get_event_ical
from store import _component_to_dict


UTC = ZoneInfo("UTC")


class FakeCalendar:
    def __init__(self, url, name):
        self.url = url
        self.name = name


class CalDAVBackendTests(unittest.TestCase):
    def test_hidden_calendar_is_empty_until_refreshed_after_showing(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            start, end = datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)
            raw = {"calendar_id": "hidden", "url": "https://example.test/event.ics",
                   "ical": _get_event_ical({"date_start": start, "date_end": start,
                                        "all_day": True}, UTC, "event")}
            backend.database.connect_account("caldav", {"id": "account", "name": "Account",
                "username": "me", "url": "https://example.test/"}, [{"id": "hidden"}])
            for event in backend._parse_events(raw["ical"], raw["url"]):
                backend.database.save_event("caldav", "account", "hidden", event, UTC)
            backend.set_visible("hidden", False, "account")
            backend.clear_calendar_events("hidden", "account")
            with patch.object(backend, "_lookup_password", return_value="password"), \
                    patch.object(backend, "_open", return_value=(object(), [FakeCalendar("hidden", "Hidden")])):
                self.assertEqual(backend.refresh(start, end), [])
            self.assertEqual(backend.get_events(include_hidden=True), [])
            backend.set_visible("hidden", True, "account")
            self.assertEqual(backend.get_events(), [])

    def test_timed_events_are_displayed_in_the_computer_timezone(self):
        event = Event()
        event.add("dtstart", datetime.datetime(2026, 9, 9, 16, 45,
                                                 tzinfo=datetime.timezone.utc))
        event.add("dtend", datetime.datetime(2026, 9, 9, 17, 45,
                                               tzinfo=datetime.timezone.utc))
        parsed = _component_to_dict(event, ZoneInfo("Europe/Dublin"))
        self.assertEqual(parsed["time_start"], datetime.time(17, 45))
        self.assertEqual(parsed["time_end"], datetime.time(18, 45))

    def test_bare_server_url_falls_back_to_nextcloud_dav_path(self):
        """If the address has no remote.php/dav/, it is added and tried."""
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            calendars = [FakeCalendar("https://cloud.example.test/remote.php/dav/calendars/me/personal/", "Personal")]
            attempts = []

            def fake_open(url, username, password):
                attempts.append(url)
                return object(), calendars if "remote.php" in url else []

            with patch.object(backend, "_open", side_effect=fake_open):
                _client, found, url = backend._discover("https://cloud.example.test/", "me", "secret")
            self.assertEqual(found, calendars)
            self.assertEqual(url, "https://cloud.example.test/remote.php/dav/")
            self.assertEqual(attempts, ["https://cloud.example.test/",
                                        "https://cloud.example.test/remote.php/dav/"])

    def test_server_without_calendars_reports_an_error(self):
        """Finding no calendars gives an error."""
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            with patch.object(backend, "_open", return_value=(object(), [])):
                with self.assertRaisesRegex(CalDAVUnavailable, "remote.php/dav"):
                    backend._discover("https://cloud.example.test/", "me", "secret")

    def test_wrong_password_gives_a_clear_error(self):
        """A rejected login says so, even if the first address failed another way."""
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            failures = [caldav_error.NotFoundError("404"), caldav_error.AuthorizationError("401")]
            with patch.object(backend, "_open", side_effect=failures):
                with self.assertRaisesRegex(CalDAVUnavailable, "username or password"):
                    backend._discover("https://cloud.example.test/", "me", "wrong")

    def test_unreachable_server_gives_a_clear_error(self):
        """A server that does not exist gives an error about the address."""
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            with patch.object(backend, "_open", side_effect=requests.ConnectionError("no route")):
                with self.assertRaisesRegex(CalDAVUnavailable, "reach the server"):
                    backend._discover("https://nowhere.example.test/", "me", "secret")

    def test_connection_metadata_excludes_password(self):
        """Connecting stores account metadata on disk but sends the password to Secret Service."""
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            calendars = [FakeCalendar("https://dav.example.test/calendars/me/work/", "Work")]
            with patch.object(backend, "_open", return_value=(object(), calendars)), \
                    patch.object(backend, "_store_password") as store_password:
                account_id = backend.connect("https://dav.example.test/", "me", "secret")

            saved = json.dumps(backend.database.get_accounts("caldav"))
            self.assertNotIn("secret", saved)
            self.assertEqual(json.loads(saved)[0]["username"], "me")
            store_password.assert_called_once_with(account_id, "me", "secret")

    def test_cached_event_is_read_only_while_offline(self):
        """CalDAV cache entries remain visible but cannot be edited offline."""
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            info = {"id": "account", "url": "https://dav.example.test/", "username": "me",
                    "calendars": [{"id": "https://dav.example.test/work/", "name": "Work",
                                   "visible": True, "writable": True}],
                    "events": [{"calendar_id": "https://dav.example.test/work/",
                                "url": "https://dav.example.test/work/one.ics",
                                "ical": _get_event_ical({"summary": "Meeting", "all_day": False,
                                                     "date_start": datetime.date(2026, 8, 22),
                                                     "date_end": datetime.date(2026, 8, 22),
                                                     "time_start": datetime.time(9),
                                                     "time_end": datetime.time(10)}, UTC, "one")}],
                    "name": "me — dav.example.test"}
            backend.database.connect_account("caldav", {key: value for key, value in info.items()
                if key not in ("calendars", "events")}, info["calendars"])
            for raw in info["events"]:
                for parsed in backend._parse_events(raw["ical"], raw["url"]):
                    backend.database.save_event("caldav", info["id"], raw["calendar_id"], parsed, UTC)
            event = backend.get_events()[0]
            self.assertEqual(event["provider"], "caldav")
            self.assertTrue(event["cached"])
            self.assertFalse(event["editable"])

    def test_caldav_event_does_not_create_an_alarm(self):
        """Clockenstein's universal notification is not stored in CalDAV."""
        payload = _get_event_ical({"summary": "Alert", "all_day": False,
                               "date_start": datetime.date.today() + datetime.timedelta(days=2),
                               "date_end": datetime.date.today() + datetime.timedelta(days=2),
                               "time_start": datetime.time(9), "time_end": datetime.time(10)}, UTC)
        self.assertNotIn("BEGIN:VALARM", payload)

    def test_remote_caldav_alarms_are_removed_from_cached_data(self):
        """Remote alarms are ignored rather than retained in Clockenstein's cache."""
        payload = _get_event_ical({"summary": "Alert", "all_day": False,
                               "date_start": datetime.date.today(),
                               "date_end": datetime.date.today(),
                               "time_start": datetime.time(9), "time_end": datetime.time(10)}, UTC)
        payload = payload.replace(
            "END:VEVENT", "BEGIN:VALARM\r\nACTION:AUDIO\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\nEND:VEVENT"
        )
        with tempfile.TemporaryDirectory() as directory:
            backend = CalDAVBackend(Path(directory), UTC)
            events = backend._parse_events(payload, "https://example.test/alert.ics")
            self.assertNotIn("notification_minutes", events[0])
            self.assertNotIn("ical", events[0])

if __name__ == "__main__":
    unittest.main()
