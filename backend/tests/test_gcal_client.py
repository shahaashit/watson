"""Unit tests for the GCal cache-refresh path.

The Google API SDK is NOT installed by default in test venvs — we mock at the
`_fetch_todays_events` boundary rather than at the SDK boundary, so the tests
run whether google-api-python-client is installed or not.
"""
import json
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services import gcal_client, secret_store


@pytest.fixture(autouse=True)
def fake_google_keychain(monkeypatch):
    values = {}
    monkeypatch.setattr(secret_store, "get_secret", lambda name: values.get(name, ""))
    monkeypatch.setattr(secret_store, "has_secret", lambda name: name in values)
    monkeypatch.setattr(
        secret_store, "set_secret", lambda name, value: values.__setitem__(name, value)
    )
    return values


# ─── configured() ───────────────────────────────────────────────────────

def test_configured_false_when_files_missing(tmp_path, monkeypatch):
    # Point both settings at scratch paths that don't exist.
    monkeypatch.setattr(settings, "gcal_credentials_path", str(tmp_path / "creds.json"))
    monkeypatch.setattr(settings, "gcal_token_path", str(tmp_path / "tok.json"))
    assert gcal_client.configured() is False


def test_configured_ignores_legacy_files_until_explicit_import(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    tok = tmp_path / "tok.json"
    creds.write_text("{}")
    tok.write_text("{}")
    monkeypatch.setattr(settings, "gcal_credentials_path", str(creds))
    monkeypatch.setattr(settings, "gcal_token_path", str(tok))
    assert gcal_client.configured() is False


def test_configured_true_when_keychain_has_both_google_secrets(fake_google_keychain):
    fake_google_keychain["google.client_config"] = "client-json"
    fake_google_keychain["google.authorized_user"] = "authorized-json"

    assert gcal_client.configured() is True


def test_load_credentials_prefers_keychain_authorized_user(
    tmp_path, monkeypatch, fake_google_keychain
):
    from google.oauth2 import credentials as google_credentials

    token_file = tmp_path / "legacy-token.json"
    token_file.write_text('{"refresh_token":"legacy-file-secret"}')
    monkeypatch.setattr(settings, "gcal_token_path", str(token_file))
    authorized_info = {
        "refresh_token": "keychain-refresh-token",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }
    fake_google_keychain["google.authorized_user"] = json.dumps(authorized_info)
    calls = {}

    class Credentials:
        expired = False
        refresh_token = "keychain-refresh-token"

    def from_authorized_user_info(info, scopes):
        calls["info"] = info
        calls["scopes"] = scopes
        return Credentials()

    monkeypatch.setattr(
        google_credentials.Credentials,
        "from_authorized_user_info",
        from_authorized_user_info,
    )
    monkeypatch.setattr(
        google_credentials.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: pytest.fail("legacy token file was read"),
    )

    credentials = gcal_client._load_credentials()

    assert isinstance(credentials, Credentials)
    assert calls == {
        "info": authorized_info,
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
    }


def test_refreshed_google_credentials_are_written_back_only_to_keychain(
    tmp_path, monkeypatch, fake_google_keychain
):
    from google.auth.transport import requests as google_requests
    from google.oauth2 import credentials as google_credentials

    token_file = tmp_path / "legacy-token.json"
    token_file.write_text("legacy-file-must-remain")
    monkeypatch.setattr(settings, "gcal_token_path", str(token_file))
    fake_google_keychain["google.authorized_user"] = "{}"

    class Request:
        def __call__(self, **_kwargs):
            return object()

    class Credentials:
        expired = True
        refresh_token = "refresh-token"

        def refresh(self, request):
            request(method="POST", url="https://oauth.example/token")

        def to_json(self):
            return '{"token":"refreshed-secret"}'

    monkeypatch.setattr(google_requests, "Request", Request)
    monkeypatch.setattr(
        google_credentials.Credentials,
        "from_authorized_user_info",
        lambda *_args, **_kwargs: Credentials(),
    )

    gcal_client._load_credentials(timeout_seconds=20)

    assert fake_google_keychain["google.authorized_user"] == (
        '{"token":"refreshed-secret"}'
    )
    assert token_file.read_text() == "legacy-file-must-remain"


def test_load_credentials_redacts_third_party_errors(
    monkeypatch, fake_google_keychain
):
    from google.oauth2 import credentials as google_credentials

    secret = "authorized-user-material-from-library-error"
    fake_google_keychain["google.authorized_user"] = json.dumps(
        {"refresh_token": secret}
    )
    monkeypatch.setattr(
        google_credentials.Credentials,
        "from_authorized_user_info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    with pytest.raises(gcal_client.GoogleCredentialError) as error:
        gcal_client._load_credentials()

    assert secret not in str(error.value)


# ─── _normalize_event ───────────────────────────────────────────────────

def test_normalize_timed_event():
    raw = {
        "id": "abc123",
        "summary": "platform planning",
        "description": "weekly sync",
        "location": "Zoom",
        "start": {"dateTime": "2026-07-10T10:30:00+05:30"},
        "end": {"dateTime": "2026-07-10T11:00:00+05:30"},
        "organizer": {"email": "priya@example.com", "displayName": "Priya"},
        "attendees": [
            {"email": "me@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "nikhil@example.com"},
        ],
        "htmlLink": "https://calendar.google.com/event?id=abc",
    }
    n = gcal_client._normalize_event(raw)
    assert n["event_id"] == "abc123"
    assert n["title"] == "platform planning"
    assert n["all_day"] == 0
    assert n["start_at"] == "2026-07-10T10:30:00+05:30"
    assert n["organizer"] == "Priya"
    assert n["my_response"] == "accepted"
    assert json.loads(n["attendees"]) == ["nikhil@example.com"]  # 'self' stripped


def test_normalize_all_day_event():
    raw = {
        "id": "d1",
        "summary": "Company holiday",
        "start": {"date": "2026-07-10"},
        "end": {"date": "2026-07-11"},
    }
    n = gcal_client._normalize_event(raw)
    assert n["all_day"] == 1
    assert n["start_at"] == "2026-07-10"
    assert n["end_at"] == "2026-07-11"


def test_normalize_missing_organizer_and_attendees():
    n = gcal_client._normalize_event({
        "id": "x",
        "summary": "Solo work block",
        "start": {"dateTime": "2026-07-10T09:00:00+05:30"},
        "end": {"dateTime": "2026-07-10T10:00:00+05:30"},
    })
    assert n["organizer"] == ""
    assert json.loads(n["attendees"]) == []
    assert n["my_response"] == ""


# ─── sync() ─────────────────────────────────────────────────────────────

def _today_iso(hour: int, minute: int = 0) -> str:
    """Build an RFC3339 string for today at the given local hour so events
    land inside the sync's day-window filter."""
    tz = gcal_client._tz()
    now = datetime.now().astimezone(tz)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def test_sync_inserts_events(conn, monkeypatch):
    fake = [
        {
            "event_id": "e1", "calendar_id": "primary",
            "title": "Standup", "description": None, "location": None,
            "start_at": _today_iso(10), "end_at": _today_iso(10, 15),
            "all_day": 0, "organizer": "Priya", "attendees": "[]",
            "my_response": "accepted", "html_link": "https://cal/e1",
        },
        {
            "event_id": "e2", "calendar_id": "primary",
            "title": "1:1", "description": None, "location": None,
            "start_at": _today_iso(15), "end_at": _today_iso(15, 30),
            "all_day": 0, "organizer": "Manager", "attendees": "[]",
            "my_response": "accepted", "html_link": "https://cal/e2",
        },
    ]
    monkeypatch.setattr(gcal_client, "_fetch_todays_events", lambda: (fake, "", []))
    n = gcal_client.sync(conn)
    assert n == 2
    rows = conn.execute("SELECT event_id, title FROM gcal_events_cache ORDER BY event_id").fetchall()
    assert [r["event_id"] for r in rows] == ["e1", "e2"]


def test_sync_replaces_todays_events(conn, monkeypatch):
    # First sync: two events.
    monkeypatch.setattr(gcal_client, "_fetch_todays_events", lambda: ([
        {"event_id": "old", "calendar_id": "primary", "title": "Cancelled",
         "description": None, "location": None,
         "start_at": _today_iso(11), "end_at": _today_iso(12),
         "all_day": 0, "organizer": "", "attendees": "[]",
         "my_response": "accepted", "html_link": ""},
    ], "", []))
    gcal_client.sync(conn)
    # Second sync: different event set for the same day — first should be gone.
    monkeypatch.setattr(gcal_client, "_fetch_todays_events", lambda: ([
        {"event_id": "fresh", "calendar_id": "primary", "title": "New",
         "description": None, "location": None,
         "start_at": _today_iso(11), "end_at": _today_iso(12),
         "all_day": 0, "organizer": "", "attendees": "[]",
         "my_response": "accepted", "html_link": ""},
    ], "", []))
    n = gcal_client.sync(conn)
    assert n == 1
    rows = conn.execute("SELECT event_id FROM gcal_events_cache").fetchall()
    assert [r["event_id"] for r in rows] == ["fresh"]


def test_fetch_drops_declined_events(monkeypatch):
    """The declined-event filter lives in _fetch_todays_events, not sync — we
    stub _service and verify _fetch drops declined rows before returning."""
    class _Events:
        def list(self, **kw):
            class _Req:
                def execute(_self):
                    return {"items": [
                        {"id": "keep", "summary": "Keep me",
                         "start": {"dateTime": _today_iso(10)},
                         "end": {"dateTime": _today_iso(11)},
                         "attendees": [{"email": "me@ex.com", "self": True,
                                        "responseStatus": "accepted"}]},
                        {"id": "drop", "summary": "Skip me",
                         "start": {"dateTime": _today_iso(14)},
                         "end": {"dateTime": _today_iso(15)},
                         "attendees": [{"email": "me@ex.com", "self": True,
                                        "responseStatus": "declined"}]},
                    ]}
            return _Req()

    class _Svc:
        def events(self):
            return _Events()

    monkeypatch.setattr(gcal_client, "_service", lambda: _Svc())
    events, _email, _raw = gcal_client._fetch_todays_events()
    assert [e["event_id"] for e in events] == ["keep"]


# ─── todays_events (used by digest + today router) ──────────────────────

def test_todays_events_returns_full_day_including_past(conn):
    """todays_events returns everything overlapping today — past AND future.
    The timeline UI dims past events; a "next up" widget can filter client-side.
    Only truly out-of-day events (yesterday or tomorrow) are excluded."""
    tz = gcal_client._tz()
    now = datetime.now().astimezone(tz)
    past_start = (now - timedelta(hours=2)).isoformat()
    past_end = (now - timedelta(hours=1)).isoformat()
    fut_start = (now + timedelta(hours=1)).isoformat()
    fut_end = (now + timedelta(hours=2)).isoformat()
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, synced_at) VALUES (?, 'primary', 'past', ?, ?, 0, '[]', ?)",
        ("past", past_start, past_end, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, synced_at) VALUES (?, 'primary', 'future', ?, ?, 0, '[]', ?)",
        ("future", fut_start, fut_end, "2026-01-01"),
    )
    conn.commit()
    rows = gcal_client.todays_events(conn)
    ids = sorted(r["event_id"] for r in rows)
    assert ids == ["future", "past"]


def test_todays_events_includes_multi_day_all_day_event(conn):
    """An all-day event that started earlier this week and ends after today
    still counts as 'today'. Uses a non-leave title so the leave filter
    (see test_todays_events_filters_leave_markers) doesn't drop it."""
    tz = gcal_client._tz()
    now = datetime.now().astimezone(tz)
    start_date = (now - timedelta(days=4)).date().isoformat()
    end_date = (now + timedelta(days=2)).date().isoformat()
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, synced_at)"
        " VALUES (?, 'primary', 'Conference — Bangalore', ?, ?, 1, '[]', ?)",
        ("multi", start_date, end_date, "2026-01-01"),
    )
    conn.commit()
    rows = gcal_client.todays_events(conn)
    assert any(r["event_id"] == "multi" for r in rows), (
        f"multi-day all-day event should overlap today; got {[r['event_id'] for r in rows]}"
    )


