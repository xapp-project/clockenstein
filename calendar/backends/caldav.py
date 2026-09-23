import datetime
import hashlib
import re
import uuid
import caldav
from caldav.elements.ical import CalendarColor
import requests
from caldav.lib import error as caldav_error
from pathlib import Path
from urllib.parse import urlparse

from icalendar import Calendar, Event
from xapp.util import l10n
from backends.remote import RemoteBackend
from clockenstein.networking import CalDAVAdapter
from clockenstein.sync import SyncDownload

_ = l10n("clockenstein")

from store import _apply_data, _component_to_dict


class CalDAVUnavailable(RuntimeError):
    pass


class CalDAVBackend(RemoteBackend):
    provider = "caldav"

    SECRET_SCHEMA = "org.x.clockenstein.CalDAV"
    REQUEST_TIMEOUT_SECONDS = 20

    def __init__(self, data_dir: Path, timezone: datetime.tzinfo):
        super().__init__(data_dir, timezone)
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
        return [{"id": a["id"], "name": a.get("name", a["username"]),
                 "online": self._account_available(a["id"]),
                 "error": self._errors.get(a["id"], "")} for a in self.accounts]

    def _account_available(self, account_id):
        return (account_id in self._clients
                or account_id in self._configured_accounts and account_id not in self._errors)

    def connect(self, url, username, password, progress=None):
        url = self._normalise_url(url)
        if not username or not password:
            raise CalDAVUnavailable(_("A username and password are required"))
        if progress:
            progress(_("Contacting CalDAV server…"))
        client, calendars, url = self._discover(url, username, password)
        account_id = hashlib.sha256(f"{url}\0{username}".encode()).hexdigest()[:20]
        account = {"id": account_id, "url": url, "username": username,
                   "name": f"{username} — {urlparse(url).hostname or url}"}
        self._store_password(account_id, username, password)
        metadata = self._get_calendar_metadata(calendars, account_id, use_server_colors=True)
        self.database.connect_account(self.provider, account, metadata)
        self._configured_accounts.add(account_id)
        self._clients[account_id] = client
        self._calendars[account_id] = {str(remote_calendar.url): remote_calendar for remote_calendar in calendars}
        self._errors.pop(account_id, None)
        return account_id

    def disconnect(self, account_id):
        account = next((a for a in self.accounts if a["id"] == account_id), None)
        if not account:
            return
        self._clear_password(account_id)
        self.database.disconnect_account(self.provider, account_id)
        self._clients.pop(account_id, None)
        self._calendars.pop(account_id, None)
        self._errors.pop(account_id, None)
        self._configured_accounts.discard(account_id)

    def refresh(self, start, end, target_account_id=None, target_calendar_id=None):
        errors = []
        for account in self.accounts:
            account_id = account["id"]
            if target_account_id and account_id != target_account_id:
                continue
            account_error = None
            try:
                password = self._lookup_password(account_id)
                if not password:
                    self._configured_accounts.discard(account_id)
                    raise CalDAVUnavailable(_("Password not found in the keyring"))
                self._configured_accounts.add(account_id)
                client, remote, _url = self._discover(account["url"], account["username"], password)
                self._clients[account_id] = client
                self._calendars[account_id] = {str(remote_calendar.url): remote_calendar for remote_calendar in remote}
                self.database.update_calendar_list(self.provider, account_id, self._get_calendar_metadata(remote, account_id))
            except Exception as exc:
                self._clients.pop(account_id, None)
                self._calendars.pop(account_id, None)
                self._errors[account_id] = str(exc)
                self._sync_failed(account_id, exc, target_calendar_id, start, end)
                errors.append(f"{account['name']}: {exc}")
                continue
            for info in self.database.get_calendars(self.provider, account_id):
                if target_calendar_id and info["id"] != target_calendar_id:
                    continue
                if not info["visible"]:
                    continue
                calendar = self._calendars[account_id].get(info["id"])
                if calendar is None:
                    continue
                download = SyncDownload(self.database.data_dir, self.provider, account_id, info["id"], start, end)
                try:
                    range_start = datetime.datetime.combine(start, datetime.time.min, self.timezone)
                    range_end = datetime.datetime.combine(end + datetime.timedelta(days=1),
                                                          datetime.time.min, self.timezone)
                    try:
                        remote_events = calendar.date_search(range_start, range_end, expand=True)
                    except Exception:
                        # Some servers do not support recurrence expansion.
                        remote_events = calendar.date_search(range_start, range_end, expand=False)
                    resources = []
                    for remote_event in remote_events:
                        payload = remote_event.data
                        if isinstance(payload, bytes):
                            payload = payload.decode("utf-8")
                        resources.append({"url": str(remote_event.url), "ical": payload})
                    download.add(start, end, resources)
                    download.save()
                    events = [event for resource in resources
                              for event in self._parse_events(resource["ical"], resource["url"])]
                    self.database.apply_sync(info, events, start, end, self.timezone)
                    download.finish()
                except Exception as exc:
                    download.finish(exc)
                    account_error = str(exc)
                    self._errors[account_id] = str(exc)
                    self._sync_failed(account_id, exc, info["id"])
                    errors.append(f"{account['name']}: {exc}")
            if account_error:
                self._clients.pop(account_id, None)
                self._calendars.pop(account_id, None)
            else:
                self._errors.pop(account_id, None)
        return errors

    def create_event(self, data):
        calendar = self._require_calendar(data["account_id"], data["calendar_id"])
        remote = calendar.save_event(_get_event_ical(data, self.timezone))
        self._cache_remote(data["account_id"], data["calendar_id"], remote)
        return data

    def update_event(self, uid, data):
        source_id = data.get("original_calendar_id") or data["calendar_id"]
        events = self.database.get_events(
            self.provider, self.timezone, include_hidden=True,
            account_id=data["account_id"], calendar_id=source_id, uid=uid
        )
        cached = events[0] if events else None
        if not cached or not cached.get("_caldav_url"):
            raise CalDAVUnavailable(_("The event has no CalDAV resource URL"))
        parent = self._require_calendar(data["account_id"], data["calendar_id"])
        if source_id != data["calendar_id"]:
            remote = parent.save_event(_get_event_ical(data, self.timezone, uid))
            source = self._require_calendar(data["account_id"], source_id)
            caldav.Event(client=self._clients[data["account_id"]], parent=source,
                         url=cached["_caldav_url"]).delete()
            self._cache_remote(data["account_id"], data["calendar_id"], remote,
                               source_id)
            return data
        remote = caldav.Event(client=self._clients[data["account_id"]], parent=parent,
                             url=cached["_caldav_url"], data=_get_event_ical(data, self.timezone, uid))
        remote.save()
        self._cache_remote(data["account_id"], data["calendar_id"], remote)
        return data

    def delete_event(self, uid, calendar_id=None, account_id=None):
        events = self.database.get_events(
            self.provider, self.timezone, include_hidden=True,
            account_id=account_id, calendar_id=calendar_id, uid=uid
        )
        cached = events[0] if events else None
        if not cached or not cached.get("_caldav_url"):
            return False
        parent = self._require_calendar(account_id, calendar_id)
        caldav.Event(client=self._clients[account_id], parent=parent,
                    url=cached["_caldav_url"]).delete()
        self.database.delete_event(self.provider, account_id, calendar_id, uid)
        return True

    def _require_calendar(self, account_id, calendar_id):
        calendar = self._calendars.get(account_id, {}).get(calendar_id)
        if calendar is None and account_id in self._configured_accounts:
            account = next((account for account in self.accounts
                            if account["id"] == account_id), None)
            try:
                password = self._lookup_password(account_id)
                if not account or not password:
                    raise CalDAVUnavailable(_("Password not found in the keyring"))
                client, remote, _url = self._discover(account["url"], account["username"], password)
                self._clients[account_id] = client
                self._calendars[account_id] = {str(item.url): item for item in remote}
                self._errors.pop(account_id, None)
                calendar = self._calendars[account_id].get(calendar_id)
            except Exception as exc:
                self._clients.pop(account_id, None)
                self._calendars.pop(account_id, None)
                self._errors[account_id] = str(exc)
        if calendar is None:
            raise CalDAVUnavailable(_("This CalDAV account is offline. It is now read-only."))
        return calendar

    def _cache_remote(self, account_id, calendar_id, remote, source_id=None):
        payload = remote.data
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        for event in self._parse_events(payload, str(remote.url)):
            self.database.save_event(self.provider, account_id, calendar_id,
                                     event, self.timezone, source_id)

    def _parse_events(self, payload, url):
        calendar = Calendar.from_ical(payload)
        result = []
        recurring = _is_recurring(calendar)
        for component in calendar.walk("VEVENT"):
            event = _component_to_dict(component, self.timezone)
            recurrence = component.get("recurrence-id")
            instance = str(recurrence.dt) if recurrence else str(component["dtstart"].dt)
            event["event_key"] = event["uid"] + "|" + instance if recurring else event["uid"]
            event["editable"] = not recurring
            event["_caldav_url"] = url
            result.append(event)
        return result

    def _discover(self, url, username, password):
        try:
            client, calendars = self._open(url, username, password)
            if not calendars and not "remote.php/dav" in url:
                url = url.rstrip("/") + "/remote.php/dav/"
                client, calendars = self._open(url, username, password)
        except Exception as exc:
            raise CalDAVUnavailable(str(exc) or exc.__class__.__name__)
        return client, calendars, url

    @classmethod
    def _open(cls, url, username, password):
        # Ubuntu 24.04 ships python-caldav 0.11, before the constructor gained
        # its timeout keyword. Its requests still use the timeout attribute.
        client = caldav.DAVClient(url=url, username=username, password=password)
        client.timeout = cls.REQUEST_TIMEOUT_SECONDS
        adapter = CalDAVAdapter()
        client.session.mount("http://", adapter)
        client.session.mount("https://", adapter)
        return client, client.principal().calendars()

    @classmethod
    def _get_password_schema(cls):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        return Secret.Schema.new(cls.SECRET_SCHEMA, Secret.SchemaFlags.NONE,
                                 {"account": Secret.SchemaAttributeType.STRING})

    @classmethod
    def _store_password(cls, account_id, username, password):
        from gi.repository import Secret
        ok = Secret.password_store_sync(cls._get_password_schema(), {"account": account_id},
                                        Secret.COLLECTION_DEFAULT,
                                        _("Calendar CalDAV password for %s") % username, password, None)
        if not ok:
            raise CalDAVUnavailable(_("Could not save the password in the keyring"))

    @classmethod
    def _lookup_password(cls, account_id):
        from gi.repository import Secret
        return Secret.password_lookup_sync(cls._get_password_schema(), {"account": account_id}, None)

    @classmethod
    def _clear_password(cls, account_id):
        from gi.repository import Secret
        Secret.password_clear_sync(cls._get_password_schema(), {"account": account_id}, None)

    def _get_calendar_metadata(self, remote, account_id, use_server_colors=False):
        previous = {calendar["id"]: calendar for calendar in
                    self.database.get_calendars(self.provider, account_id)}
        result = []
        for calendar in remote:
            calendar_id = str(calendar.url)
            old = previous.get(calendar_id, {})
            try:
                name = calendar.name or old.get("name") or _("Calendar")
            except Exception:
                name = old.get("name") or _("Calendar")
            color = old.get("color", self._color(calendar_id))
            if use_server_colors:
                try:
                    server_color = calendar.get_property(CalendarColor())
                    if server_color:
                        server_color = server_color.strip()
                        if re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", server_color):
                            # Servers may append alpha; calendar colors are opaque.
                            color = server_color[:7]
                except Exception:
                    # Color is optional. Keep the saved or generated color if
                    # the server cannot provide this property during setup.
                    pass
            result.append({"id": calendar_id, "name": str(name),
                           "color": color, "writable": True})
        return result

    @staticmethod
    def _normalise_url(url):
        url = url.strip()
        if not url:
            raise CalDAVUnavailable(_("A server URL is required"))
        if not urlparse(url).scheme:
            url = "https://" + url
        if urlparse(url).scheme not in ("http", "https"):
            raise CalDAVUnavailable(_("CalDAV server URLs must use HTTP or HTTPS"))
        return url.rstrip("/") + "/"

    @staticmethod
    def _color(value):
        colors = ("#3584e4", "#33d17a", "#e5a50a", "#e66100", "#c061cb", "#1c71d8")
        return colors[int(hashlib.sha256(value.encode()).hexdigest()[:4], 16) % len(colors)]


def _get_event_ical(data, timezone: datetime.tzinfo, uid=None):
    calendar = Calendar()
    calendar.add("prodid", "-//Clockenstein//EN")
    calendar.add("version", "2.0")
    event = Event()
    event.add("uid", uid or data.get("uid") or str(uuid.uuid4()))
    event.add("dtstamp", datetime.datetime.now(datetime.timezone.utc))
    _apply_data(event, data, timezone)
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def _is_recurring(calendar):
    return any(any(key in event for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"))
               for event in calendar.walk("VEVENT"))
