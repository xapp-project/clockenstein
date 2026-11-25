import datetime
import json
import tempfile
import unittest
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "calendar"))
from unittest.mock import patch

from backends.ics import IcsBackend, IcsUnavailable


UTC = ZoneInfo("UTC")
SECRET_URL = "https://feeds.example.test/private/s3cr3t-token/basic.ics"

FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example//EN
X-WR-CALNAME:Team Schedule
X-APPLE-CALENDAR-COLOR:#E66100
BEGIN:VEVENT
UID:single
DTSTAMP:20260901T000000Z
DTSTART:20260910T090000Z
DTEND:20260910T100000Z
SUMMARY:Kick-off
END:VEVENT
BEGIN:VEVENT
UID:later
DTSTAMP:20260901T000000Z
DTSTART;VALUE=DATE:20261201
DTEND;VALUE=DATE:20261202
SUMMARY:Out of range
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTAMP:20260901T000000Z
DTSTART:20260907T150000Z
DTEND:20260907T160000Z
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE:20260914T150000Z
SUMMARY:Practice
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTAMP:20260901T000000Z
RECURRENCE-ID:20260921T150000Z
DTSTART:20260921T170000Z
DTEND:20260921T180000Z
SUMMARY:Practice (moved)
END:VEVENT
END:VCALENDAR
"""

START, END = datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)


def _feed_without(uid):
    blocks = FEED.split("BEGIN:VEVENT")
    kept = [block for block in blocks[1:] if f"UID:{uid}\n" not in block]
    payload = blocks[0] + "".join("BEGIN:VEVENT" + block for block in kept)
    if not payload.rstrip().endswith("END:VCALENDAR"):
        payload += "END:VCALENDAR\n"
    return payload


class IcsBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.keyring = {}
        patches = (
            patch.object(
                IcsBackend,
                "_store_password",
                side_effect=lambda account_id, name, url: self.keyring.__setitem__(
                    account_id, url
                ),
            ),
            patch.object(
                IcsBackend,
                "_lookup_password",
                side_effect=lambda account_id: self.keyring.get(account_id),
            ),
            patch.object(
                IcsBackend,
                "_clear_password",
                side_effect=lambda account_id: self.keyring.pop(account_id, None),
            ),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.directory.cleanup)
        self.backend = IcsBackend(Path(self.directory.name), UTC)

    def _connect(self, url=SECRET_URL, payload=FEED, name=None):
        with patch.object(self.backend, "_fetch", return_value=payload):
            return self.backend.connect(url, name)

    def _refresh(self, payload=FEED, start=START, end=END):
        with patch.object(self.backend, "_fetch", return_value=payload) as fetch:
            errors = self.backend.refresh(start, end)
        return errors, fetch

    def test_urls_are_normalised_to_http(self):
        self.assertEqual(
            IcsBackend._normalise_url(" webcal://example.test/a.ics "),
            "https://example.test/a.ics",
        )
        self.assertEqual(
            IcsBackend._normalise_url("webcals://example.test/a.ics"),
            "https://example.test/a.ics",
        )
        self.assertEqual(
            IcsBackend._normalise_url("example.test/a.ics"),
            "https://example.test/a.ics",
        )
        self.assertEqual(
            IcsBackend._normalise_url("http://example.test/a.ics"),
            "http://example.test/a.ics",
        )
        for url in ("", "ftp://example.test/a.ics"):
            with self.assertRaises(IcsUnavailable):
                IcsBackend._normalise_url(url)

    def test_connect_keeps_the_feed_url_out_of_the_database(self):
        """Feed links are often secret, so only the keyring holds the full URL."""
        account_id = self._connect(
            "webcal://feeds.example.test/private/s3cr3t-token/basic.ics"
        )
        self.assertEqual(self.keyring[account_id], SECRET_URL)
        saved = json.dumps(self.backend.database.get_accounts("ics"))
        self.assertNotIn("s3cr3t-token", saved)
        self.assertEqual(self.backend.accounts[0]["url"], "https://feeds.example.test/")

    def test_connect_uses_feed_name_and_color(self):
        self._connect()
        calendar = self.backend.list_calendars()[0]
        self.assertEqual(calendar["name"], "Team Schedule")
        self.assertEqual(calendar["color"], "#e66100")
        self.assertFalse(calendar["writable"])
        self.assertTrue(calendar["available"])
        self.assertEqual(self.backend.accounts[0]["name"], "Team Schedule")

    def test_connect_name_overrides_feed_and_falls_back_to_host(self):
        self._connect(name="Football")
        self.assertEqual(self.backend.list_calendars()[0]["name"], "Football")
        bare = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//EN\nEND:VCALENDAR\n"
        self._connect("https://other.example.test/cal.ics", bare)
        names = {calendar["name"] for calendar in self.backend.list_calendars()}
        self.assertIn("other.example.test", names)

    def test_connect_rejects_content_that_is_not_a_calendar(self):
        with self.assertRaises(IcsUnavailable):
            self._connect(payload="<html>Not found</html>")
        self.assertEqual(self.backend.accounts, [])
        self.assertEqual(self.keyring, {})

    def test_refresh_stores_events_within_range(self):
        self._connect()
        errors, fetch = self._refresh()
        self.assertEqual(errors, [])
        fetch.assert_called_once_with(SECRET_URL)
        summaries = sorted(
            event["summary"] for event in self.backend.get_events(START, END)
        )
        self.assertIn("Kick-off", summaries)
        self.assertNotIn("Out of range", summaries)
        self.assertTrue(
            all(event["provider"] == "ics" for event in self.backend.get_events())
        )

    def test_recurring_events_are_expanded_locally(self):
        self._connect()
        self._refresh()
        practice = sorted(
            (
                event
                for event in self.backend.get_events(START, END)
                if event["uid"] == "weekly"
            ),
            key=lambda event: event["date_start"],
        )
        self.assertEqual(
            [(event["date_start"], event["time_start"]) for event in practice],
            [
                (datetime.date(2026, 9, 7), datetime.time(15)),
                (datetime.date(2026, 9, 21), datetime.time(17)),
                (datetime.date(2026, 9, 28), datetime.time(15)),
            ],
        )
        self.assertEqual(practice[1]["summary"], "Practice (moved)")
        self.assertEqual(len({event["event_key"] for event in practice}), 3)
        self.assertFalse(any(event["editable"] for event in self.backend.get_events()))

    def test_events_removed_from_the_feed_are_removed_on_refresh(self):
        self._connect()
        self._refresh()
        self._refresh(_feed_without("single"))
        uids = {event["uid"] for event in self.backend.get_events(START, END)}
        self.assertNotIn("single", uids)
        self.assertIn("weekly", uids)

    def test_failed_refresh_keeps_cached_events_and_reports_the_error(self):
        self._connect()
        self._refresh()
        with patch.object(
            self.backend, "_fetch", side_effect=IcsUnavailable("HTTP 503")
        ):
            errors = self.backend.refresh(START, END)
        self.assertEqual(errors, ["Team Schedule: HTTP 503"])
        calendar = self.backend.list_calendars()[0]
        self.assertEqual(calendar["sync_error"], "HTTP 503")
        self.assertFalse(calendar["available"])
        events = self.backend.get_events(START, END)
        self.assertTrue(events)
        self.assertTrue(all(event["cached"] for event in events))
        self.assertEqual(len(self.backend.accounts), 1)

    def test_hidden_calendar_is_not_downloaded(self):
        account_id = self._connect()
        self.backend.set_visible("feed", False, account_id)
        errors, fetch = self._refresh()
        self.assertEqual(errors, [])
        fetch.assert_not_called()

    def test_refresh_honours_account_target(self):
        self._connect()
        with patch.object(self.backend, "_fetch", return_value=FEED) as fetch:
            self.backend.refresh(START, END, target_account_id="other")
        fetch.assert_not_called()

    def test_missing_keyring_entry_is_reported(self):
        account_id = self._connect()
        self.keyring.clear()
        errors, fetch = self._refresh()
        fetch.assert_not_called()
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.backend.get_account_states()[0]["online"])
        self.assertEqual(self.backend.get_account_states()[0]["id"], account_id)

    def test_disconnect_removes_account_and_keyring_entry(self):
        account_id = self._connect()
        self._refresh()
        self.backend.disconnect(account_id)
        self.assertEqual(self.backend.accounts, [])
        self.assertEqual(self.backend.get_events(), [])
        self.assertEqual(self.keyring, {})

    def test_feed_is_read_only(self):
        account_id = self._connect()
        with self.assertRaises(IcsUnavailable):
            self.backend.create_event(
                {"provider": "ics", "account_id": account_id, "calendar_id": "feed"}
            )
        with self.assertRaises(IcsUnavailable):
            self.backend.update_event(
                "single", {"account_id": account_id, "calendar_id": "feed"}
            )
        with self.assertRaises(IcsUnavailable):
            self.backend.delete_event("single", "feed", account_id)

    def test_events_without_uid_get_a_stable_key(self):
        payload = FEED.replace("UID:single\n", "")
        self._connect(payload=payload)
        self._refresh(payload)
        first = {event["event_key"] for event in self.backend.get_events(START, END)}
        self._refresh(payload)
        second = {event["event_key"] for event in self.backend.get_events(START, END)}
        self.assertEqual(first, second)
        kickoff = [
            event
            for event in self.backend.get_events(START, END)
            if event["summary"] == "Kick-off"
        ]
        self.assertEqual(len(kickoff), 1)
        self.assertTrue(kickoff[0]["uid"])


class CalendarManagerIcsTests(unittest.TestCase):
    def test_manager_aggregates_ics_calendars_and_events(self):
        from store import CalendarManager

        keyring = {}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                IcsBackend,
                "_store_password",
                side_effect=lambda account_id, name, url: keyring.__setitem__(
                    account_id, url
                ),
            ),
            patch.object(
                IcsBackend,
                "_lookup_password",
                side_effect=lambda account_id: keyring.get(account_id),
            ),
        ):
            manager = CalendarManager(UTC, Path(directory))
            self.assertFalse(manager.has_remote_accounts)
            with patch.object(IcsBackend, "_fetch", return_value=FEED):
                account_id = manager.ics.connect(SECRET_URL)
                self.assertEqual(manager.refresh_remote(START, END), [])
            self.assertTrue(manager.has_remote_accounts)
            self.assertIn(
                ("ics", account_id, "feed"),
                [
                    (c["provider"], c.get("account_id"), c["id"])
                    for c in manager.list_calendars()
                ],
            )
            self.assertIn(
                "Kick-off",
                [event["summary"] for event in manager.get_events(START, END)],
            )
            self.assertNotIn(
                ("ics", account_id, "feed"),
                [
                    (c["provider"], c["account_id"], c["id"])
                    for c in manager.get_writable_calendars()
                ],
            )
            with self.assertRaises(IcsUnavailable):
                manager.create_event(
                    {"provider": "ics", "account_id": account_id, "calendar_id": "feed"}
                )


if __name__ == "__main__":
    unittest.main()
