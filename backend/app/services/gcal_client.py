"""Google Calendar cache refresh.

Read-only: fetches today's events from the user's primary calendar into
`gcal_events_cache`. The one-time OAuth is performed by `python -m app.auth.gcal`;
this module only reads the persisted token and refreshes it silently when the
Google client determines it's expired. If any of that fails, sync raises — the
sync pipeline catches per-step exceptions and continues with other steps.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from ..config import settings
from ..models import now_iso
from . import app_settings, external_errors, secret_store, user_meta as _user_meta

log = logging.getLogger("watson.gcal")

# calendar.events covers both READ (used by the today-sync fetch loop) and
# WRITE (used by the `gcal_create_event` executor). A previously-authenticated
# user on the older calendar.readonly scope must re-run `python -m app.auth.gcal`
# — Watson detects the scope mismatch at token load and logs a clear warning.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.created"
OAUTH_SCOPES = list(SCOPES)  # Nickname links don't require a Meet API grant.
_MAX_CONNECTION_TIMEOUT_SECONDS = 20.0


class GoogleCredentialError(RuntimeError):
    """Google credentials failed without exposing authorized-user material."""


def credential_json(credentials, *, require_granted=False):
    """Persist actual consent, not the OAuth library's requested-scope override."""
    serialized = credentials.to_json()
    granted = getattr(credentials, 'granted_scopes', None)
    if granted is None and not require_granted:
        return serialized
    data = json.loads(serialized)
    data['scopes'] = granted.split() if isinstance(granted, str) else list(granted or [])
    return json.dumps(data)


def gcal_config() -> dict:
    """Resolve OAuth material from Keychain only, failing closed on errors."""
    if app_settings.integration_disabled("google-calendar"):
        return MappingProxyType({"client_config": "", "authorized_user": ""})
    try:
        return MappingProxyType({
            "client_config": secret_store.get_secret("google.client_config"),
            "authorized_user": secret_store.get_secret("google.authorized_user"),
        })
    except secret_store.SecretStoreError:
        return MappingProxyType({"client_config": "", "authorized_user": ""})


def configured(config=None) -> bool:
    """True when both Google OAuth secrets are present in Keychain."""
    config = config or gcal_config()
    return bool(config["client_config"] and config["authorized_user"])


def _require_config(config=None):
    config = config or gcal_config()
    if not configured(config):
        raise RuntimeError(
            "Google Calendar is not configured (missing credentials/token)"
        )
    return config


def _tz():
    return ZoneInfo(settings.tz)


def _load_credentials(config=None, *, timeout_seconds: float | None = None):
    try:
        return _load_credentials_unredacted(
            config, timeout_seconds=timeout_seconds
        )
    except GoogleCredentialError:
        raise
    except Exception:
        raise GoogleCredentialError(
            "Google Calendar credentials could not be loaded."
        ) from None


