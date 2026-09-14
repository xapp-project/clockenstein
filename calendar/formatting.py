import locale
import re

from gi.repository import Gio
from xapp.util import l10n

_ = l10n("clockenstein")

WEEKDAY_NAMES = (_("MON"), _("TUE"), _("WED"), _("THU"), _("FRI"), _("SAT"), _("SUN"))

# The desktop's "System" 12/24-hour choice (Date & Time settings) is a separate
# knob from the process locale, so it has to be read from GSettings directly.
# Different desktops expose it under different schemas/keys.
_CLOCK_FORMAT_SOURCES = (
    ("org.cinnamon.desktop.interface", "clock-use-24h", "bool"),
    ("org.gnome.desktop.interface", "clock-format", "24h-string"),
)


def _find_clock_format_setting():
    schemas = Gio.Settings.list_schemas()
    for schema, key, kind in _CLOCK_FORMAT_SOURCES:
        if schema in schemas:
            return Gio.Settings.new(schema), key, kind
    return None, None, None


_clock_settings, _clock_key, _clock_kind = _find_clock_format_setting()
_clock_format_listeners = []


def on_clock_format_changed(callback):
    """Register a callback invoked whenever the system 12/24-hour setting changes."""
    _clock_format_listeners.append(callback)


def _notify_clock_format_listeners(*_args):
    for callback in list(_clock_format_listeners):
        callback()


if _clock_settings is not None:
    _clock_settings.connect(f"changed::{_clock_key}", _notify_clock_format_listeners)


def capitalize_first(value):
    return value[:1].upper() + value[1:]


def uses_12_hour_clock():
    if _clock_settings is not None:
        if _clock_kind == "bool":
            return not _clock_settings.get_boolean(_clock_key)
        return _clock_settings.get_string(_clock_key) != "24h"

    pattern = locale.nl_langinfo(locale.T_FMT)
    return "%I" in pattern or "%r" in pattern


def format_time(value):
    if uses_12_hour_clock():
        return value.strftime("%l:%M%P").strip()

    pattern = locale.nl_langinfo(locale.T_FMT)
    if "%I" in pattern or "%r" in pattern:
        pattern = "%H:%M"
    else:
        pattern = pattern.replace("%T", "%H:%M:%S")
        pattern = re.sub(r"([:.])?%S", "", pattern)
    return value.strftime(pattern).strip()
