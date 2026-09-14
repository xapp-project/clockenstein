import datetime
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from icalendar import Calendar, Event
from xapp.util import l10n

_ = l10n("clockenstein")


def _data_dir() -> Path:
    override = os.environ.get("CLOCKENSTEIN_DATA_DIR")
    return Path(override) if override else Path.home() / ".local" / "share" / "clockenstein"


DEFAULT_COLOR = "#2aa198"


class LocalStore:
    """One ICS file per local calendar, with a small JSON calendar registry."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else _data_dir()
        self.calendars_dir = self.data_dir / "calendars"
        self.registry_file = self.data_dir / "calendars.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.calendars_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_file()
        self._registry = self._load_registry()
        if not self._registry:
            self.create_calendar(_("Personal"), DEFAULT_COLOR, calendar_id="personal")

    def _migrate_legacy_file(self):
        legacy = self.data_dir / "calendar.ics"
        target = self.calendars_dir / "personal.ics"
        if legacy.exists() and not target.exists():
            shutil.copy2(legacy, target)

    def _load_registry(self):
        try:
            value = json.loads(self.registry_file.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            if (self.calendars_dir / "personal.ics").exists():
                return [self._calendar_info("personal", _("Personal"), DEFAULT_COLOR)]
            return []

    def _save_registry(self):
        temp = self.registry_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self._registry, indent=2), encoding="utf-8")
        temp.replace(self.registry_file)

    @staticmethod
    def _calendar_info(calendar_id, name, color):
        return {"id": calendar_id, "name": name, "color": color,
                "provider": "local", "account_id": "local", "account_name": "local",
                "visible": True, "reminders": True,
                "writable": True, "available": True}

    def list_calendars(self) -> list[dict]:
        return [dict(item) for item in self._registry]

    def create_calendar(self, name: str, color: str = DEFAULT_COLOR,
                        calendar_id: Optional[str] = None) -> dict:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "calendar"
        candidate = calendar_id or base
        used = {item["id"] for item in self._registry}
        suffix = 2
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        info = self._calendar_info(candidate, name.strip() or _("Calendar"), color)
        self._registry.append(info)
        self._path(candidate).write_bytes(self._new_calendar().to_ical())
        self._save_registry()
        return dict(info)

    def set_visible(self, calendar_id: str, visible: bool, _account_id=None):
        for item in self._registry:
            if item["id"] == calendar_id:
                item["visible"] = bool(visible)
                self._save_registry()
                return

    def set_reminders(self, calendar_id: str, enabled: bool, _account_id=None):
        for item in self._registry:
            if item["id"] == calendar_id:
                item["reminders"] = bool(enabled)
                self._save_registry()
                return

    def update_calendar(self, calendar_id: str, name: str, color: str):
        calendar = next((item for item in self._registry
                         if item["id"] == calendar_id), None)
        if calendar is None:
            raise KeyError(_("Unknown calendar %s") % calendar_id)
        calendar["name"] = name.strip() or _("Calendar")
        calendar["color"] = color
        self._save_registry()
        return dict(calendar)

    def delete_calendar(self, calendar_id: str):
        if len(self._registry) <= 1:
            raise ValueError(_("At least one local calendar is required"))
        index = next((i for i, item in enumerate(self._registry)
                      if item["id"] == calendar_id), None)
        if index is None:
            raise KeyError(_("Unknown calendar %s") % calendar_id)
        self._path(calendar_id).unlink(missing_ok=True)
        del self._registry[index]
        self._save_registry()

    def get_events(self, start=None, end=None, include_hidden=False) -> list[dict]:
        results = []
        for info in self._registry:
            if not include_hidden and not info.get("visible", True):
                continue
            for component in self._load_calendar(info["id"]).walk():
                if component.name != "VEVENT":
                    continue
                ev = _component_to_dict(component)
                if start and ev["date_end"] < start:
                    continue
                if end and ev["date_start"] > end:
                    continue
                ev.update(calendar_id=info["id"], calendar_name=info["name"],
                          calendar_color=info["color"], provider="local",
                          account_id="local", editable=True,
                          reminders=info.get("reminders", True))
                results.append(ev)
        return sorted(results, key=_event_sort_key)

    def create_event(self, data: dict) -> dict:
        calendar_id = data.get("calendar_id") or self._registry[0]["id"]
        cal = self._load_calendar(calendar_id)
        uid = data.get("uid") or str(uuid.uuid4())
        ev = Event()
        ev.add("uid", uid)
        ev.add("dtstamp", datetime.datetime.now(datetime.timezone.utc))
        ev.add("created", datetime.datetime.now(datetime.timezone.utc))
        _apply_data(ev, data)
        cal.add_component(ev)
        self._save_calendar(calendar_id, cal)
        return next(e for e in self.get_events() if e["uid"] == uid and e["calendar_id"] == calendar_id)

    def update_event(self, uid: str, data: dict) -> Optional[dict]:
        source_id = data.get("original_calendar_id") or self._find_calendar(uid)
        calendar_id = data.get("calendar_id") or source_id
        source = self._load_calendar(source_id)
        component = self._find(source, uid)
        if component is None:
            return None
        ev = Event()
        for key in ("uid", "dtstamp", "created"):
            if key.upper() in component:
                ev.add(key, component[key.upper()])
        ev.add("last-modified", datetime.datetime.now(datetime.timezone.utc))
        _apply_data(ev, data)
        source.subcomponents = [c for c in source.subcomponents
                                if not (c.name == "VEVENT" and str(c.get("uid", "")) == uid)]
        if calendar_id == source_id:
            source.add_component(ev)
            self._save_calendar(source_id, source)
        else:
            destination = self._load_calendar(calendar_id)
            destination.add_component(ev)
            self._save_calendar(calendar_id, destination)
            self._save_calendar(source_id, source)
        return next((e for e in self.get_events() if e["uid"] == uid and e["calendar_id"] == calendar_id), None)

    def delete_event(self, uid: str, calendar_id: Optional[str] = None, _account_id=None) -> bool:
        calendar_id = calendar_id or self._find_calendar(uid)
        cal = self._load_calendar(calendar_id)
        before = len(cal.subcomponents)
        cal.subcomponents = [c for c in cal.subcomponents
                             if not (c.name == "VEVENT" and str(c.get("uid", "")) == uid)]
        if len(cal.subcomponents) == before:
            return False
        self._save_calendar(calendar_id, cal)
        return True

    def _find_calendar(self, uid):
        for info in self._registry:
            if self._find(self._load_calendar(info["id"]), uid):
                return info["id"]
        raise KeyError(_("Unknown event %s") % uid)

    @staticmethod
    def _find(cal, uid):
        return next((c for c in cal.walk()
                     if c.name == "VEVENT" and str(c.get("uid", "")) == uid), None)

    def _path(self, calendar_id):
        return self.calendars_dir / f"{calendar_id}.ics"

    @staticmethod
    def _new_calendar():
        cal = Calendar()
        cal.add("prodid", "-//Clockenstein//EN")
        cal.add("version", "2.0")
        cal.add("calscale", "GREGORIAN")
        return cal

    def _load_calendar(self, calendar_id):
        path = self._path(calendar_id)
        if not path.exists():
            return self._new_calendar()
        try:
            return Calendar.from_ical(path.read_bytes())
        except Exception as exc:
            raise RuntimeError(_("Could not read %s: %s") % (path.name, exc)) from exc

    def _save_calendar(self, calendar_id, cal):
        path = self._path(calendar_id)
        temp = path.with_suffix(".tmp")
        temp.write_bytes(cal.to_ical())
        temp.replace(path)


class CalendarManager:
    """Aggregate local calendars and optional online accounts for the UI."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.local = LocalStore(data_dir)
        from backends.google import GoogleBackend
        from backends.caldav import CalDAVBackend
        self.google = GoogleBackend(self.local.data_dir / "google")
        self.caldav = CalDAVBackend(self.local.data_dir / "caldav")

    def list_calendars(self):
        return (self.local.list_calendars() + self.google.list_calendars()
                + self.caldav.list_calendars())

    def get_events(self, start=None, end=None, include_hidden=False):
        return sorted(
            self.local.get_events(start, end, include_hidden)
            + self.google.get_events(start, end, include_hidden)
            + self.caldav.get_events(start, end, include_hidden),
            key=_event_sort_key,
        )

    def create_calendar(self, name, color=DEFAULT_COLOR):
        return self.local.create_calendar(name, color)

    def delete_local_calendar(self, calendar_id):
        return self.local.delete_calendar(calendar_id)

    def update_local_calendar(self, calendar_id, name, color):
        return self.local.update_calendar(calendar_id, name, color)

    def set_visible(self, provider, calendar_id, visible, account_id=None):
        self._backend(provider).set_visible(calendar_id, visible, account_id)

    def set_reminders(self, provider, calendar_id, enabled, account_id=None):
        self._backend(provider).set_reminders(calendar_id, enabled, account_id)

    def writable_calendars(self):
        return [c for c in self.list_calendars() if c.get("writable") and c.get("available")]

    def create_event(self, data):
        return self._backend(data.get("provider", "local")).create_event(data)

    def update_event(self, uid, data):
        return self._backend(data.get("provider", "local")).update_event(uid, data)

    def delete_event(self, uid, calendar_id=None, provider="local", account_id=None):
        return self._backend(provider).delete_event(uid, calendar_id, account_id)

    def _backend(self, provider):
        return {"local": self.local, "google": self.google, "caldav": self.caldav}[provider]

    @property
    def has_remote_accounts(self):
        return self.google.has_accounts or self.caldav.has_accounts

    def refresh_remote(self, start, end, google_limited_range=None,
                       google_restricted_range=None):
        return (self.google.refresh(start, end, google_limited_range,
                                    google_restricted_range)
                + self.caldav.refresh(start, end))


