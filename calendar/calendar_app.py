#!/usr/bin/python3
import datetime
import os
import sys
from setproctitle import setproctitle
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib
from xapp.util import l10n

_ = l10n("clockenstein")

from main_window import MainWindow


def _activate(application):
    windows = application.get_windows()
    if windows:
        windows[0].present()
        return

    css_provider = Gtk.CssProvider()
    css_path = os.path.join(os.path.dirname(__file__), "style.css")
    if os.path.exists(css_path):
        css_provider.load_from_path(css_path)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    window = MainWindow()
    application.add_window(window)
    window.show_all()


def _command_line(application, command_line):
    arguments = [arg.decode() if isinstance(arg, bytes) else arg
                 for arg in command_line.get_arguments()]
    requested_date = None
    requested_event = None
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument.startswith("--date="):
            value = argument.partition("=")[2]
            try:
                requested_date = datetime.date.fromisoformat(value)
            except ValueError:
                command_line.printerr(f"Invalid date: {value}\n")
                return 2
        elif argument == "--date":
            if index + 1 >= len(arguments):
                command_line.printerr("The --date option requires a date.\n")
                return 2
            index += 1
            try:
                requested_date = datetime.date.fromisoformat(arguments[index])
            except ValueError:
                command_line.printerr(f"Invalid date: {arguments[index]}\n")
                return 2
        elif argument.startswith("--event="):
            requested_event = argument.partition("=")[2]
        elif argument == "--event":
            if index + 1 >= len(arguments):
                command_line.printerr("The --event option requires an event id.\n")
                return 2
            index += 1
            requested_event = arguments[index]
        index += 1

    application.activate()
    window = application.get_windows()[0]
    if requested_event is not None:
        # The Cinnamon applet passes the event id it got from the daemon.
        window.open_event(requested_event)
    elif requested_date is not None:
        window.focus_date(requested_date)
    window.present()
    return 0


def main():
    setproctitle("clockenstein-calendar")
    GLib.set_prgname("org.x.clockenstein.Calendar")
    GLib.set_application_name(_("Calendar"))
    application = Gtk.Application(
        application_id="org.x.clockenstein.Calendar",
        flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
    )
    application.connect("activate", _activate)
    application.connect("command-line", _command_line)
    return application.run(sys.argv)


if __name__ == "__main__":
    main()