def test_todays_events_filters_leave_markers(conn):
    """Events with leave/OOO/PTO in the title AND a long duration (>=6h)
    are filtered out — covers both all-day markers and multi-day timed
    blocks (some folks book leave as 9am-6pm timed events across days).
    A short 30-min 1:1 that happens to say 'leave' stays visible."""
    tz = gcal_client._tz()
    now = datetime.now().astimezone(tz)
    today = now.date().isoformat()
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    day_after = (now + timedelta(days=3)).date().isoformat()
    hh = now.replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
    hh_end = now.replace(hour=14, minute=30, second=0, microsecond=0).isoformat()
    # timed multi-day leave block — 9am today → 6pm two days from now
    long_start = now.replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
    long_end   = (now + timedelta(days=2)).replace(hour=18, minute=0, second=0, microsecond=0).isoformat()

    # All-day leave marker — should be filtered.
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, synced_at)"
        " VALUES ('leave-avi', 'primary', 'On leave - avi.s', ?, ?, 1, '[]', '2026-01-01')",
        (today, tomorrow),
    )
    # All-day PTO — should be filtered.
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, synced_at)"
        " VALUES ('pto', 'primary', 'Morgan PTO', ?, ?, 1, '[]', '2026-01-01')",
        (today, tomorrow),
    )
    # Multi-day TIMED leave block (all_day=0, but 3 days long) — must be filtered.
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, synced_at)"
        " VALUES ('timed-leave', 'primary', 'Vacation — Goa trip', ?, ?, 0, '[]', '2026-01-01')",
        (long_start, long_end),
    )
    # Timed 30-min 1:1 with the word "leave" in title — should STAY visible
    # (short duration, plausibly a real meeting about leave policy).
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, synced_at)"
        " VALUES ('discuss-leave', 'primary', 'Discuss leave policy', ?, ?, 0, '[]', '2026-01-01')",
        (hh, hh_end),
    )
    conn.commit()
    rows = gcal_client.todays_events(conn)
    ids = {r["event_id"] for r in rows}
    assert "leave-avi" not in ids
    assert "pto" not in ids
    assert "timed-leave" not in ids
    assert "discuss-leave" in ids


