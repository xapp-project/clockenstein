import datetime
import base64
import hashlib
import json
import httplib2
import zlib
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from xapp.util import l10n

_ = l10n("clockenstein")


SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
EVENTS_PAGE_SIZE = 2500
NORMAL_RANGE = (2 * 31, 2 * 365)
LIMITED_RANGE = (31, 365)
RESTRICTED_RANGE = (31, 3 * 31)
SYNC_RANGES = {
    "normal": NORMAL_RANGE,
    "limited": LIMITED_RANGE,
    "restricted": RESTRICTED_RANGE,
}


class GoogleUnavailable(RuntimeError):
    pass


class GoogleBackend:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_file = self.data_dir / "accounts.json"
        self.accounts = self._read_json(self.accounts_file, [])
        self._services = {}
        self._credentials = {}
        self._errors = {}
        self.last_refresh_stats = {}
        self._load_services()

    @property
    def has_accounts(self):
        return bool(self.accounts)

    def account_states(self):
        return [{"id": a["id"], "name": a.get("name", a["id"]),
                 "online": self._account_available(a["id"]),
                 "error": self._errors.get(a["id"], "")} for a in self.accounts]

    def _account_available(self, account_id):
        return (account_id in self._services
                or account_id in self._credentials and account_id not in self._errors)

    def connect(self, progress=None) -> str:
        scopes = self._scopes_for_credentials()
        if progress:
            progress(_("Waiting for Google authorization…"))
        flow = InstalledAppFlow.from_client_config(
            self._read_oauth_client_config(), scopes
        )
        creds = flow.run_local_server(port=0, authorization_prompt_message="Opening Google sign-in…",
                                      prompt="select_account consent")
        if progress:
            progress(_("Authorization received • Contacting Google Calendar…"))
        service = self._build_service(creds)
        if progress:
            progress(_("Loading your Google calendars…"))
        calendars = self._fetch_calendars(service)
        primary = next((c for c in calendars if c.get("primary")), None)
        if not primary:
            raise GoogleUnavailable(_("Google did not return a primary calendar"))
        account_id = primary["id"]
        token_name = hashlib.sha256(account_id.encode()).hexdigest()[:20] + ".json"
        (self.data_dir / token_name).write_text(self._credentials_json(creds), encoding="utf-8")
        account = next((a for a in self.accounts if a["id"] == account_id), None)
        if account is None:
            account = {"id": account_id, "name": account_id, "token": token_name,
                       "calendars": [], "events": [], "scopes": scopes,
                       "auth_provider": "clockenstein"}
            self.accounts.append(account)
        account["auth_provider"] = "clockenstein"
        account["token"] = token_name
        account["scopes"] = scopes
        account["calendars"] = self._merge_calendar_preferences(account.get("calendars", []), calendars)
        self._services[account_id] = service
        self._credentials[account_id] = creds
        self._errors.pop(account_id, None)
        self._save()
        return account_id

    @staticmethod
    def _read_oauth_client_config():
        encoded = base64.b85decode(b'8@4j@wlA$SWZ<?7f(e^H$eqI2bw}&>AS|KY@<(t|_sdT#y}Viz_ti_O<nc(Mn7ag?AzR_cTGf5n$u%!6ZvBJ&WA|JA?^lBYZAU-e;}Li}i#{Mjy!no1w@Jf<)h9&r@qrMtS;H?+XAxlEw&QV&1jwNF16euHOW#x7O6dSCSl{?~`zjMq1|*NN$PYAL>Md?azkP+dq&;?;hyis{N2?Vn=!o~;9p@Y(v+bGZDu<&Au&7I@dMg&=kf}A|gl}7On9&l2eAbfoe?(-bjh?CK<HCe&!<Gl2jUi{>CKQ^6Zw8W;!pyw>=@Pk1VpTw+e~STAIiG$>CI@S}_k<@OcWV*CvgmL')
        scrambled = bytes(
            value ^ b"clockenstein-desktop-oauth"[index % len(b"clockenstein-desktop-oauth")]
            for index, value in enumerate(encoded)
        )
        return json.loads(zlib.decompress(scrambled).decode("utf-8"))

    def list_goa_accounts(self):
        result = []
        for goa_object in self._goa_accounts():
            account = goa_object.get_account()
            result.append({
                "id": account.props.id,
                "name": account.props.presentation_identity or account.props.id,
            })
        return result

    def connect_goa(self, goa_account_id, progress=None):
        goa_object = self._find_goa_account(goa_account_id)
        goa_account = goa_object.get_account()
        if progress:
            progress(_("Requesting authorization from Online Accounts…"))
        service = self._build_goa_service(goa_object)
        if progress:
            progress(_("Loading your Google calendars…"))
        calendars = self._fetch_calendars(service)
        primary = next((calendar for calendar in calendars if calendar.get("primary")), None)
        if not primary:
            raise GoogleUnavailable(_("Google did not return a primary calendar"))
        account_id = f"goa:{goa_account_id}"
        account = next((item for item in self.accounts if item["id"] == account_id), None)
        if account is None:
            account = {
                "id": account_id,
                "name": goa_account.props.presentation_identity or primary["id"],
                "auth_provider": "goa",
                "goa_account_id": goa_account_id,
                "calendars": [],
                "events": [],
            }
            self.accounts.append(account)
        account["auth_provider"] = "goa"
        account["goa_account_id"] = goa_account_id
        account["name"] = goa_account.props.presentation_identity or primary["id"]
        account["calendars"] = self._merge_calendar_preferences(
            account.get("calendars", []), calendars
        )
        self._services[account_id] = service
        self._errors.pop(account_id, None)
        self._save()
        return account_id

    def disconnect(self, account_id: str):
        account = next((a for a in self.accounts if a["id"] == account_id), None)
        if not account:
            return
        if account.get("auth_provider", "clockenstein") == "clockenstein":
            token = self.data_dir / account.get("token", "missing")
            if token.exists():
                token.unlink()
        self.accounts.remove(account)
        self._services.pop(account_id, None)
        self._credentials.pop(account_id, None)
        self._errors.pop(account_id, None)
        self._save()

    def list_calendars(self):
        result = []
        for account in self.accounts:
            online = self._account_available(account["id"])
            for cal in account.get("calendars", []):
                result.append({"id": cal["id"], "name": cal.get("name", cal["id"]),
                               "color": cal.get("color", "#4285f4"), "provider": "google",
                               "account_id": account["id"], "account_name": account.get("name", account["id"]),
                               "visible": cal.get("visible", True),
                               "reminders": cal.get("reminders", True),
                               "primary": cal.get("primary", cal["id"] == account["id"]),
                               "sync_range": cal.get("sync_range", "normal"),
                               "last_sync": cal.get("last_sync"),
                               "sync_error": cal.get("sync_error", ""),
                               "writable": cal.get("access_role") in ("writer", "owner"),
                               "available": online and cal.get("sync_range") != "too-big"})
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
        calendars = {(a["id"], c["id"]): c for a in self.accounts
                     for c in a.get("calendars", [])
                     if include_hidden or c.get("visible", True)}
        result = []
        for account in self.accounts:
            online = self._account_available(account["id"])
            for raw in account.get("events", []):
                cal = calendars.get((account["id"], raw.get("_calendar_id")))
                if not cal or raw.get("status") == "cancelled":
                    continue
                event = google_event_to_dict(raw, cal, account, online)
                if start and event["date_end"] < start:
                    continue
                if end and event["date_start"] > end:
                    continue
                result.append(event)
        return result

    def refresh(self, start: datetime.date, end: datetime.date,
                limited_range=None, restricted_range=None,
                target_account_id=None, target_calendar_id=None):
        """Refresh the requested range. Returns a list of account errors."""
        errors = []
        stats = {
            "accounts": len(self.accounts),
            "page_size": EVENTS_PAGE_SIZE,
            "calendars": 0,
            "limited_calendars": 0,
            "restricted_calendars": 0,
            "too_big_calendars": 0,
            "calendar_list_requests": 0,
            "event_list_requests": 0,
            "events": 0,
        }
        for account in self.accounts:
            account_id = account["id"]
            if target_account_id and account_id != target_account_id:
                continue
            service = self._services.get(account_id)
            if account.get("auth_provider", "clockenstein") == "goa":
                try:
                    service = self._refresh_goa_service(account)
                except Exception as exc:
                    self._errors[account_id] = str(exc)
                    service = None
            elif service is None and account_id in self._credentials:
                try:
                    service = self._build_service(self._credentials[account_id])
                    self._services[account_id] = service
                except Exception as exc:
                    self._errors[account_id] = str(exc)
            if not service:
                error = self._errors.get(account_id, _("not connected"))
                for cal in account.get("calendars", []):
                    if target_calendar_id and cal["id"] != target_calendar_id:
                        continue
                    cal["sync_error"] = error
                errors.append(f"{account_id}: {error}")
                continue
            try:
                retained = [e for e in account.get("events", [])
                            if not _raw_overlaps(e, start, end)]
                fetched = []
                for cal in account["calendars"]:
                    if target_calendar_id and cal["id"] != target_calendar_id:
                        continue
                    if not cal.get("visible", True) and not target_calendar_id:
                        continue
                    stats["calendars"] += 1
                    sync_range = cal.get("sync_range", "normal")
                    if sync_range == "too-big":
                        stats["too_big_calendars"] += 1
                        continue
                    if sync_range == "restricted" and restricted_range:
                        cal_start, cal_end = restricted_range
                        stats["restricted_calendars"] += 1
                    elif sync_range == "limited" and limited_range:
                        cal_start, cal_end = limited_range
                        stats["limited_calendars"] += 1
                    else:
                        cal_start, cal_end = start, end
                    events, paginated = self._fetch_events(
                        service, cal["id"], cal_start, cal_end, stats,
                        first_page_only=bool(limited_range)
                    )
                    if paginated and sync_range == "normal" and limited_range:
                        cal["sync_range"] = sync_range = "limited"
                        cal_start, cal_end = limited_range
                        stats["limited_calendars"] += 1
                        events, paginated = self._fetch_events(
                            service, cal["id"], cal_start, cal_end, stats,
                            first_page_only=bool(restricted_range)
                        )
                    if paginated and sync_range == "limited" and restricted_range:
                        cal["sync_range"] = sync_range = "restricted"
                        cal_start, cal_end = restricted_range
                        stats["restricted_calendars"] += 1
                        events, paginated = self._fetch_events(
                            service, cal["id"], cal_start, cal_end, stats,
                            first_page_only=True
                        )
                    if paginated and sync_range == "restricted":
                        cal["sync_range"] = "too-big"
                        stats["too_big_calendars"] += 1
                        events = []
                    if cal.get("sync_range") != "too-big":
                        cal["last_sync"] = int(datetime.datetime.now().timestamp())
                        cal["sync_error"] = ""
                    fetched.extend(events)
                too_big_ids = {cal["id"] for cal in account["calendars"]
                               if cal.get("sync_range") == "too-big"}
                if too_big_ids:
                    retained = [event for event in retained
                                if event.get("_calendar_id") not in too_big_ids]
                if target_calendar_id:
                    retained = [event for event in account.get("events", [])
                                if event.get("_calendar_id") != target_calendar_id
                                or not _raw_overlaps(event, start, end)]
                account["events"] = retained + fetched
                creds = self._credentials.get(account_id)
                if creds is not None and account.get("token"):
                    (self.data_dir / account["token"]).write_text(
                        self._credentials_json(creds), encoding="utf-8"
                    )
                self._errors.pop(account_id, None)
            except Exception as exc:
                self._services.pop(account_id, None)
                self._errors[account_id] = str(exc)
                for cal in account.get("calendars", []):
                    if target_calendar_id and cal["id"] != target_calendar_id:
                        continue
                    cal["sync_error"] = str(exc)
                errors.append(f"{account_id}: {exc}")
        self.last_refresh_stats = stats
        self._save()
        return errors

    def create_event(self, data):
        self._validate_event_range(data)
        service = self._require_service(data["account_id"])
        body = event_dict_to_google(data)
        raw = service.events().insert(calendarId=data["calendar_id"], body=body).execute()
        raw["_calendar_id"] = data["calendar_id"]
        self._upsert_cached(data["account_id"], raw)
        return raw

    def update_event(self, uid, data):
        self._validate_event_range(data)
        service = self._require_service(data["account_id"])
        source_id = data.get("original_calendar_id") or data["calendar_id"]
        if source_id != data["calendar_id"]:
            service.events().move(calendarId=source_id, eventId=uid,
                                  destination=data["calendar_id"]).execute()
        raw = service.events().patch(calendarId=data["calendar_id"], eventId=uid,
                                     body=event_dict_to_google(data)).execute()
        raw["_calendar_id"] = data["calendar_id"]
        if source_id != data["calendar_id"]:
            account = next(a for a in self.accounts if a["id"] == data["account_id"])
            account["events"] = [event for event in account.get("events", [])
                                 if not (event.get("id") == uid
                                         and event.get("_calendar_id") == source_id)]
        self._upsert_cached(data["account_id"], raw)
        return raw

    def delete_event(self, uid, calendar_id=None, account_id=None):
        self._require_service(account_id).events().delete(calendarId=calendar_id, eventId=uid).execute()
        account = next(a for a in self.accounts if a["id"] == account_id)
        account["events"] = [e for e in account.get("events", [])
                             if not (e.get("id") == uid and e.get("_calendar_id") == calendar_id)]
        self._save()
        return True

    def _require_service(self, account_id):
        account = next((item for item in self.accounts if item["id"] == account_id), None)
        if account and account.get("auth_provider", "clockenstein") == "goa":
            try:
                return self._refresh_goa_service(account)
            except Exception as exc:
                self._errors[account_id] = str(exc)
                raise GoogleUnavailable(
                    _("This Google account is offline.")
                ) from exc
        service = self._services.get(account_id)
        if service is None and account_id in self._credentials:
            try:
                service = self._build_service(self._credentials[account_id])
                self._services[account_id] = service
                self._errors.pop(account_id, None)
            except Exception as exc:
                self._errors[account_id] = str(exc)
        if not service:
            raise GoogleUnavailable(_("This Google account is offline."))
        return service

    def _validate_event_range(self, data):
        account = next((account for account in self.accounts
                        if account["id"] == data["account_id"]), None)
        calendar = next((calendar for calendar in account.get("calendars", [])
                         if calendar["id"] == data["calendar_id"]), None) if account else None
        if calendar is None:
            raise GoogleUnavailable(_("Google calendar not found."))
        if not google_event_fits_sync_range(
                calendar, data["date_start"], data.get("date_end", data["date_start"])):
            raise GoogleUnavailable(
                _("The event dates are outside the sync range for %s.")
                % calendar.get("name", calendar["id"])
            )

    def _upsert_cached(self, account_id, raw):
        account = next(a for a in self.accounts if a["id"] == account_id)
        account["events"] = [e for e in account.get("events", []) if not (
            e.get("id") == raw.get("id") and e.get("_calendar_id") == raw.get("_calendar_id"))]
        account["events"].append(raw)
        self._save()

    def _load_services(self):
        for account in self.accounts:
            try:
                if account.get("auth_provider", "clockenstein") == "goa":
                    self._refresh_goa_service(account)
                    self._errors.pop(account["id"], None)
                    continue
                scopes = account.get("scopes", SCOPES)
                creds = Credentials.from_authorized_user_file(str(self.data_dir / account["token"]), scopes)
                # Older distro versions restore only the refresh token here.
                # AuthorizedHttp refreshes lazily on the first API request.
                if not creds.valid and not creds.refresh_token:
                    raise GoogleUnavailable(_("authorization expired"))
                self._credentials[account["id"]] = creds
                self._errors.pop(account["id"], None)
            except Exception as exc:
                self._errors[account["id"]] = str(exc)

    @staticmethod
    def _goa_accounts():
        try:
            import gi
            gi.require_version("Goa", "1.0")
            from gi.repository import Goa
        except (ImportError, ValueError) as exc:
            raise GoogleUnavailable(
                _("The gir1.2-goa-1.0 package is missing")
            ) from exc
        try:
            client = Goa.Client.new_sync(None)
            return [
                item for item in client.get_accounts()
                if item.get_account().props.provider_type == "google"
                and item.get_calendar() is not None
                and item.get_oauth2_based() is not None
            ]
        except Exception as exc:
            raise GoogleUnavailable(_("Could not contact Online Accounts: %s") % exc) from exc

    @classmethod
    def _find_goa_account(cls, goa_account_id):
        for goa_object in cls._goa_accounts():
            if goa_object.get_account().props.id == goa_account_id:
                return goa_object
        raise GoogleUnavailable(_("The selected Online Account is unavailable"))

    @classmethod
    def _build_goa_service(cls, goa_object):
        account = goa_object.get_account()
        oauth2 = goa_object.get_oauth2_based()
        try:
            account.call_ensure_credentials_sync(None)
            result = oauth2.call_get_access_token_sync(None)
            token = next((value for value in reversed(result)
                          if isinstance(value, str)), None) if isinstance(result, tuple) else result
            if not token:
                raise GoogleUnavailable(_("Online Accounts returned no access token"))
            return cls._build_service(Credentials(token=token))
        except Exception as exc:
            if isinstance(exc, GoogleUnavailable):
                raise
            raise GoogleUnavailable(_("Could not obtain Google authorization: %s") % exc) from exc

    def _refresh_goa_service(self, account):
        goa_object = self._find_goa_account(account["goa_account_id"])
        service = self._build_goa_service(goa_object)
        self._services[account["id"]] = service
        self._errors.pop(account["id"], None)
        return service

    @staticmethod
    def _fetch_calendars(service, stats=None):
        items, token = [], None
        while True:
            response = service.calendarList().list(pageToken=token).execute()
            if stats is not None:
                stats["calendar_list_requests"] += 1
            items.extend(response.get("items", []))
            token = response.get("nextPageToken")
            if not token:
                return items

    @staticmethod
    def _build_service(creds):
        """Build an API client whose network calls cannot hang the UI forever."""
        http = AuthorizedHttp(creds, http=httplib2.Http(timeout=20))
        return build("calendar", "v3", http=http, cache_discovery=False)

    @staticmethod
    def _fetch_events(service, calendar_id, start, end, stats=None,
                      first_page_only=False):
        local_tz = datetime.datetime.now().astimezone().tzinfo
        time_min = datetime.datetime.combine(start, datetime.time.min, local_tz).isoformat()
        time_max = datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, local_tz).isoformat()
        items, token = [], None
        while True:
            response = service.events().list(calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
                                             singleEvents=True, showDeleted=True,
                                             orderBy="startTime",
                                             maxResults=EVENTS_PAGE_SIZE,
                                             pageToken=token).execute()
            page_items = response.get("items", [])
            if stats is not None:
                stats["event_list_requests"] += 1
                stats["events"] += len(page_items)
            for event in page_items:
                event["_calendar_id"] = calendar_id
                items.append(event)
            token = response.get("nextPageToken")
            if token and first_page_only:
                return items, True
            if not token:
                return items, False

    @staticmethod
    def _merge_calendar_preferences(old, remote):
        preferences = {c["id"]: c for c in old}
        result = [{"id": c["id"], "name": c.get("summary", c["id"]),
                 "color": c.get("backgroundColor", "#4285f4"),
                 "access_role": c.get("accessRole", "reader"),
                 "primary": c.get("primary", False),
                 "visible": preferences.get(c["id"], {}).get("visible", c.get("selected", True)),
                 "reminders": preferences.get(c["id"], {}).get("reminders", True),
                 "sync_range": preferences.get(c["id"], {}).get(
                     "sync_range",
                     "limited" if preferences.get(c["id"], {}).get("limited_range") else "normal"
                 ),
                 "last_sync": preferences.get(c["id"], {}).get("last_sync"),
                 "sync_error": preferences.get(c["id"], {}).get("sync_error", "")}
                for c in remote]
        return result

    def _save(self):
        temp = self.accounts_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.accounts, indent=2), encoding="utf-8")
        temp.replace(self.accounts_file)

    @staticmethod
    def _read_json(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _scopes_for_credentials():
        """Read optional OAuth scopes declared by the bundled client configuration."""
        try:
            config = GoogleBackend._read_oauth_client_config()
        except (OSError, ValueError, json.JSONDecodeError, zlib.error):
            config = {}
        scopes = config.get("clockenstein_scopes", SCOPES)
        return scopes if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes) else SCOPES

    @staticmethod
    def _credentials_json(creds):
        """Serialize credentials on both current and older distro google-auth."""
        if hasattr(creds, "to_json"):
            return creds.to_json()
        expiry = getattr(creds, "expiry", None)
        payload = {
            "token": getattr(creds, "token", None),
            "refresh_token": getattr(creds, "refresh_token", None),
            "token_uri": getattr(creds, "token_uri", None),
            "client_id": getattr(creds, "client_id", None),
            "client_secret": getattr(creds, "client_secret", None),
            "scopes": list(getattr(creds, "scopes", None) or []),
        }
        if expiry is not None:
            payload["expiry"] = expiry.isoformat().replace("+00:00", "Z")
        return json.dumps(payload)