def _load_credentials_unredacted(
    config=None, *, timeout_seconds: float | None = None
):
    """Load user credentials, refreshing the access token if expired.

    Returns None when the Keychain authorized-user item is absent. Legacy token
    files are accepted only by the explicit Settings import endpoint.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    config = config or gcal_config()
    authorized_user_json = config["authorized_user"]
    if not authorized_user_json:
        return None
    info = json.loads(authorized_user_json)
    # Old records without scope metadata retain their Calendar-only behavior.
    # Never pass the expanded OAuth request scopes into existing credentials.
    creds = Credentials.from_authorized_user_info(info, None if 'scopes' in info else SCOPES)
    if creds and creds.expired and creds.refresh_token:
        refresh_request = Request()
        if timeout_seconds is None:
            creds.refresh(refresh_request)
        else:
            timeout_seconds = min(
                _MAX_CONNECTION_TIMEOUT_SECONDS,
                max(0.001, timeout_seconds),
            )
            deadline = time.monotonic() + timeout_seconds

            def bounded_refresh_request(*args, **kwargs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Google credential refresh timed out")
                kwargs["timeout"] = min(timeout_seconds, remaining)
                return refresh_request(*args, **kwargs)

            creds.refresh(bounded_refresh_request)
        serialized = credential_json(creds)
        secret_store.set_secret("google.authorized_user", serialized)
    return creds


def _service(config=None, *, timeout_seconds: float | None = None):
    """Build the Calendar v3 API client. Kept out of module scope so tests can
    monkey-patch without importing googleapiclient at test-collection time."""
    from googleapiclient.discovery import build

    config = config or gcal_config()
    creds = _load_credentials(config, timeout_seconds=timeout_seconds)
    if creds is None:
        raise RuntimeError(
            "gcal token missing — run: python -m app.auth.gcal"
        )
    # cache_discovery=False silences a filesystem warning under uvicorn's reloader.
    if timeout_seconds is None:
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    # Connection tests need a deadline in Google's transport itself.  Watson's
    # outer worker deadline keeps the HTTP request responsive, while this
    # transport timeout ensures the abandoned worker will eventually exit.
    import google_auth_httplib2
    import httplib2

    timeout_seconds = min(
        _MAX_CONNECTION_TIMEOUT_SECONDS,
        max(0.001, timeout_seconds),
    )
    transport = httplib2.Http(timeout=timeout_seconds)
    authorized_transport = google_auth_httplib2.AuthorizedHttp(
        creds, http=transport
    )
    return build(
        "calendar",
        "v3",
        http=authorized_transport,
        cache_discovery=False,
    )


def _day_bounds(now: datetime):
    """Local-TZ start-of-day and end-of-day as RFC3339 strings for the API."""
    tz = _tz()
    now_local = now.astimezone(tz)
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _day_bounds_for_date(date_str: str):
    """Bounds for a YYYY-MM-DD date in the configured local TZ. Used by the
    ad-hoc /api/meetings endpoint when the user navigates to a specific day."""
    tz = _tz()
    y, m, d = date_str.split("-")
    start = datetime(int(y), int(m), int(d), 0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _normalize_event(ev: dict) -> dict:
    """Flatten Google's event shape into the columns of gcal_events_cache.

    Returns a dict ready for INSERT. All-day events use `Date` (YYYY-MM-DD)
    on start/end; timed events use `DateTime` (RFC3339). We normalize both
    to ISO strings and mark all_day so the digest / UI can render appropriately.
    """
    start = ev.get("start", {}) or {}
    end = ev.get("end", {}) or {}
    all_day = "date" in start and "dateTime" not in start
    if all_day:
        start_at = start.get("date", "")
        end_at = end.get("date", "")
    else:
        start_at = start.get("dateTime", "")
        end_at = end.get("dateTime", "")

    attendees = []
    my_response = ""
    for a in ev.get("attendees", []) or []:
        email = a.get("email")
        if a.get("self"):
            my_response = a.get("responseStatus", "") or ""
            continue
        if email:
            attendees.append(email)

    organizer = ""
    org = ev.get("organizer") or {}
    if org.get("displayName"):
        organizer = org["displayName"]
    elif org.get("email"):
        organizer = org["email"]

    return {
        "event_id": ev.get("id", ""),
        "calendar_id": "primary",
        "title": ev.get("summary", "") or "",
        "description": ev.get("description"),
        "location": ev.get("location"),
        "start_at": start_at,
        "end_at": end_at,
        "all_day": 1 if all_day else 0,
        "organizer": organizer,
        "attendees": json.dumps(attendees),
        "my_response": my_response,
        "html_link": ev.get("htmlLink", ""),
        "meet_link": _extract_meet_link(ev),
    }


_MEET_URL_RE = re.compile(
    r"https?://("
    r"meet\.google\.com/[a-z0-9\-]+"
    r"|[a-z0-9\-]+\.?zoom\.us/j/[\w?=&.\-/]+"
    r"|teams\.microsoft\.com/l/meetup-join/[\w%?=&.\-/]+"
    r"|[a-z0-9\-]+\.webex\.com/(?:meet|join)/[\w?=&.\-/]+"
    r"|meet\.jit\.si/[\w\-]+"
    r")",
    re.IGNORECASE,
)


def _extract_meet_link(ev: dict) -> str:
    """Return a direct-join video URL for the event, or "" if none found.

    Priority: (1) Google's `hangoutLink` (quick Meet URL) → (2) any video
    entryPoint on `conferenceData` (newer Meet + third-party conferences
    that Google surfaces this way) → (3) regex scan of the description
    for common video-conference URLs (Zoom / Teams / Webex / Jitsi).
    """
    # (1) hangoutLink — set for old-style Meet-enabled events.
    hl = ev.get("hangoutLink")
    if hl:
        return hl
    # (2) conferenceData.entryPoints — modern API. Prefer video, else phone.
    cdata = ev.get("conferenceData") or {}
    eps = cdata.get("entryPoints") or []
    video = next((e for e in eps if e.get("entryPointType") == "video" and e.get("uri")), None)
    if video:
        return video["uri"]
    # (3) description scrape.
    desc = ev.get("description") or ""
    if desc:
        m = _MEET_URL_RE.search(desc)
        if m:
            return m.group(0)
    return ""


def _fetch_events_in_window(time_min, time_max, max_results: int = 50):
    """Call the Calendar API for events in [time_min, time_max). Skips declined
    events. Returns (events, user_email, raw_items) — the email is resolved
    from the response so callers can persist it for cross-integration
    authuser use, and raw_items lets the sync path harvest full attendee
    info (email + displayName) for the contacts store.
    """
    svc = _service()
    resp = svc.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()

    raw_items = resp.get("items", []) or []
    user_email = _resolve_user_email(svc, raw_items)

    events = []
    for ev in raw_items:
        norm = _normalize_event(ev)
        # Drop events I've declined — they don't belong on my morning brief.
        if norm["my_response"] == "declined":
            continue
        # Sanity: drop rows without a start_at, they'd break the index.
        if not norm["start_at"]:
            continue
        if user_email:
            # Any Google-owned URL gets authuser=<work-email>. Non-Google
            # URLs (Zoom, Teams, custom locations) pass through unchanged.
            norm["html_link"] = _user_meta.with_authuser_if_google(norm.get("html_link", ""), user_email)
            norm["meet_link"] = _user_meta.with_authuser_if_google(norm.get("meet_link", ""), user_email)
        events.append(norm)
    return events, user_email, raw_items


def _harvest_contact_pairs(raw_items: list, self_email: str = "") -> list:
    """Extract (email, display_name) pairs from raw event attendee lists.
    Skips the user's own row + resource emails (rooms, projectors — Google
    uses `resource: true` on those). Later rows overwrite earlier ones so
    the freshest displayName wins."""
    out: dict = {}
    for ev in raw_items:
        for a in ev.get("attendees", []) or []:
            if a.get("self") or a.get("resource"):
                continue
            email = (a.get("email") or "").strip().lower()
            if not email or "@" not in email:
                continue
            if self_email and email == self_email.lower():
                continue
            name = (a.get("displayName") or "").strip()
            out[email] = name
    return list(out.items())


def _upsert_contacts(conn, pairs: list) -> int:
    """Idempotent upsert into gcal_contacts. Only overwrites `name` if the
    new value is non-empty — so a good displayName from an old event doesn't
    get clobbered by an empty one on a newer event."""
    if not pairs:
        return 0
    stamp = now_iso()
    n = 0
    for email, name in pairs:
        conn.execute(
            "INSERT INTO gcal_contacts (email, name, last_seen_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(email) DO UPDATE SET"
            "   name = CASE WHEN excluded.name != '' THEN excluded.name ELSE gcal_contacts.name END,"
            "   last_seen_at = excluded.last_seen_at",
            (email, name, stamp),
        )
        n += 1
    conn.commit()
    return n


def _fetch_todays_events():
    """Today-window wrapper around _fetch_events_in_window. Returns
    (events, user_email, raw_items) — sync() uses the raw items to harvest
    contacts. Tests can monkey-patch this whole function to stub gcal."""
    time_min, time_max = _day_bounds(datetime.now())
    return _fetch_events_in_window(time_min, time_max)


def events_for_date(date_str: str) -> list:
    """Fetch and normalize events for a specific YYYY-MM-DD date, live from
    the Google Calendar API — bypasses the today-only sqlite cache. Used by
    the Today UI's back/forward date navigation. Returns dicts shaped like
    the ones `gcal_event_dict()` produces, so the frontend renderer treats
    them identically.
    """
    import json as _json
    from ..models import now_iso
    time_min, time_max = _day_bounds_for_date(date_str)
    events, _user_email, _raw = _fetch_events_in_window(time_min, time_max)
    # Same leave-marker filter as todays_events — the Today timeline should
    # look the same when the user navigates to yesterday / tomorrow.
    events = [e for e in events if not _is_leave_marker(e)]
    stamp = now_iso()
    # Reshape from the "cache row" schema (int all_day, JSON attendees) to
    # the "client dict" schema (bool all_day, list attendees) so the
    # frontend timeline renderer doesn't need to distinguish the source.
    out = []
    for e in events:
        try:
            attendees = _json.loads(e.get("attendees") or "[]")
            if not isinstance(attendees, list):
                attendees = []
        except (ValueError, TypeError):
            attendees = []
        out.append({
            "event_id": e.get("event_id", ""),
            "calendar_id": e.get("calendar_id", "primary"),
            "title": e.get("title", ""),
            "description": e.get("description") or "",
            "location": e.get("location") or "",
            "start_at": e.get("start_at", ""),
            "end_at": e.get("end_at", ""),
            "all_day": bool(e.get("all_day")),
            "organizer": e.get("organizer") or "",
            "attendees": attendees,
            "my_response": e.get("my_response") or "",
            "html_link": e.get("html_link") or "",
            "meet_link": e.get("meet_link") or "",
            "synced_at": stamp,
        })
    return out


def _resolve_user_email(svc, raw_items):
    """Find the email of the account that consented to OAuth. Prefer the
    `self: true` attendee on any event (free, already in the response). Fall
    back to a calendarList.get(primary) call, whose id is the owner's email."""
    for ev in raw_items:
        for a in ev.get("attendees", []) or []:
            if a.get("self") and a.get("email"):
                return a["email"]
    try:
        primary = svc.calendarList().get(calendarId="primary").execute()
        return primary.get("id", "") or ""
    except Exception as exc:  # noqa: BLE001
        external_errors.log_failure(
            log, "resolve Google Calendar user email", "google-calendar", exc
        )
        return ""