# ─── /api/today integration ─────────────────────────────────────────────

def test_today_endpoint_returns_meetings(client, conn):
    tz = gcal_client._tz()
    now = datetime.now().astimezone(tz)
    start = (now + timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=2)).isoformat()
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " end_at, all_day, attendees, my_response, html_link, synced_at)"
        " VALUES ('m1', 'primary', 'Design review', ?, ?, 0, '[]', 'accepted', 'https://c/1', '2026-01-01')",
        (start, end),
    )
    conn.commit()
    resp = client.get("/api/today")
    assert resp.status_code == 200
    meetings = resp.json()["meetings"]
    assert len(meetings) == 1
    assert meetings[0]["title"] == "Design review"
    assert meetings[0]["all_day"] is False


# ─── digest inclusion ───────────────────────────────────────────────────

def test_digest_text_includes_meetings_section(conn):
    from app.services import digest
    tz = gcal_client._tz()
    now = datetime.now().astimezone(tz)
    start = (now + timedelta(hours=1)).isoformat()
    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, calendar_id, title, start_at,"
        " all_day, attendees, synced_at) VALUES ('a', 'primary', 'Playback sync', ?, 0, '[]', '2026-01-01')",
        (start,),
    )
    conn.commit()
    text = digest.build_digest_text(conn)
    assert "Today's meetings" in text
    assert "Playback sync" in text


def test_digest_shows_empty_state_when_no_meetings(conn):
    from app.services import digest
    text = digest.build_digest_text(conn)
    assert "Today's meetings (0)" in text
    assert "no meetings today" in text