def google_event_to_dict(raw, calendar, account, online):
    start, end = raw.get("start", {}), raw.get("end", {})
    all_day = "date" in start
    if all_day:
        date_start = datetime.date.fromisoformat(start["date"])
        date_end = datetime.date.fromisoformat(end.get("date", start["date"])) - datetime.timedelta(days=1)
        time_start = time_end = None
    else:
        # Google returns dateTime with the event's own UTC offset, which may
        # not match the system timezone. Convert to local wall-clock time
        # before dropping tzinfo, otherwise the displayed time is wrong.
        start_dt = _parse_datetime(start.get("dateTime")).astimezone()
        end_dt = _parse_datetime(end.get("dateTime", start.get("dateTime"))).astimezone()
        date_start, date_end = start_dt.date(), end_dt.date()
        time_start, time_end = start_dt.time(), end_dt.time()
    writable = calendar.get("access_role") in ("writer", "owner")
    event_type = raw.get("eventType", "default")
    return {"uid": raw.get("id", ""), "summary": raw.get("summary") or _("Untitled"),
            "location": raw.get("location", ""), "description": raw.get("description", ""),
            "all_day": all_day, "date_start": date_start, "date_end": date_end,
            "time_start": time_start, "time_end": time_end, "provider": "google",
            "account_id": account["id"], "calendar_id": calendar["id"],
            "calendar_name": calendar.get("name", calendar["id"]),
            "calendar_color": calendar.get("color", "#4285f4"),
            "reminders": calendar.get("reminders", True),
            "sync_range": calendar.get("sync_range", "normal"),
            "event_type": event_type,
            "editable": bool(online and writable and event_type == "default"),
            "cached": not online}