def _event_sort_key(e):
    return e["date_start"], e.get("time_start") or datetime.time.min


def _apply_data(ev: Event, data: dict):
    ev.add("summary", data.get("summary", ""))
    if data.get("location"):
        ev.add("location", data["location"])
    if data.get("description"):
        ev.add("description", data["description"])
    date_start = data.get("date_start") or datetime.date.today()
    date_end = data.get("date_end") or date_start
    if data.get("all_day", True):
        ev.add("dtstart", date_start)
        ev.add("dtend", date_end + datetime.timedelta(days=1))
    else:
        tz = datetime.datetime.now().astimezone().tzinfo
        ev.add("dtstart", datetime.datetime.combine(date_start, data.get("time_start") or datetime.time(9)).replace(tzinfo=tz))
        ev.add("dtend", datetime.datetime.combine(date_end, data.get("time_end") or datetime.time(10)).replace(tzinfo=tz))


def _component_to_dict(component) -> dict:
    dtstart = component.get("dtstart").dt
    dtend = component.get("dtend").dt if component.get("dtend") else dtstart
    all_day = isinstance(dtstart, datetime.date) and not isinstance(dtstart, datetime.datetime)
    if all_day:
        date_start, date_end = dtstart, dtend - datetime.timedelta(days=1)
        time_start = time_end = None
    else:
        # dtstart/dtend carry whatever timezone the source recorded (e.g. a
        # Nextcloud event stored in UTC). Convert to local wall-clock time
        # before dropping tzinfo, otherwise the displayed time is wrong.
        dtstart, dtend = dtstart.astimezone(), dtend.astimezone()
        date_start, date_end = dtstart.date(), dtend.date()
        time_start, time_end = dtstart.time(), dtend.time()
    return {"uid": str(component.get("uid", "")), "summary": str(component.get("summary", "")),
            "location": str(component.get("location", "")), "description": str(component.get("description", "")),
            "all_day": all_day, "date_start": date_start, "date_end": date_end,
            "time_start": time_start, "time_end": time_end}