def _with_authuser(url: str, email: str) -> str:
    """Kept as an alias for callers that pre-date the shared helper.
    New code should use user_meta.with_authuser_if_google(url, email) —
    that one also guards against non-Google URLs slipping through."""
    return _user_meta.with_authuser_if_google(url, email)


def sync(conn) -> int:
    """Refresh today's events. Full-replace semantics — no history kept.

    Contract mirrors clickup_client.sync / gitlab_client.sync / flock_client.sync
    — one integer, non-abort-on-failure (raises freely; the sync_pipeline wraps).

    Why full-replace: we only ever fetch today's window from Google, so rows
    from previous days are stale by definition. Keeping them around risks
    event_id collisions with fresh fetches (Google reuses the base id for the
    same event across days in some recurrence expansions), and there's no read
    path that wants historical events.
    """
    events, user_email, raw_items = _fetch_todays_events()
    # Persist the resolved email so any integration surfacing a Google URL
    # (Drive, Docs, Mail, Photos…) can pick it up via user_meta.
    if user_email:
        _user_meta.set_google_user_email(conn, user_email)
    # Harvest attendee emails + names for the classifier to reference when
    # drafting gcal_create_event actions. Every sync tick incrementally
    # adds anyone new; a wider backfill can seed history via the
    # `python -m app.auth.gcal_backfill` command.
    n_contacts = _upsert_contacts(conn, _harvest_contact_pairs(raw_items, user_email))
    if n_contacts:
        log.info("gcal sync: upserted %d contact(s) from today's attendees", n_contacts)
    conn.execute("DELETE FROM gcal_events_cache")
    stamp = now_iso()
    for e in events:
        conn.execute(
            "INSERT INTO gcal_events_cache ("
            " event_id, calendar_id, title, description, location, start_at, end_at,"
            " all_day, organizer, attendees, my_response, html_link, meet_link, synced_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                e["event_id"], e["calendar_id"], e["title"], e["description"],
                e["location"], e["start_at"], e["end_at"], e["all_day"],
                e["organizer"], e["attendees"], e["my_response"], e["html_link"],
                e.get("meet_link", ""), stamp,
            ),
        )
    conn.commit()
    return len(events)


