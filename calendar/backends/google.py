import datetime
import base64
import hashlib
import json
import zlib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from xapp.util import l10n
from backends.remote import RemoteBackend
from clockenstein.networking import GoogleHttp
from clockenstein.sync import SyncDownload, write_private_json

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


class GoogleBackend(RemoteBackend):
    provider = "google"

    def __init__(self, data_dir: Path, timezone: datetime.tzinfo):
        super().__init__(data_dir, timezone)
        self.data_dir = self.database.data_dir / "google"
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        self._services = {}
        self._credentials = {}
        self._errors = {}
        self.last_refresh_stats = {}
        self._load_services()

    def get_account_states(self):
        return [{"id": a["id"], "name": a.get("name", a["id"]),
                 "online": self._account_available(a["id"]),
                 "error": self._errors.get(a["id"], "")} for a in self.accounts]

    def _account_available(self, account_id):
        return (account_id in self._services
                or account_id in self._credentials and account_id not in self._errors)

    def connect(self, progress=None) -> str:
        scopes = self._get_scopes_for_credentials()
        if progress:
            progress(_("Waiting for Google authorization…"))
        flow = InstalledAppFlow.from_client_config(
            self._read_oauth_client_config(), scopes
        )
        credentials = flow.run_local_server(port=0, authorization_prompt_message="Opening Google sign-in…",
                                      prompt="select_account consent")
        if progress:
            progress(_("Authorization received • Contacting Google Calendar…"))
        service = self._build_service(credentials)
        if progress:
            progress(_("Loading your Google calendars…"))
        calendars = self._fetch_calendars(service)
        primary = next((calendar for calendar in calendars if calendar.get("primary")), None)
        if not primary:
            raise GoogleUnavailable(_("Google did not return a primary calendar"))
        account_id = primary["id"]
        token_name = hashlib.sha256(account_id.encode()).hexdigest()[:20] + ".json"
        self._save_credentials(token_name, credentials)
        account = {"id": account_id, "name": account_id, "token": token_name,
                   "scopes": scopes, "auth_provider": "clockenstein"}
        self.database.connect_account(self.provider, account,
                                      self._get_calendar_metadata(calendars))
        self._services[account_id] = service
        self._credentials[account_id] = credentials
        self._errors.pop(account_id, None)
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
        for goa_object in self._get_goa_accounts():
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
        account = {"id": account_id,
                   "name": goa_account.props.presentation_identity or primary["id"],
                   "auth_provider": "goa", "goa_account_id": goa_account_id}
        self.database.connect_account(self.provider, account,
                                      self._get_calendar_metadata(calendars))
        self._services[account_id] = service
        self._errors.pop(account_id, None)
        return account_id

    def disconnect(self, account_id: str):
        account = next((a for a in self.accounts if a["id"] == account_id), None)
        if not account:
            return
        if account.get("auth_provider", "clockenstein") == "clockenstein":
            token = self.data_dir / account.get("token", "missing")
            if token.exists():
                token.unlink()
        self.database.disconnect_account(self.provider, account_id)
        self._services.pop(account_id, None)
        self._credentials.pop(account_id, None)
        self._errors.pop(account_id, None)

    def refresh(self, start: datetime.date, end: datetime.date,
                limited_range=None, restricted_range=None,
                target_account_id=None, target_calendar_id=None):
        errors = []
        stats = {"accounts": len(self.accounts), "page_size": EVENTS_PAGE_SIZE,
                 "calendars": 0, "limited_calendars": 0, "restricted_calendars": 0,
                 "too_big_calendars": 0, "calendar_list_requests": 0,
                 "event_list_requests": 0, "events": 0}
        for account in self.accounts:
            account_id = account["id"]
            if target_account_id and account_id != target_account_id:
                continue
            account_error = None
            try:
                service = self._require_service(account_id)
            except Exception as exc:
                self._sync_failed(account_id, exc, target_calendar_id, start, end)
                errors.append(f"{account_id}: {exc}")
                continue
            for calendar in self.database.get_calendars(self.provider, account_id):
                if target_calendar_id and calendar["id"] != target_calendar_id:
                    continue
                if not calendar["visible"]:
                    continue
                sync_range = calendar["sync_range"]
                if sync_range == "too-big":
                    stats["too_big_calendars"] += 1
                    continue
                stats["calendars"] += 1
                download = SyncDownload(self.database.data_dir, self.provider, account_id, calendar["id"], start, end)
                try:
                    ranges = {"normal": (start, end), "limited": limited_range,
                              "restricted": restricted_range}
                    while True:
                        cal_start, cal_end = ranges[sync_range] or (start, end)
                        if sync_range in ("limited", "restricted"):
                            stats[f"{sync_range}_calendars"] += 1
                        first_page_only = bool(limited_range) if sync_range == "normal" else bool(restricted_range)
                        if sync_range == "restricted":
                            first_page_only = True
                        raw_events, paginated = self._fetch_events(
                            service, calendar["id"], cal_start, cal_end, self.timezone, stats,
                            first_page_only=first_page_only, download=download)
                        if paginated and sync_range == "normal" and limited_range:
                            sync_range = "limited"
                        elif paginated and sync_range == "limited" and restricted_range:
                            sync_range = "restricted"
                        elif paginated and sync_range == "restricted":
                            sync_range = "too-big"
                            stats["too_big_calendars"] += 1
                            raw_events = []
                            break
                        else:
                            break
                    # Save before parsing: malformed data is precisely what we need
                    # to inspect when conversion fails. Failed downloads keep the old file.
                    download.save()
                    events = [google_event_to_dict(raw, calendar, account, True, self.timezone)
                              for raw in raw_events if raw.get("status") != "cancelled"]
                    for event in events:
                        # Calendar permissions are applied when reading, not frozen
                        # into the event when downloading from a read-only calendar.
                        event["editable"] = event["event_type"] == "default"
                    self.database.apply_sync(calendar, events, start, end, self.timezone, sync_range)
                    download.finish()
                except Exception as exc:
                    download.finish(exc)
                    account_error = str(exc)
                    self._errors[account_id] = str(exc)
                    self._sync_failed(account_id, exc, calendar["id"])
                    errors.append(f"{account_id}: {exc}")
            if account_error:
                self._services.pop(account_id, None)
            else:
                self._errors.pop(account_id, None)
            credentials = self._credentials.get(account_id)
            if credentials is not None and account.get("token"):
                # Do not recreate a token file after a concurrent disconnect.
                if any(a["id"] == account_id for a in self.accounts):
                    self._save_credentials(account["token"], credentials)
        self.last_refresh_stats = stats
        return errors

    def create_event(self, data):
        self._validate_event_range(data)
        service = self._require_service(data["account_id"])
        body = event_dict_to_google(data, self.timezone)
        raw = service.events().insert(calendarId=data["calendar_id"], body=body).execute()
        self._upsert_cached(data["account_id"], data["calendar_id"], raw)
        return raw

    def update_event(self, uid, data):
        self._validate_event_range(data)
        service = self._require_service(data["account_id"])
        source_id = data.get("original_calendar_id") or data["calendar_id"]
        if source_id != data["calendar_id"]:
            service.events().move(calendarId=source_id, eventId=uid,
                                  destination=data["calendar_id"]).execute()
        raw = service.events().patch(calendarId=data["calendar_id"], eventId=uid,
                                     body=event_dict_to_google(data, self.timezone)).execute()
        self._upsert_cached(data["account_id"], data["calendar_id"], raw, source_id)
        return raw

    def delete_event(self, uid, calendar_id=None, account_id=None):
        self._require_service(account_id).events().delete(calendarId=calendar_id, eventId=uid).execute()
        self.database.delete_event(self.provider, account_id, calendar_id, uid)
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
        account_exists = any(account["id"] == data["account_id"] for account in self.accounts)
        calendar = None
        if account_exists:
            calendars = self.database.get_calendars(self.provider, data["account_id"])
            for candidate in calendars:
                if candidate["id"] == data["calendar_id"]:
                    calendar = candidate
                    break
        if calendar is None:
            raise GoogleUnavailable(_("Google calendar not found."))
        if not google_event_fits_sync_range(
                calendar, data["date_start"], data.get("date_end", data["date_start"])):
            raise GoogleUnavailable(
                _("The event dates are outside the sync range for %s.")
                % calendar.get("name", calendar["id"])
            )

    def _upsert_cached(self, account_id, calendar_id, raw, source_id=None):
        calendar = next(calendar for calendar in self.database.get_calendars(self.provider, account_id)
                        if calendar["id"] == calendar_id)
        event = google_event_to_dict(raw, calendar, {"id": account_id}, True, self.timezone)
        event["editable"] = event["event_type"] == "default"
        self.database.save_event(self.provider, account_id, calendar_id, event, self.timezone, source_id)

    def _save_credentials(self, name, credentials):
        path = self.data_dir / name
        # Token files are separate from calendar records and diagnostic downloads.
        write_private_json(path, json.loads(self._get_credentials_json(credentials)))

    def _load_services(self):
        for account in self.accounts:
            try:
                if account.get("auth_provider", "clockenstein") == "goa":
                    self._refresh_goa_service(account)
                    self._errors.pop(account["id"], None)
                    continue
                scopes = account.get("scopes", SCOPES)
                credentials = Credentials.from_authorized_user_file(str(self.data_dir / account["token"]), scopes)
                # Older distro versions restore only the refresh token here.
                # AuthorizedHttp refreshes lazily on the first API request.
                if not credentials.valid and not credentials.refresh_token:
                    raise GoogleUnavailable(_("authorization expired"))
                self._credentials[account["id"]] = credentials
                self._errors.pop(account["id"], None)
            except Exception as exc:
                self._errors[account["id"]] = str(exc)

    @staticmethod
    def _get_goa_accounts():
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
        for goa_object in cls._get_goa_accounts():
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
    def _build_service(credentials):
        http = AuthorizedHttp(credentials, http=GoogleHttp())
        return build("calendar", "v3", http=http, cache_discovery=False)

    @staticmethod
    def _fetch_events(service, calendar_id, start, end, timezone, stats=None,
                      first_page_only=False, download=None):
        local_tz = timezone
        time_min = datetime.datetime.combine(start, datetime.time.min, local_tz).isoformat()
        time_max = datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, local_tz).isoformat()
        items, token = [], None
        while True:
            response = service.events().list(calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
                                             singleEvents=True, showDeleted=False,
                                             timeZone=getattr(local_tz, "key", None),
                                             orderBy="startTime",
                                             maxResults=EVENTS_PAGE_SIZE,
                                             pageToken=token).execute()
            if download is not None:
                download.add(start, end, response)
            page_items = response.get("items", [])
            if stats is not None:
                stats["event_list_requests"] += 1
                stats["events"] += len(page_items)
            items.extend(page_items)
            token = response.get("nextPageToken")
            if token and first_page_only:
                return items, True
            if not token:
                return items, False

    @staticmethod
    def _get_calendar_metadata(remote):
        calendars = []
        for calendar in remote:
            calendars.append({
                "id": calendar["id"],
                "name": calendar.get("summary", calendar["id"]),
                "color": calendar.get("backgroundColor", "#4285f4"),
                "writable": calendar.get("accessRole") in ("writer", "owner"),
                "primary": calendar.get("primary", False),
                "visible": calendar.get("selected", True),
            })
        return calendars

    @staticmethod
    def _get_scopes_for_credentials():
        """Read optional OAuth scopes declared by the bundled client configuration."""
        try:
            config = GoogleBackend._read_oauth_client_config()
        except (OSError, ValueError, json.JSONDecodeError, zlib.error):
            config = {}
        scopes = config.get("clockenstein_scopes", SCOPES)
        return scopes if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes) else SCOPES

    @staticmethod
    def _get_credentials_json(credentials):
        """Serialize credentials on both current and older distro google-auth."""
        if hasattr(credentials, "to_json"):
            return credentials.to_json()
        expiry = getattr(credentials, "expiry", None)
        payload = {
            "token": getattr(credentials, "token", None),
            "refresh_token": getattr(credentials, "refresh_token", None),
            "token_uri": getattr(credentials, "token_uri", None),
            "client_id": getattr(credentials, "client_id", None),
            "client_secret": getattr(credentials, "client_secret", None),
            "scopes": list(getattr(credentials, "scopes", None) or []),
        }
        if expiry is not None:
            payload["expiry"] = expiry.isoformat().replace("+00:00", "Z")
        return json.dumps(payload)


