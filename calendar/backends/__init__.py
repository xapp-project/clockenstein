"""Static configuration shared by every calendar backend."""

import gettext
from collections.abc import Iterable
from dataclasses import dataclass, replace

from xapp.util import l10n

l10n("clockenstein")


def N_(message):
    """Mark a string for translation; it is translated where it is displayed."""
    return message


@dataclass(frozen=True)
class BackendConfig:
    # Stable identifier stored in the database and used over D-Bus.
    provider: str
    # Untranslated display name; pass it through _() before showing it.
    label: str
    # Seconds between automatic syncs; None when the backend is never synced.
    sync_interval: int | None


DEFAULT_CONFIG = BackendConfig(provider="", label="", sync_interval=15 * 60)

LOCAL_CONFIG = replace(
    DEFAULT_CONFIG, provider="local", label=N_("Local"), sync_interval=None
)
GOOGLE_CONFIG = replace(
    DEFAULT_CONFIG, provider="google", label="Google", sync_interval=2 * 60 * 60
)
CALDAV_CONFIG = replace(
    DEFAULT_CONFIG, provider="caldav", label=N_("CalDAV (Nextcloud, Memotoo, etc.)")
)
ICS_CONFIG = replace(
    DEFAULT_CONFIG, provider="ics", label=N_("Internet calendar (ICS link)")
)

# Display order.
BACKENDS = {
    config.provider: config
    for config in (LOCAL_CONFIG, GOOGLE_CONFIG, CALDAV_CONFIG, ICS_CONFIG)
}
REMOTE_PROVIDERS = tuple(
    provider for provider, config in BACKENDS.items() if config.sync_interval
)


def get_refresh_schedule(
    configs: Iterable[BackendConfig] = BACKENDS.values(),
) -> dict[int, frozenset[str]]:
    """Group synced providers by interval, so each interval needs a single timer."""
    schedule: dict[int, set[str]] = {}
    for config in configs:
        if config.sync_interval:
            schedule.setdefault(config.sync_interval, set()).add(config.provider)
    return {interval: frozenset(providers) for interval, providers in schedule.items()}


def format_sync_interval(seconds: int) -> str:
    minutes = seconds // 60
    if minutes % 60 == 0:
        hours = minutes // 60
        message = gettext.ngettext("Sync every %d hour", "Sync every %d hours", hours)
        return message % hours
    message = gettext.ngettext("Sync every %d minute", "Sync every %d minutes", minutes)
    return message % minutes