# Events with a title matching this regex AND a long duration are
# teammate-leave markers ("On leave - avi.s", "Aviral OOO", "PTO - vacation")
# — noise on the Today timeline / notifier, and not actionable. Filtered
# out at read time so the cache still holds them (Ask can answer
# "who's out today?" from the DB). Kept conservative — "WFH" isn't in
# the pattern because the user may still take meetings from those people.
_LEAVE_TITLE_RE = re.compile(
    r"\b(on\s+leave|leave|ooo|out\s+of\s+office|pto|vacation|holiday|day\s+off|is\s+off)\b",
    re.IGNORECASE,
)
# Minimum duration (seconds) at which a leave-titled event is treated as a
# real leave marker vs an ordinary meeting. 6h covers all-day (24h) and
# half/full-workday timed events, while a 30-60 min "discuss leave policy"
# 1:1 stays on the timeline.
_LEAVE_MIN_DURATION_S = 6 * 3600


def _duration_seconds(start_at: str, end_at: str) -> int:
    """Best-effort duration in seconds between two ISO/date strings.
    Handles both all-day (bare YYYY-MM-DD dates) and timed (with time and
    optional TZ offset) formats. Returns 0 on any parse failure — the
    caller treats 0-duration as 'don't apply the leave filter'."""
    if not start_at or not end_at:
        return 0
    try:
        s = datetime.fromisoformat(start_at)
    except ValueError:
        try:
            s = datetime.fromisoformat(start_at + "T00:00:00")
        except ValueError:
            return 0
    try:
        e = datetime.fromisoformat(end_at)
    except ValueError:
        try:
            e = datetime.fromisoformat(end_at + "T00:00:00")
        except ValueError:
            return 0
    if s.tzinfo and not e.tzinfo:
        e = e.replace(tzinfo=s.tzinfo)
    elif e.tzinfo and not s.tzinfo:
        s = s.replace(tzinfo=e.tzinfo)
    return max(0, int((e - s).total_seconds()))


