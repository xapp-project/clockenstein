import dataclasses
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "calendar"))
from backends import (
    BACKENDS,
    CALDAV_CONFIG,
    DEFAULT_CONFIG,
    GOOGLE_CONFIG,
    ICS_CONFIG,
    LOCAL_CONFIG,
    REMOTE_PROVIDERS,
    format_sync_interval,
    get_refresh_schedule,
)
from backends.caldav import CalDAVBackend
from backends.google import GoogleBackend
from backends.ics import IcsBackend


class BackendConfigTests(unittest.TestCase):
    def test_registry_lists_every_backend_in_display_order(self):
        self.assertEqual(list(BACKENDS), ["local", "google", "caldav", "ics"])
        self.assertEqual(
            list(BACKENDS.values()),
            [LOCAL_CONFIG, GOOGLE_CONFIG, CALDAV_CONFIG, ICS_CONFIG],
        )

    def test_backend_classes_use_their_config(self):
        for backend, config in (
            (GoogleBackend, GOOGLE_CONFIG),
            (CalDAVBackend, CALDAV_CONFIG),
            (IcsBackend, ICS_CONFIG),
        ):
            with self.subTest(backend=backend.__name__):
                self.assertIs(backend.config, config)
                self.assertEqual(backend.provider, config.provider)

    def test_configs_inherit_the_default_sync_interval(self):
        self.assertEqual(DEFAULT_CONFIG.sync_interval, 15 * 60)
        self.assertEqual(CALDAV_CONFIG.sync_interval, DEFAULT_CONFIG.sync_interval)
        self.assertEqual(ICS_CONFIG.sync_interval, DEFAULT_CONFIG.sync_interval)
        self.assertEqual(GOOGLE_CONFIG.sync_interval, 2 * 60 * 60)
        self.assertIsNone(LOCAL_CONFIG.sync_interval)

    def test_every_backend_has_a_label(self):
        for config in BACKENDS.values():
            with self.subTest(provider=config.provider):
                self.assertTrue(config.label)

    def test_configs_are_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            GOOGLE_CONFIG.sync_interval = 60

    def test_remote_providers_exclude_local(self):
        self.assertEqual(REMOTE_PROVIDERS, ("google", "caldav", "ics"))


class RefreshScheduleTests(unittest.TestCase):
    def test_providers_sharing_an_interval_share_a_timer(self):
        self.assertEqual(
            get_refresh_schedule(),
            {
                15 * 60: frozenset({"caldav", "ics"}),
                2 * 60 * 60: frozenset({"google"}),
            },
        )

    def test_unsynced_backends_are_not_scheduled(self):
        configs = (
            LOCAL_CONFIG,
            dataclasses.replace(DEFAULT_CONFIG, provider="x", label="X"),
        )
        self.assertEqual(get_refresh_schedule(configs), {15 * 60: frozenset({"x"})})


class SyncIntervalFormattingTests(unittest.TestCase):
    def test_whole_hours_are_shown_in_hours(self):
        self.assertEqual(format_sync_interval(2 * 60 * 60), "Sync every 2 hours")
        self.assertEqual(format_sync_interval(60 * 60), "Sync every 1 hour")

    def test_other_intervals_are_shown_in_minutes(self):
        self.assertEqual(format_sync_interval(15 * 60), "Sync every 15 minutes")
        self.assertEqual(format_sync_interval(90 * 60), "Sync every 90 minutes")
        self.assertEqual(format_sync_interval(60), "Sync every 1 minute")


if __name__ == "__main__":
    unittest.main()
