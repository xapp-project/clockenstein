import datetime
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


class CalendarDatabase:
    """Shared calendar records. Each write changes only the records it owns."""

    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.environ.get("CLOCKENSTEIN_DATA_DIR")
                             or Path.home() / ".local/share/clockenstein")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self.path = self.data_dir / "calendars.db"
        with self.connection(write=True) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS accounts (
                provider TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                details TEXT NOT NULL, PRIMARY KEY (provider, id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS calendars (
                provider TEXT NOT NULL, account_id TEXT NOT NULL, id TEXT NOT NULL,
                name TEXT NOT NULL, color TEXT NOT NULL, visible INTEGER NOT NULL,
                reminders INTEGER NOT NULL, writable INTEGER NOT NULL,
                is_primary INTEGER NOT NULL, sync_range TEXT NOT NULL,
                last_sync INTEGER, sync_error TEXT NOT NULL, revision TEXT NOT NULL,
                PRIMARY KEY (provider, account_id, id),
                FOREIGN KEY (provider, account_id) REFERENCES accounts(provider, id)
                    ON DELETE CASCADE)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS events (
                provider TEXT NOT NULL, account_id TEXT NOT NULL, calendar_id TEXT NOT NULL,
                event_key TEXT NOT NULL, uid TEXT NOT NULL, summary TEXT NOT NULL,
                location TEXT NOT NULL, description TEXT NOT NULL,
                all_day INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                editable INTEGER NOT NULL, event_type TEXT NOT NULL, remote_url TEXT,
                PRIMARY KEY (provider, account_id, calendar_id, event_key),
                FOREIGN KEY (provider, account_id, calendar_id)
                    REFERENCES calendars(provider, account_id, id) ON DELETE CASCADE)""")
            connection.execute("CREATE INDEX IF NOT EXISTS events_uid ON events(provider, account_id, uid)")
        self.path.chmod(0o600)

    @contextmanager
    def connection(self, write=False):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def get_accounts(self, provider):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM accounts WHERE provider = ? ORDER BY rowid", (provider,)
            )
            accounts = []
            for row in rows:
                account = json.loads(row["details"])
                account["id"] = row["id"]
                account["name"] = row["name"]
                accounts.append(account)
            return accounts

    def connect_account(self, provider, account, calendars):
        details = {key: value for key, value in account.items() if key not in ("id", "name")}
        with self.connection(write=True) as connection:
            connection.execute("""INSERT INTO accounts VALUES (?, ?, ?, ?)
                ON CONFLICT (provider, id) DO UPDATE SET name=excluded.name, details=excluded.details""",
                       (provider, account["id"], account["name"], json.dumps(details)))
            self._save_calendar_list(connection, provider, account["id"], calendars)

    def disconnect_account(self, provider, account_id):
        with self.connection(write=True) as connection:
            connection.execute("DELETE FROM accounts WHERE provider=? AND id=?", (provider, account_id))

    def update_calendar_list(self, provider, account_id, calendars):
        # Discovery only updates metadata. Local preferences and sync results belong
        # to other operations, and must not be copied back from an earlier read.
        with self.connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM accounts WHERE provider=? AND id=?",
                          (provider, account_id)).fetchone():
                self._save_calendar_list(connection, provider, account_id, calendars)

    def _save_calendar_list(self, connection, provider, account_id, calendars):
        calendar_ids = {calendar["id"] for calendar in calendars}
        for row in connection.execute("SELECT id FROM calendars WHERE provider=? AND account_id=?",
                              (provider, account_id)).fetchall():
            if row["id"] not in calendar_ids:
                connection.execute("DELETE FROM calendars WHERE provider=? AND account_id=? AND id=?",
                           (provider, account_id, row["id"]))
        for calendar in calendars:
            # Use the supplied color for new calendars only. Existing colors
            # belong to local preferences, not remote sync metadata.
            connection.execute("""INSERT INTO calendars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, account_id, id) DO UPDATE SET
                    name=excluded.name, writable=excluded.writable,
                    is_primary=excluded.is_primary, revision=excluded.revision""",
                       (provider, account_id, calendar["id"], calendar.get("name", calendar["id"]),
                        calendar.get("color", "#4285f4"), calendar.get("visible", True),
                        calendar.get("reminders", True), calendar.get("writable", True),
                        calendar.get("primary", False), calendar.get("sync_range", "normal"),
                        calendar.get("last_sync"), calendar.get("sync_error", ""), uuid.uuid4().hex))

    def get_calendars(self, provider, account_id=None):
        with self.connection() as connection:
            query = """SELECT c.*, a.name AS account_name FROM calendars c JOIN accounts a
                ON c.provider=a.provider AND c.account_id=a.id WHERE c.provider=?"""
            parameters = [provider]
            if account_id is not None:
                query += " AND c.account_id=?"
                parameters.append(account_id)
            return [self._get_calendar_from_row(row) for row in connection.execute(query + " ORDER BY c.rowid", parameters)]

    @staticmethod
    def _get_calendar_from_row(row):
        calendar = dict(row)
        calendar["primary"] = bool(calendar.pop("is_primary"))
        for field in ("visible", "reminders", "writable"):
            calendar[field] = bool(calendar[field])
        return calendar

    def update_calendar(self, provider, account_id, calendar_id, **values):
        allowed_fields = {"name", "color", "visible", "reminders", "sync_error"}
        if not values or not values.keys() <= allowed_fields:
            raise ValueError("Invalid calendar fields")
        if "visible" in values:
            values["revision"] = uuid.uuid4().hex
        with self.connection(write=True) as connection:
            connection.execute("UPDATE calendars SET " + ", ".join(f"{key}=?" for key in values)
                       + " WHERE provider=? AND account_id=? AND id=?",
                       (*values.values(), provider, account_id, calendar_id))

    def ensure_local_calendar(self, name, color):
        with self.connection(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO accounts VALUES ('local', 'local', 'local', '{}')")
            if not connection.execute("SELECT 1 FROM calendars WHERE provider='local'").fetchone():
                self._save_calendar_list(connection, "local", "local", [{"id": "personal", "name": name,
                                                                 "color": color}])

    def create_local_calendar(self, base, name, color):
        with self.connection(write=True) as connection:
            used_calendar_ids = {row[0] for row in connection.execute("SELECT id FROM calendars WHERE provider='local'")}
            calendar_id, suffix = base, 2
            while calendar_id in used_calendar_ids:
                calendar_id = f"{base}-{suffix}"
                suffix += 1
            connection.execute("""INSERT INTO calendars VALUES
                ('local', 'local', ?, ?, ?, 1, 1, 1, 0, 'normal', NULL, '', ?)""",
                       (calendar_id, name, color, uuid.uuid4().hex))
        return next(calendar for calendar in self.get_calendars("local") if calendar["id"] == calendar_id)

    def delete_local_calendar(self, calendar_id):
        with self.connection(write=True) as connection:
            if connection.execute("SELECT COUNT(*) FROM calendars WHERE provider='local'").fetchone()[0] <= 1:
                return False
            connection.execute("DELETE FROM calendars WHERE provider='local' AND id=?", (calendar_id,))
        return True

    def get_events(self, provider, timezone, start=None, end=None, include_hidden=False,
               account_id=None, calendar_id=None, uid=None):
        with self.connection() as connection:
            query = """SELECT e.*, c.name AS calendar_name, c.color AS calendar_color,
                c.reminders, c.writable, c.sync_range, a.name AS account_name FROM events e
                JOIN calendars c ON e.provider=c.provider AND e.account_id=c.account_id
                    AND e.calendar_id=c.id
                JOIN accounts a ON e.provider=a.provider AND e.account_id=a.id
                WHERE e.provider=?"""
            parameters = [provider]
            if not include_hidden:
                query += " AND c.visible=1"
            for column, value in (("account_id", account_id), ("calendar_id", calendar_id), ("uid", uid)):
                if value is not None:
                    query += f" AND e.{column}=?"
                    parameters.append(value)
            result = []
            for row in connection.execute(query, parameters):
                event = self._get_event_from_row(row, timezone)
                if start and event["date_end"] < start:
                    continue
                if end and event["date_start"] > end:
                    continue
                result.append(event)
            return result

    @staticmethod
    def _get_event_from_row(row, timezone):
        event = dict(row)
        event["all_day"] = bool(event["all_day"])
        for field in ("start", "end"):
            value = event.pop(field)
            if event["all_day"]:
                event[f"date_{field}"] = datetime.date.fromisoformat(value)
                event[f"time_{field}"] = None
            else:
                event_datetime = datetime.datetime.fromisoformat(value)
                if event_datetime.tzinfo is not None:
                    event_datetime = event_datetime.astimezone(timezone)
                event[f"date_{field}"] = event_datetime.date()
                event[f"time_{field}"] = event_datetime.time().replace(tzinfo=None)
        writable = event.pop("writable")
        event["editable"] = bool(event["editable"] and writable)
        event["reminders"] = bool(event["reminders"])
        event["_caldav_url"] = event.pop("remote_url")
        return event

    @staticmethod
    def _put_event(connection, provider, account_id, calendar_id, event, timezone):
        dates = []
        for field in ("start", "end"):
            value = event.get(f"_{field}")
            if value is None:
                value = event[f"date_{field}"]
                if not event["all_day"]:
                    value = datetime.datetime.combine(value, event[f"time_{field}"], timezone)
            dates.append(value.isoformat())
        connection.execute("""INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (provider, account_id, calendar_id, event_key) DO UPDATE SET
                uid=excluded.uid, summary=excluded.summary, location=excluded.location,
                description=excluded.description, all_day=excluded.all_day, start=excluded.start,
                end=excluded.end, editable=excluded.editable, event_type=excluded.event_type,
                remote_url=excluded.remote_url""",
                   (provider, account_id, calendar_id, event.get("event_key", event["uid"]),
                    event["uid"], event.get("summary", ""), event.get("location", ""),
                    event.get("description", ""), event["all_day"], *dates,
                    event.get("editable", True), event.get("event_type", "default"),
                    event.get("_caldav_url")))

    def save_event(self, provider, account_id, calendar_id, event, timezone, source_id=None):
        with self.connection(write=True) as connection:
            if source_id and source_id != calendar_id:
                connection.execute("DELETE FROM events WHERE provider=? AND account_id=? AND calendar_id=? AND uid=?",
                           (provider, account_id, source_id, event["uid"]))
                self._touch(connection, provider, account_id, source_id)
            self._put_event(connection, provider, account_id, calendar_id, event, timezone)
            self._touch(connection, provider, account_id, calendar_id)

    def delete_event(self, provider, account_id, calendar_id, uid):
        with self.connection(write=True) as connection:
            count = connection.execute("DELETE FROM events WHERE provider=? AND account_id=? AND calendar_id=? AND uid=?",
                               (provider, account_id, calendar_id, uid)).rowcount
            self._touch(connection, provider, account_id, calendar_id)
            return bool(count)

    def clear_events(self, provider, account_id, calendar_id):
        with self.connection(write=True) as connection:
            connection.execute("DELETE FROM events WHERE provider=? AND account_id=? AND calendar_id=?",
                       (provider, account_id, calendar_id))
            self._touch(connection, provider, account_id, calendar_id)

    @staticmethod
    def _touch(connection, provider, account_id, calendar_id):
        connection.execute("UPDATE calendars SET revision=? WHERE provider=? AND account_id=? AND id=?",
                   (uuid.uuid4().hex, provider, account_id, calendar_id))

    def apply_sync(self, calendar, events, start, end, timezone, sync_range="normal"):
        provider, account_id, calendar_id = calendar["provider"], calendar["account_id"], calendar["id"]
        with self.connection(write=True) as connection:
            current = connection.execute("SELECT revision, visible FROM calendars WHERE provider=? AND account_id=? AND id=?",
                                 (provider, account_id, calendar_id)).fetchone()
            # An edit, hide/show, disconnect or reconnect happened during the download.
            # Keep the newer local state; another refresh can fetch current remote data.
            if not current or current["revision"] != calendar["revision"] or not current["visible"]:
                return False
            rows = connection.execute("SELECT * FROM events WHERE provider=? AND account_id=? AND calendar_id=?",
                              (provider, account_id, calendar_id)).fetchall()
            for row in rows:
                overlaps = sync_range == "too-big"
                if not overlaps:
                    dates = []
                    for field in ("start", "end"):
                        if row["all_day"]:
                            dates.append(datetime.date.fromisoformat(row[field]))
                        else:
                            event_datetime = datetime.datetime.fromisoformat(row[field])
                            if event_datetime.tzinfo is not None:
                                event_datetime = event_datetime.astimezone(timezone)
                            dates.append(event_datetime.date())
                    overlaps = dates[1] >= start and dates[0] <= end
                if overlaps:
                    connection.execute("DELETE FROM events WHERE provider=? AND account_id=? AND calendar_id=? AND event_key=?",
                               (provider, account_id, calendar_id, row["event_key"]))
            for event in events:
                self._put_event(connection, provider, account_id, calendar_id, event, timezone)
            connection.execute("""UPDATE calendars SET sync_range=?, last_sync=?, sync_error='', revision=?
                WHERE provider=? AND account_id=? AND id=?""",
                       (sync_range, int(datetime.datetime.now().timestamp()), uuid.uuid4().hex,
                        provider, account_id, calendar_id))
            return True