def _is_leave_marker(row) -> bool:
    """True when the event title matches the leave regex AND the event is
    long enough to plausibly be a leave block (≥6h). This catches both
    all-day markers and multi-day timed events (some folks create leave
    as a 5-day 9am–6pm timed block), while leaving normal-length meetings
    that happen to say "leave" (a 30-min 'discuss leave policy' 1:1) alone.
    """
    def _get(key, default=None):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default
    title = _get("title") or ""
    if not _LEAVE_TITLE_RE.search(title):
        return False
    # All-day events pass the duration threshold trivially — Google
    # represents them as YYYY-MM-DD with end_date one day after start_date.
    if _get("all_day"):
        return True
    dur = _duration_seconds(_get("start_at") or "", _get("end_at") or "")
    return dur >= _LEAVE_MIN_DURATION_S


def todays_events(conn) -> list:
    """Read the cache for events still relevant today (not yet ended).

    "Overlaps today AND isn't already over."  A multi-day all-day event
    like an on-leave marker (start_at="2026-07-06", end_at="2026-07-11")
    counts as relevant to today (2026-07-10) — the previous filter that
    required start_at >= today_start silently hid all such events.

    Excludes teammate-leave markers (all-day events matching
    _LEAVE_TITLE_RE) so the Today timeline stays focused on actionable
    meetings. Ask still sees them via a separate query path.

    Comparisons work as string operations because all our timestamps are
    ISO 8601 with a consistent TZ offset (or bare YYYY-MM-DD dates, which
    sort correctly against the offsetted datetimes).
    """
    time_min, time_max = _day_bounds(datetime.now())
    rows = conn.execute(
        "SELECT * FROM gcal_events_cache"
        # Event overlaps today: started before today ends AND ends after today began.
        # We deliberately do NOT filter out past-of-day events here — the timeline
        # on /today shows the whole day for context (past events render dimmed).
        # Callers that want only future events (e.g. a "next up" widget) can do
        # that filter client-side against end_at > now.
        " WHERE start_at < ?"
        "   AND (end_at IS NULL OR end_at = '' OR end_at > ?)"
        " ORDER BY start_at",
        (time_max, time_min),
    ).fetchall()
    return [r for r in rows if not _is_leave_marker(r)]


