import datetime
import hashlib
import json
import caldav
from pathlib import Path
from urllib.parse import urlparse

from icalendar import Calendar, Event
from xapp.util import l10n

_ = l10n("clockenstein")

from store import _apply_data, _component_to_dict


class CalDAVUnavailable(RuntimeError):
    pass


class CalDAVBackend:
    SECRET_SCHEMA = "org.x.clockenstein.CalDAV"

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_file = self.data_dir / "accounts.json"
        self.accounts = self._read_json(self.accounts_file, [])
        cache_changed = False
        for account in self.accounts:
            for event in account.get("events", []):
                sanitized = _without_alarms(event.get("ical", ""))
                if sanitized != event.get("ical", ""):
                    event["ical"] = sanitized
                    cache_changed = True
        if cache_changed:
            self._save()
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

    @property
    def has_accounts(self):
        return bool(self.accounts)

    def account_states(self):
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
        account = next((a for a in self.accounts if a["id"] == account_id), None)
        if account is None:
            account = {"id": account_id, "url": url, "username": username,
                       "name": f"{username} — {urlparse(url).hostname or url}",
                       "calendars": [], "events": []}
            self.accounts.append(account)
        account.update(url=url, username=username)
        account["calendars"] = self._merge_calendars(account.get("calendars", []), calendars)
        self._store_password(account_id, username, password)
        self._configured_accounts.add(account_id)
        self._clients[account_id] = client
        self._calendars[account_id] = {str(c.url): c for c in calendars}
        self._errors.pop(account_id, None)
        self._save()
        return account_id

    def disconnect(self, account_id):
        account = next((a for a in self.accounts if a["id"] == account_id), None)
        if not account:
            return
        self._clear_password(account_id)
        self.accounts.remove(account)
        self._clients.pop(account_id, None)
        self._calendars.pop(account_id, None)
        self._errors.pop(account_id, None)
        self._configured_accounts.discard(account_id)
        self._save()

    def list_calendars(self):
        result = []
        for account in self.accounts:
            online = self._account_available(account["id"])
            for cal in account.get("calendars", []):
                result.append({"id": cal["id"], "name": cal.get("name", _("Calendar")),
                               "color": cal.get("color", self._color(cal["id"])),
                               "provider": "caldav", "account_id": account["id"],
                               "account_name": account.get("name", account["username"]),
                               "visible": cal.get("visible", True), "primary": False,
                               "reminders": cal.get("reminders", True),
                               "last_sync": cal.get("last_sync"),
                               "sync_error": cal.get("sync_error", ""),
                               "writable": cal.get("writable", True), "available": online})
        return result

    def set_visible(self, calendar_id, visible, account_id=None):
        for account in self.accounts:
            if account_id and account["id"] != account_id:
                continue
            for cal in account.get("calendars", []):
                if cal["id"] == calendar_id:
                    cal["visible"] = bool(visible)
                    self._save()
                    return

    def set_reminders(self, calendar_id, enabled, account_id=None):
        for account in self.accounts:
            if account_id and account["id"] != account_id:
                continue
            for cal in account.get("calendars", []):
                if cal["id"] == calendar_id:
                    cal["reminders"] = bool(enabled)
                    self._save()
                    return

    def get_events(self, start=None, end=None, include_hidden=False):
        result = []
        for account in self.accounts:
            online = self._account_available(account["id"])
            calendars = {c["id"]: c for c in account.get("calendars", [])
                         if include_hidden or c.get("visible", True)}
            for raw in account.get("events", []):
                cal = calendars.get(raw.get("calendar_id"))
                if not cal:
                    continue
                try:
                    component = next(c for c in Calendar.from_ical(raw["ical"]).walk()
                                     if c.name == "VEVENT")
                    event = _component_to_dict(component)
                except Exception:
                    continue
                if start and event["date_end"] < start or end and event["date_start"] > end:
                    continue
                event.update(provider="caldav", account_id=account["id"],
                             account_name=account.get("name", account["username"]),
                             calendar_id=cal["id"], calendar_name=cal.get("name", _("Calendar")),
                             calendar_color=cal.get("color", self._color(cal["id"])),
                             reminders=cal.get("reminders", True),
                             editable=bool(online and cal.get("writable", True)), cached=not online,
                             _caldav_url=raw.get("url"))
                result.append(event)
        return result

    def refresh(self, start, end, target_account_id=None, target_calendar_id=None):
        errors = []
        for account in self.accounts:
            account_id = account["id"]
            if target_account_id and account_id != target_account_id:
                continue
            try:
                password = self._lookup_password(account_id)
                if not password:
                    self._configured_accounts.discard(account_id)
                    raise CalDAVUnavailable(_("Password not found in the keyring"))
                self._configured_accounts.add(account_id)
                client, remote, resolved_url = self._discover(
                    account["url"], account["username"], password)
                account["url"] = resolved_url
                self._clients[account_id] = client
                self._calendars[account_id] = {str(c.url): c for c in remote}
                account["calendars"] = self._merge_calendars(account.get("calendars", []), remote)
                retained = [e for e in account.get("events", []) if not _overlaps(e, start, end)]
                fetched = []
                for info in account["calendars"]:
                    if target_calendar_id and info["id"] != target_calendar_id:
                        continue
                    if not info.get("visible", True) and not target_calendar_id:
                        continue
                    calendar = self._calendars[account_id].get(info["id"])
                    if calendar is None:
                        continue
                    range_start = datetime.datetime.combine(start, datetime.time.min)
                    range_end = datetime.datetime.combine(
                        end + datetime.timedelta(days=1), datetime.time.min)
                    try:
                        remote_events = calendar.date_search(range_start, range_end, expand=True)
                    except Exception:
                        # Expansion is optional and rejected by some otherwise valid servers.
                        remote_events = calendar.date_search(range_start, range_end, expand=False)
                    for remote_event in remote_events:
                        payload = remote_event.data
                        if isinstance(payload, bytes):
                            payload = payload.decode("utf-8")
                        fetched.append({"calendar_id": info["id"], "url": str(remote_event.url),
                                        "ical": _without_alarms(payload)})
                    info["last_sync"] = int(datetime.datetime.now().timestamp())
                    info["sync_error"] = ""
                if target_calendar_id:
                    retained = [event for event in account.get("events", [])
                                if event.get("calendar_id") != target_calendar_id
                                or not _overlaps(event, start, end)]
                account["events"] = retained + fetched
                self._errors.pop(account_id, None)
            except Exception as exc:
                self._clients.pop(account_id, None)
                self._calendars.pop(account_id, None)
                self._errors[account_id] = str(exc)
                for info in account.get("calendars", []):
                    if target_calendar_id and info["id"] != target_calendar_id:
                        continue
                    info["sync_error"] = str(exc)
                errors.append(f"{account.get('name', account['username'])}: {exc}")
        self._save()
        return errors

    def create_event(self, data):
        calendar = self._require_calendar(data["account_id"], data["calendar_id"])
        remote = calendar.save_event(_event_ical(data))
        self._cache_remote(data["account_id"], data["calendar_id"], remote)
        return data

    def update_event(self, uid, data):
        account = next(a for a in self.accounts if a["id"] == data["account_id"])
        source_id = data.get("original_calendar_id") or data["calendar_id"]
        cached = next((e for e in account.get("events", [])
                       if e.get("calendar_id") == source_id and _cached_uid(e) == uid), None)
        if not cached or not cached.get("url"):
            raise CalDAVUnavailable(_("The event has no CalDAV resource URL"))
        parent = self._require_calendar(data["account_id"], data["calendar_id"])
        if source_id != data["calendar_id"]:
            remote = parent.save_event(_event_ical(data, uid))
            source = self._require_calendar(data["account_id"], source_id)
            caldav.Event(client=self._clients[data["account_id"]], parent=source,
                         url=cached["url"]).delete()
            self._cache_remote(data["account_id"], data["calendar_id"], remote,
                               cached["url"])
            return data
        remote = caldav.Event(client=self._clients[data["account_id"]], parent=parent,
                             url=cached["url"], data=_event_ical(data, uid))
        remote.save()
        self._cache_remote(data["account_id"], data["calendar_id"], remote, cached["url"])
        return data

    def delete_event(self, uid, calendar_id=None, account_id=None):
        account = next(a for a in self.accounts if a["id"] == account_id)
        cached = next((e for e in account.get("events", [])
                       if e.get("calendar_id") == calendar_id and _cached_uid(e) == uid), None)
        if not cached or not cached.get("url"):
            return False
        self._require_calendar(account_id, calendar_id)
        parent = self._require_calendar(account_id, calendar_id)
        caldav.Event(client=self._clients[account_id], parent=parent,
                    url=cached["url"]).delete()
        account["events"].remove(cached)
        self._save()
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
                client, remote, resolved_url = self._discover(
                    account["url"], account["username"], password)
                account["url"] = resolved_url
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

    def _cache_remote(self, account_id, calendar_id, remote, old_url=None):
        account = next(a for a in self.accounts if a["id"] == account_id)
        payload = remote.data
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        url = str(remote.url)
        account["events"] = [e for e in account.get("events", [])
                             if e.get("url") not in (url, old_url)]
        account["events"].append({"calendar_id": calendar_id, "url": url,
                                  "ical": _without_alarms(payload)})
        self._save()

    def _discover(self, url, username, password):
        # Nextcloud (and ownCloud) serve calendars under remote.php/dav/, not
        # at the bare server address. A PROPFIND against the bare address
        # often succeeds with zero calendars instead of raising, which used
        # to look like a silent no-op. Retry with the conventional path and
        # only give up with a clear error if that also comes up empty.
        candidates = [url]
        if "remote.php" not in url:
            candidates.append(url.rstrip("/") + "/remote.php/dav/")
        last_error = None
        for candidate in candidates:
            try:
                client, calendars = self._open(candidate, username, password)
            except Exception as exc:
                last_error = exc
                continue
            if calendars:
                return client, calendars, candidate
            last_error = CalDAVUnavailable(
                _("Connected, but no calendars were found at this address. "
                  "If this is a Nextcloud or ownCloud server, make sure the "
                  "URL includes remote.php/dav/."))
        raise last_error

    @staticmethod
    def _open(url, username, password):
        # Ubuntu 24.04 ships python-caldav 0.11, before the constructor gained
        # its timeout keyword. Network work always runs outside the GTK thread.
        client = caldav.DAVClient(url=url, username=username, password=password)
        return client, client.principal().calendars()

    @classmethod
    def _schema(cls):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        return Secret.Schema.new(cls.SECRET_SCHEMA, Secret.SchemaFlags.NONE,
                                 {"account": Secret.SchemaAttributeType.STRING})

    @classmethod
    def _store_password(cls, account_id, username, password):
        from gi.repository import Secret
        ok = Secret.password_store_sync(cls._schema(), {"account": account_id},
                                        Secret.COLLECTION_DEFAULT,
                                        _("Calendar CalDAV password for %s") % username, password, None)
        if not ok:
            raise CalDAVUnavailable(_("Could not save the password in the keyring"))

    @classmethod
    def _lookup_password(cls, account_id):
        from gi.repository import Secret
        return Secret.password_lookup_sync(cls._schema(), {"account": account_id}, None)

    @classmethod
    def _clear_password(cls, account_id):
        from gi.repository import Secret
        Secret.password_clear_sync(cls._schema(), {"account": account_id}, None)

    @staticmethod
    def _merge_calendars(old, remote):
        preferences = {c["id"]: c for c in old}
        result = []
        for calendar in remote:
            calendar_id = str(calendar.url)
            previous = preferences.get(calendar_id, {})
            try:
                name = calendar.name or previous.get("name") or _("Calendar")
            except Exception:
                name = previous.get("name") or _("Calendar")
            result.append({"id": calendar_id, "name": str(name),
                           "color": previous.get("color", CalDAVBackend._color(calendar_id)),
                           "visible": previous.get("visible", True), "writable": True,
                           "reminders": previous.get("reminders", True),
                           "last_sync": previous.get("last_sync"),
                           "sync_error": previous.get("sync_error", "")})
        return result

    @staticmethod
    def _normalise_url(url):
        url = url.strip()
        if not url:
            raise CalDAVUnavailable(_("A server URL is required"))
        if not urlparse(url).scheme:
            url = "https://" + url
        if urlparse(url).scheme != "https":
            raise CalDAVUnavailable(_("CalDAV connections must use HTTPS"))
        return url.rstrip("/") + "/"

    @staticmethod
    def _color(value):
        colors = ("#3584e4", "#33d17a", "#e5a50a", "#e66100", "#c061cb", "#1c71d8")
        return colors[int(hashlib.sha256(value.encode()).hexdigest()[:4], 16) % len(colors)]

    def _save(self):
        temp = self.accounts_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.accounts, indent=2), encoding="utf-8")
        temp.replace(self.accounts_file)

    @staticmethod
    def _read_json(path, default):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else default
        except (OSError, ValueError):
            return default


def _event_ical(data, uid=None):
    calendar = Calendar()
    calendar.add("prodid", "-//Clockenstein//EN")
    calendar.add("version", "2.0")
    event = Event()
    event.add("uid", uid or data.get("uid") or hashlib.sha256(
        f"{datetime.datetime.now().isoformat()}:{data.get('summary', '')}".encode()).hexdigest())
    event.add("dtstamp", datetime.datetime.now(datetime.timezone.utc))
    _apply_data(event, data)
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def _without_alarms(payload):
    try:
        calendar = Calendar.from_ical(payload)
        for component in calendar.walk("VEVENT"):
            component.subcomponents = [child for child in component.subcomponents
                                       if child.name != "VALARM"]
        return calendar.to_ical().decode("utf-8")
    except Exception:
        return payload


def _cached_uid(raw):
    try:
        return str(next(c for c in Calendar.from_ical(raw["ical"]).walk()
                        if c.name == "VEVENT").get("uid", ""))
    except Exception:
        return ""


def _overlaps(raw, start, end):
    try:
        event = _component_to_dict(next(c for c in Calendar.from_ical(raw["ical"]).walk()
                                        if c.name == "VEVENT"))
        return event["date_end"] >= start and event["date_start"] <= end
    except Exception:
        return False
