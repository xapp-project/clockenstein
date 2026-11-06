import datetime


def get_calendar_key(record):
    """Identify a calendar across providers and accounts.

    Accepts a calendar record (with "id") or an event (with "calendar_id").
    """
    return ":".join((record.get("provider", "local"),
                     record.get("account_id", "local"),
                     record.get("id", record.get("calendar_id", ""))))


def get_event_uid(event):
    """The id the daemon hands to the applet for this event."""
    return get_calendar_key(event) + ":" + event["uid"]


def get_event_last_day(event):
    """Return the last local date an event occupies.

    A timed event ending exactly at midnight doesn't occupy the day it ends on.
    """
    date_start = event["date_start"]
    date_end = event.get("date_end") or date_start
    if not event.get("all_day") and date_end > date_start and event.get("time_end") == datetime.time.min:
        return date_end - datetime.timedelta(days=1)
    return date_end


def is_event_all_day(event):
    """Return whether an event presents as all-day.

    This includes timed events spanning whole local days, from midnight to midnight.
    """
    if event.get("all_day") or event.get("time_start") is None:
        return True
    date_end = event.get("date_end") or event["date_start"]
    return event["time_start"] == event.get("time_end") == datetime.time.min and date_end > event["date_start"]
