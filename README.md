# Clockenstein

Clock and Calendar applications for Linux desktops.

<img width="1317" height="737" alt="image" src="https://github.com/user-attachments/assets/97f35ad3-e434-49cc-9ab1-1c37b216ecc7" />

## Supported Calendars

- Local calendars.
- Google calendars
- CalDAV calendars (Nextcloud, Memotoo, etc)

Remote calendars are read-only when disconnected or offline.

Recurring CalDAV events share the same UID for all their instances. So
in Clockenstein, these are read-only to prevent CRUD operations from
affecting all instances at once.

## Architecture and synchronization

`clockenstein-calendar` and `clockenstein-clocks` are the client applications.

They save their changes to the shared stores and notify the daemon when it needs to reload them.

`clockenstein-daemon` runs in the background, syncs remote calendars, schedules
event reminders and alarms, and emits them over D-Bus. The client windows do not
need to stay open for reminders or alarms to ring.

Reminders are managed by Clockenstein, remote reminders are not supported.

All-day events do not trigger reminders.

The daemon:

- writes to `.xsession-errors`.
- becomes verbose if the `gsettings` key `org.x.clockenstein verbose` is set to `true`
- is restarted on package updates (this is done in `debian/postinst`)
- handles all interactions with remote (Google, Caldav) servers except for CRUD operations and accounts setup (which are handled by the client)
- syncs remote events on startup and then on a regular basis
- communicates to clients via DBUS to tell when something has `Changed` or to accept or queue refresh requests

`clockenstein-notification-agent` runs in the background and listens for reminder
and alarm signals from the daemon. It displays notification windows with dismiss,
snooze and mute controls, and plays the selected sound. Both the daemon and agent
need to be running for alarms to ring.

Just like the daemon, it is started via XDG autostart, and it runs as a systemd user service which is respawned automatically when it dies.

`clockenstein-cli` is a CLI tool for troubleshooting. Mainly to query the databases in read-only mode.

Database contents can include personal event details, so review output before sharing it.

`tools/dbus-calendar-client.py` simulates an applet which shows calendar events (similar to the Cinnamon clock applet)

Clockenstein stores its data in `~/.local/share/clockenstein`:

- Alarms are stored in `alarms.db`, a SQLite database shared by Clocks and the daemon.
- Local calendars, Google and CalDAV accounts, calendar preferences and events
  are stored in `calendars.db`, a SQLite database shared by Calendar and the daemon.
- Google OAuth credentials are stored separately in `google/`. Accounts connected
  through Online Accounts use its credentials instead. CalDAV passwords are kept
  in the desktop keyring.

Calendar and the daemon update individual database records. A remote sync updates
the fetched calendar's events and sync status without rewriting account settings
or calendar preferences. Remote event edits are sent to the server before the
local database is updated.

### Troubleshooting remote synchronization

`sync/<provider>/<account-hash>/<calendar-hash>/latest.json` contains the latest
completed event download for each remote calendar. The file identifies the account
and calendar, when it was fetched and the requested date ranges. Google responses
are saved as returned JSON pages; CalDAV responses contain each resource's URL and
original iCalendar text. These are the libraries' responses, not HTTP traffic.
No authorization headers or OAuth credentials are included.

Downloads are saved before conversion to the shared event model, so data that
cannot be parsed is available for inspection. `status.json` records the latest
attempt's outcome. Failed downloads retain the previous `latest.json`; successful
downloads replace it rather than accumulating history. These files are diagnostic
copies, not the event cache used by Calendar.

The data directory is private to its owner. Raw downloads contain personal event
details: review them before sharing a bug report. Diagnostic copies are retained
when an account is disconnected and can be removed from `sync/` manually.

### Limits, synchronization frequencies and ranges

There are no limits for local calendars.

It's important to keep the the number of requests low when it comes to the Google API because
we share one key for all users.

We sync Google every 2 hours.

To limit API requests, we only read the list of Google calendars during account
setup. Event refreshes do not discover new calendars or update calendar names
and colours changed in Google after setup.

We want a maximum of 2500 events per Google calendar in order to be able to sync in a single
API request.

Calendars with less than 2500 events get a sync range of 2 years. If the number of events
is larger than 2500, we reduce this to 1 year and try again, then 3 months and eventually
we refuse to sync the calendar.

CalDav is different because it's a different connection for each user.
We sync it every 15 minutes for a range of 2 years.

When we navigate outside the range, in the case of Google no events are shown, in the case of
CalDav we sync extra ranges from the remote.

## Building from source

### For Mint with mint-dev-tools

```bash
# Install mint-dev-tools
apt install mint-dev-tools
# Remove any previous versions
apt remove 'clockenstein*'
# Build and install from github
mint-build -i -g https://github.com/xapp-project/clockenstein.git
```

### For Debian distributions (Mint, Ubuntu, etc.) with dpkg-buildpackage

```bash
# Get the source code..
git clone https://github.com/xapp-project/clockenstein.git
# Go in..
cd clockenstein
# Install the build dependencies..
sudo apt build-dep --mark-auto .
# Remove any previously built packages
rm -f ../clockenstein*.deb
# Build
dpkg-buildpackage
# Install
sudo apt install ../clockenstein*.deb
```

### For other distributions with meson

```bash
# Get the source code..
git clone https://github.com/xapp-project/clockenstein.git
# Go in..
cd clockenstein
```

Install the build and runtime dependencies for your distribution. For example:

```bash
# Fedora: sudo dnf install meson ninja-build python3 gettext
# Arch: sudo pacman -S meson ninja python gettext
# openSUSE: sudo zypper install meson ninja python3 gettext-tools
```

The dependencies are listed below (using debian pkg names, names may be different in your distribution). Install all of them.

#### Dependencies for building and runtime

```text
gir1.2-gsound-1.0
gir1.2-gtk-3.0
python3
python3-babel
python3-caldav
python3-gi
python3-google-auth-httplib2
python3-google-auth-oauthlib
python3-googleapi
python3-icalendar
python3-pycurl
python3-requests
python3-setproctitle
python3-xapp
```

#### Dependencies for building

```text
gettext
libglib2.0-dev or libgio-2.0-dev
meson
pkg-config
```

#### Dependencies for runtime

```text
gir1.2-secret-1
python3-rich
xapp-symbolic-icons
```

#### Build and install

```bash
meson setup build --prefix=/usr/local
meson compile -C build
sudo meson install -C build
```

#### Uninstall

To remove a Meson installation while retaining the build directory:

```bash
sudo ninja -C build uninstall
```

## Translations

Please use Launchpad to translate this project: https://translations.launchpad.net/linuxmint/latest/.

The PO files in this project are imported from there.

## License

Code: GPLv3
