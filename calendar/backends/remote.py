from clockenstein.calendars import CalendarDatabase
from clockenstein.sync import SyncDownload


class RemoteBackend:
    """Common persistence and display model; subclasses handle remote requests."""

    def __init__(self, data_dir, timezone):
        self.timezone = timezone
        self.database = CalendarDatabase(data_dir)

    @property
    def accounts(self):
        return self.database.get_accounts(self.provider)

    @property
    def has_accounts(self):
        return bool(self.accounts)

    def list_calendars(self):
        calendars = self.database.get_calendars(self.provider)
        for calendar in calendars:
            calendar["available"] = (self._account_available(calendar["account_id"])
                                     and calendar["sync_range"] != "too-big")
        return calendars

    def set_visible(self, calendar_id, visible, account_id=None):
        self.database.update_calendar(self.provider, account_id, calendar_id, visible=bool(visible))

    def set_reminders(self, calendar_id, enabled, account_id=None):
        self.database.update_calendar(self.provider, account_id, calendar_id, reminders=bool(enabled))

    def set_color(self, calendar_id, color, account_id):
        self.database.update_calendar(self.provider, account_id, calendar_id, color=color)

    def clear_calendar_events(self, calendar_id, account_id):
        self.database.clear_events(self.provider, account_id, calendar_id)

    def get_events(self, start=None, end=None, include_hidden=False):
        events = self.database.get_events(self.provider, self.timezone, start, end, include_hidden)
        for event in events:
            online = self._account_available(event["account_id"])
            event["editable"] = event["editable"] and online
            event["cached"] = not online
        return events

    def _sync_failed(self, account_id, error, calendar_id=None, start=None, end=None):
        for calendar in self.database.get_calendars(self.provider, account_id):
            if calendar_id is None or calendar["id"] == calendar_id:
                self.database.update_calendar(self.provider, account_id, calendar["id"],
                                              sync_error=str(error))
                if start is not None:
                    download = SyncDownload(self.database.data_dir, self.provider, account_id,
                                            calendar["id"], start, end)
                    download.finish(error)
