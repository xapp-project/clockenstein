def get_calendar_key(record):
    """Identify a calendar across providers and accounts.

    Accepts a calendar record (with "id") or an event (with "calendar_id").
    """
    return ":".join((record.get("provider", "local"),
                     record.get("account_id", "local"),
                     record.get("id", record.get("calendar_id", ""))))