# ---------- contacts (harvest + lookup) ---------------------------------

def backfill_contacts(conn, days_back: int = 90, per_page_max: int = 250) -> int:
    """One-shot backfill: pull attendees from the past N days of calendar
    events and populate gcal_contacts. Call from a CLI entrypoint after
    first re-auth so the classifier can immediately draft events with
    correct email addresses instead of empty attendee lists.

    Returns the number of unique contacts upserted.
    """
    from datetime import timedelta as _td
    now = datetime.now().astimezone(_tz())
    time_min = (now - _td(days=days_back)).isoformat()
    time_max = now.isoformat()
    _events, user_email, raw_items = _fetch_events_in_window(
        time_min, time_max, max_results=per_page_max,
    )
    pairs = _harvest_contact_pairs(raw_items, user_email)
    n = _upsert_contacts(conn, pairs)
    log.info("gcal contacts backfill: %d unique from last %d days", n, days_back)
    return n


def known_contacts(conn, limit: int = 60) -> list:
    """Return the N most-recently-seen contacts as (name, email) tuples,
    freshest first. Consumed by the classifier prompt so the LLM can
    resolve names in a capture ("Morgan") to real email addresses on
    drafted gcal_create_event actions."""
    return [(r["name"] or "", r["email"]) for r in conn.execute(
        "SELECT email, name FROM gcal_contacts"
        " WHERE last_seen_at IS NOT NULL"
        " ORDER BY last_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()]


# ---------- event creation (approve → execute) --------------------------

def create_event(title: str, start_at: str, end_at: str,
                 attendees: list = None, description: str = "",
                 add_meet: bool = True, calendar_id: str = "primary",
                 config=None) -> dict:
    """Create a Google Calendar event on the user's primary calendar.
    `start_at` / `end_at` are ISO 8601 strings; the Calendar API infers the
    time zone from a trailing offset (e.g. "+05:30") or from a bare local
    datetime combined with the calendar's default TZ.

    add_meet=True asks Google to attach a Meet URL. Requires
    `conferenceDataVersion=1` on the insert call — without it Google
    silently ignores the conferenceData block and returns an event with
    no Meet link. See https://developers.google.com/calendar/api/v3/reference/events/insert.

    Returns the created event dict (with `id`, `htmlLink`, `hangoutLink`
    if Meet was added). Raises googleapiclient errors on failure — the
    caller (actions.py) catches them and marks the pending_action failed.
    """
    import uuid
    config = _require_config(config)
    svc = _service(config)
    body: dict = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": start_at},
        "end":   {"dateTime": end_at},
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    if add_meet:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": f"watson-{uuid.uuid4().hex[:12]}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    result = svc.events().insert(
        calendarId=calendar_id,
        body=body,
        conferenceDataVersion=1 if add_meet else 0,
        # sendUpdates="all" emails attendees; use "none" for a self-only
        # calendar block. Default here is "all" since a Meet-invite you
        # DON'T tell the invitee about is useless.
        sendUpdates="all" if attendees else "none",
    ).execute()
    log.info("created gcal event %s%s", result.get("id", "?"),
             " with meet" if add_meet else "")
    return result


def execute_action(kind: str, target_id: str, payload: dict):
    """Dispatch table for actions.py — mirrors clickup_client.execute_action.
    Currently only routes `gcal_create_event`. Raises on unknown kind."""
    if kind != "gcal_create_event":
        raise ValueError(f"gcal_client cannot execute action kind {kind!r}")
    config = _require_config()
    draft = payload.get("draft") or {}
    title = draft.get("title") or "(untitled)"
    start_at = draft.get("start_at") or ""
    end_at = draft.get("end_at") or ""
    if not start_at or not end_at:
        raise ValueError(f"gcal_create_event draft missing start_at/end_at: {draft!r}")
    return create_event(
        title=title, start_at=start_at, end_at=end_at,
        attendees=draft.get("attendees") or [],
        description=draft.get("description") or "",
        add_meet=bool(draft.get("add_meet", True)),
        config=config,
    )