def google_event_to_dict(raw, calendar, account, online, timezone):
    start, end = raw.get("start", {}), raw.get("end", {})
    all_day = "date" in start
    if all_day:
        date_start = datetime.date.fromisoformat(start["date"])
        date_end = datetime.date.fromisoformat(end.get("date", start["date"])) - datetime.timedelta(days=1)
        time_start = time_end = None
    else:
        start_dt = _parse_datetime(start.get("dateTime"), start.get("timeZone"))
        end_dt = _parse_datetime(end.get("dateTime", start.get("dateTime")), end.get("timeZone"))
        start_dt = start_dt.astimezone(timezone)
        end_dt = end_dt.astimezone(timezone)
        date_start, date_end = start_dt.date(), end_dt.date()
        time_start, time_end = start_dt.time().replace(tzinfo=None), end_dt.time().replace(tzinfo=None)
    writable = calendar.get("writable", calendar.get("access_role") in ("writer", "owner"))
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
            "cached": not online,
            "_start": date_start if all_day else start_dt,
            "_end": date_end if all_day else end_dt}


def google_event_fits_sync_range(calendar, date_start, date_end, today=None):
    sync_range = calendar.get("sync_range", "normal")
    if sync_range == "too-big":
        return False
    past_days, future_days = SYNC_RANGES.get(sync_range, NORMAL_RANGE)
    today = today or datetime.date.today()
    synced_start = today - datetime.timedelta(days=past_days)
    synced_end = today + datetime.timedelta(days=future_days)
    return date_start >= synced_start and date_end <= synced_end


def event_dict_to_google(data, timezone):
    body = {"summary": data.get("summary", ""), "location": data.get("location", ""),
            "description": data.get("description", "")}
    # Updates are sent with patch, which merges, so the fields of the other
    # start/end shape must be cleared explicitly.
    if data.get("all_day", True):
        end_date = data.get("date_end", data["date_start"]) + datetime.timedelta(days=1)
        body["start"] = {"date": data["date_start"].isoformat(), "dateTime": None, "timeZone": None}
        body["end"] = {"date": end_date.isoformat(), "dateTime": None, "timeZone": None}
    else:
        start = datetime.datetime.combine(data["date_start"], data["time_start"], timezone)
        end = datetime.datetime.combine(data.get("date_end", data["date_start"]), data["time_end"], timezone)
        timezone_name = getattr(timezone, "key", None)
        body["start"] = {"dateTime": start.isoformat(), "date": None, "timeZone": timezone_name}
        body["end"] = {"dateTime": end.isoformat(), "date": None, "timeZone": timezone_name}
    return body


def _parse_datetime(value, time_zone=None):
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None and time_zone:
        try:
            return parsed.replace(tzinfo=ZoneInfo(time_zone))
        except ZoneInfoNotFoundError:
            pass
    return parsed