def google_event_fits_sync_range(calendar, date_start, date_end, today=None):
    sync_range = calendar.get("sync_range", "normal")
    if sync_range == "too-big":
        return False
    past_days, future_days = SYNC_RANGES.get(sync_range, NORMAL_RANGE)
    today = today or datetime.date.today()
    synced_start = today - datetime.timedelta(days=past_days)
    synced_end = today + datetime.timedelta(days=future_days)
    return date_start >= synced_start and date_end <= synced_end


def event_dict_to_google(data):
    body = {"summary": data.get("summary", ""), "location": data.get("location", ""),
            "description": data.get("description", "")}
    if data.get("all_day", True):
        body["start"] = {"date": data["date_start"].isoformat()}
        body["end"] = {"date": (data.get("date_end", data["date_start"]) + datetime.timedelta(days=1)).isoformat()}
    else:
        tz = datetime.datetime.now().astimezone().tzinfo
        start = datetime.datetime.combine(data["date_start"], data["time_start"], tz)
        end = datetime.datetime.combine(data.get("date_end", data["date_start"]), data["time_end"], tz)
        body["start"] = {"dateTime": start.isoformat()}
        body["end"] = {"dateTime": end.isoformat()}
    return body


def _parse_datetime(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _raw_start_date(raw):
    start = raw.get("start", {})
    if "date" in start:
        return datetime.date.fromisoformat(start["date"])
    if "dateTime" in start:
        return _parse_datetime(start["dateTime"]).astimezone().date()
    return None


def _raw_end_date(raw):
    end = raw.get("end", {})
    if "date" in end:
        return datetime.date.fromisoformat(end["date"]) - datetime.timedelta(days=1)
    if "dateTime" in end:
        return _parse_datetime(end["dateTime"]).astimezone().date()
    return _raw_start_date(raw)


def _raw_overlaps(raw, start, end):
    raw_start, raw_end = _raw_start_date(raw), _raw_end_date(raw)
    return raw_start is not None and raw_end is not None and raw_end >= start and raw_start <= end
