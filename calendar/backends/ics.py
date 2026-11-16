import datetime
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret
import recurring_ical_events
import requests
from icalendar import Calendar
from xapp.util import l10n
from backends.remote import RemoteBackend
from clockenstein.networking import CalDAVAdapter
from clockenstein.sync import SyncDownload

_ = l10n("clockenstein")

from store import _component_to_dict


class IcsUnavailable(RuntimeError):
    pass


class IcsBackend(RemoteBackend):
    """Read-only subscription to an iCalendar (.ics) link."""

    provider = "ics"
    calendar_id = "feed"

    SECRET_SCHEMA = "org.x.clockenstein.ICS"
    REQUEST_TIMEOUT_SECONDS = 20
    MAX_FEED_BYTES = 20 * 1024 * 1024

    def __init__(self, data_dir: Path, timezone: datetime.tzinfo):
        super().__init__(data_dir, timezone)
        # RemoteBackend._sync_failed also clears these on disconnect.
        self._clients = {}
        self._calendars = {}
        self._errors = {}
        self._configured_accounts = set()
        for account in self.accounts:
            try:
                if self._lookup_password(account["id"]):
                    self._configured_accounts.add(account["id"])
            except Exception as exc:
                self._errors[account["id"]] = str(exc)

    def get_account_states(self):
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "online": self._account_available(a["id"]),
                "error": self._errors.get(a["id"], ""),
            }
            for a in self.accounts
        ]

    def _account_available(self, account_id):
        return (
            account_id in self._configured_accounts and account_id not in self._errors
        )

    def connect(self, url, name=None, progress=None):
        url = self._normalise_url(url)
        if progress:
            progress(_("Downloading calendar…"))
        calendar = self._read_calendar(self._fetch(url))
        parsed = urlparse(url)
        host = parsed.hostname or url
        name = (
            (name or "").strip()
            or str(calendar.get("x-wr-calname", "")).strip()
            or host
        )
        account_id = hashlib.sha256(url.encode()).hexdigest()[:20]
        # Feed links often embed an access token, so only the keyring keeps the full URL.
        account = {"id": account_id, "name": name, "url": f"{parsed.scheme}://{host}/"}
        self._store_password(account_id, name, url)
        metadata = [
            {
                "id": self.calendar_id,
                "name": name,
                "writable": False,
                "color": self._get_feed_color(calendar) or self._color(account_id),
            }
        ]
        self.database.connect_account(self.provider, account, metadata)
        self._configured_accounts.add(account_id)
        self._errors.pop(account_id, None)
        return account_id

    def disconnect(self, account_id):
        self._clear_password(account_id)
        self.database.disconnect_account(self.provider, account_id)
        self._errors.pop(account_id, None)
        self._configured_accounts.discard(account_id)

    def refresh(self, start, end, target_account_id=None, target_calendar_id=None):
        errors = []
        for account in self.accounts:
            account_id = account["id"]
            if target_account_id and account_id != target_account_id:
                continue
            for info in self.database.get_calendars(self.provider, account_id):
                if target_calendar_id and info["id"] != target_calendar_id:
                    continue
                if not info["visible"]:
                    continue
                download = SyncDownload(
                    self.database.data_dir,
                    self.provider,
                    account_id,
                    info["id"],
                    start,
                    end,
                )
                try:
                    url = self._lookup_password(account_id)
                    if not url:
                        self._configured_accounts.discard(account_id)
                        raise IcsUnavailable(
                            _("Calendar link not found in the keyring")
                        )
                    self._configured_accounts.add(account_id)
                    payload = self._fetch(url)
                    download.add(start, end, payload)
                    download.save()
                    events = self._parse_events(payload, start, end)
                    self.database.apply_sync(info, events, start, end, self.timezone)
                    download.finish()
                    self._errors.pop(account_id, None)
                except Exception as exc:
                    download.finish(exc)
                    self._errors[account_id] = str(exc)
                    self.database.update_calendar(
                        self.provider, account_id, info["id"], sync_error=str(exc)
                    )
                    errors.append(f"{account['name']}: {exc}")
        return errors

    def create_event(self, data):
        raise IcsUnavailable(_("Internet calendars are read-only"))

    def update_event(self, uid, data):
        raise IcsUnavailable(_("Internet calendars are read-only"))

    def delete_event(self, uid, calendar_id=None, account_id=None):
        raise IcsUnavailable(_("Internet calendars are read-only"))

    def _fetch(self, url):
        session = requests.Session()
        adapter = CalDAVAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        try:
            response = session.get(
                url,
                timeout=self.REQUEST_TIMEOUT_SECONDS,
                headers={"Accept": "text/calendar, */*;q=0.5"},
            )
        except requests.RequestException as exc:
            raise IcsUnavailable(str(exc) or exc.__class__.__name__)
        finally:
            session.close()
        if response.status_code >= 400:
            raise IcsUnavailable(
                _("The server answered HTTP %d") % response.status_code
            )
        if len(response.content) > self.MAX_FEED_BYTES:
            raise IcsUnavailable(_("The calendar is too large"))
        return response.content.decode("utf-8", errors="replace")

    @staticmethod
    def _read_calendar(payload):
        try:
            calendar = Calendar.from_ical(payload)
        except Exception:
            calendar = None
        if calendar is None or calendar.name != "VCALENDAR":
            raise IcsUnavailable(
                _("The link does not point to an iCalendar (.ics) file")
            )
        return calendar

    def _parse_events(self, payload, start, end):
        calendar = self._read_calendar(payload)
        recurring = {
            str(component.get("uid", ""))
            for component in calendar.walk("VEVENT")
            if any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID"))
        }
        range_start = datetime.datetime.combine(start, datetime.time.min, self.timezone)
        range_end = datetime.datetime.combine(
            end + datetime.timedelta(days=1), datetime.time.min, self.timezone
        )
        result = []
        for component in recurring_ical_events.of(calendar).between(
            range_start, range_end
        ):
            event = _component_to_dict(component, self.timezone)
            if not event["uid"]:
                # The UID is required by RFC 5545, but some generators omit it.
                seed = f"{event['_start']}\0{event['summary']}"
                event["uid"] = "ics-" + hashlib.sha256(seed.encode()).hexdigest()[:20]
            if event["uid"] in recurring:
                recurrence = component.get("recurrence-id")
                instance = recurrence.dt if recurrence else component["dtstart"].dt
                event["event_key"] = event["uid"] + "|" + str(instance)
            else:
                event["event_key"] = event["uid"]
            event["editable"] = False
            result.append(event)
        return result

    @staticmethod
    def _get_feed_color(calendar):
        for key in ("color", "x-apple-calendar-color"):
            value = str(calendar.get(key, "")).strip()
            if re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
                # Feeds may append alpha; calendar colors are opaque.
                return value[:7].lower()
        return None

    @staticmethod
    def _normalise_url(url):
        url = url.strip()
        if not url:
            raise IcsUnavailable(_("A calendar link is required"))
        scheme = urlparse(url).scheme.lower()
        if scheme in ("webcal", "webcals"):
            url = "https" + url[len(scheme) :]
        elif not scheme:
            url = "https://" + url
        if urlparse(url).scheme.lower() not in ("http", "https"):
            raise IcsUnavailable(_("Calendar links must use HTTP, HTTPS or webcal"))
        return url

    @classmethod
    def _get_password_schema(cls):
        return Secret.Schema.new(
            cls.SECRET_SCHEMA,
            Secret.SchemaFlags.NONE,
            {"account": Secret.SchemaAttributeType.STRING},
        )

    @classmethod
    def _store_password(cls, account_id, name, url):
        ok = Secret.password_store_sync(
            cls._get_password_schema(),
            {"account": account_id},
            Secret.COLLECTION_DEFAULT,
            _("Calendar link for %s") % name,
            url,
            None,
        )
        if not ok:
            raise IcsUnavailable(_("Could not save the calendar link in the keyring"))

    @classmethod
    def _lookup_password(cls, account_id):
        return Secret.password_lookup_sync(
            cls._get_password_schema(), {"account": account_id}, None
        )

    @classmethod
    def _clear_password(cls, account_id):
        Secret.password_clear_sync(
            cls._get_password_schema(), {"account": account_id}, None
        )
