"""Unit tests for the Flock cache-refresh path.

Playwright is a heavy dependency and requires a real Chromium install
(`playwright install chromium`) — we mock at the `_fetch_inbox` boundary so
tests run in any env, install or not.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import flock_client


# ─── configured() ───────────────────────────────────────────────────────

def test_configured_false_when_profile_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "flock_profile_dir", str(tmp_path / "doesnt-exist"))
    assert flock_client.configured() is False


def test_configured_false_when_profile_empty(tmp_path, monkeypatch):
    (tmp_path / "profile").mkdir()
    monkeypatch.setattr(settings, "flock_profile_dir", str(tmp_path / "profile"))
    assert flock_client.configured() is False


def _profile_with_cookie(tmp_path, *, host, name, encrypted, expires_utc):
    profile = tmp_path / "profile"
    cookie_db = profile / "Default" / "Network" / "Cookies"
    cookie_db.parent.mkdir(parents=True)
    cookies = sqlite3.connect(cookie_db)
    cookies.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, "
        "expires_utc INTEGER)"
    )
    cookies.execute(
        "INSERT INTO cookies (host_key, name, encrypted_value, expires_utc) "
        "VALUES (?, ?, ?, ?)",
        (host, name, encrypted, expires_utc),
    )
    cookies.commit()
    cookies.close()
    return profile


def _chrome_expiry(seconds_from_now):
    chrome_epoch_offset_seconds = 11_644_473_600
    return int(
        (
            datetime.now(timezone.utc).timestamp()
            + chrome_epoch_offset_seconds
            + seconds_from_now
        )
        * 1_000_000
    )


def test_configured_false_for_analytics_only_flock_cookie(tmp_path, monkeypatch):
    profile = _profile_with_cookie(
        tmp_path,
        host=".flock.com",
        name="_ga",
        encrypted=b"analytics",
        expires_utc=_chrome_expiry(3600),
    )
    monkeypatch.setattr(settings, "flock_profile_dir", str(profile))
    assert flock_client.configured() is False


def test_configured_true_for_flock_login_session_cookie(tmp_path, monkeypatch):
    profile = _profile_with_cookie(
        tmp_path,
        host=".flock.com",
        name="flock-login",
        encrypted=b"session",
        expires_utc=0,
    )
    monkeypatch.setattr(settings, "flock_profile_dir", str(profile))
    assert flock_client.configured() is True


def test_configured_false_for_expired_flock_login_cookie(tmp_path, monkeypatch):
    profile = _profile_with_cookie(
        tmp_path,
        host=".flock.com",
        name="flock-login",
        encrypted=b"expired",
        expires_utc=_chrome_expiry(-3600),
    )
    monkeypatch.setattr(settings, "flock_profile_dir", str(profile))
    assert flock_client.configured() is False


def test_configured_true_for_unexpired_persistent_flock_login_cookie(
    tmp_path, monkeypatch
):
    profile = _profile_with_cookie(
        tmp_path,
        host="flock.com",
        name="flock-login",
        encrypted=b"future",
        expires_utc=_chrome_expiry(3600),
    )
    monkeypatch.setattr(settings, "flock_profile_dir", str(profile))
    assert flock_client.configured() is True


def test_configured_false_for_empty_encrypted_flock_login_cookie(
    tmp_path, monkeypatch
):
    profile = _profile_with_cookie(
        tmp_path,
        host=".flock.com",
        name="flock-login",
        encrypted=b"",
        expires_utc=0,
    )
    monkeypatch.setattr(settings, "flock_profile_dir", str(profile))
    assert flock_client.configured() is False


# ─── filter logic (_relevant / _within_lookback) ────────────────────────

def _recent_ms(hours_ago: float = 1) -> int:
    return int((datetime.now() - timedelta(hours=hours_ago)).timestamp() * 1000)


def test_relevant_keeps_mentions():
    assert flock_client._relevant({"hasMention": True, "type": "group"}) is True


def test_relevant_keeps_unread_dm():
    assert flock_client._relevant({"type": "buddy", "unreadCount": 2}) is True


def test_relevant_drops_muted_channel_without_mention():
    assert flock_client._relevant({
        "type": "group", "unreadCount": 42, "hasMention": False, "isMuted": True,
    }) is False


def test_relevant_drops_group_with_unread_but_no_mention():
    """Groups without a mention are noise — mirrors architecture doc §6.5.5."""
    assert flock_client._relevant({"type": "group", "unreadCount": 5, "hasMention": False}) is False


def test_within_lookback_keeps_recent_message():
    assert flock_client._within_lookback({"lastMessageTime": _recent_ms(1)}) is True


def test_within_lookback_drops_old_message(monkeypatch):
    monkeypatch.setattr(settings, "flock_lookback_hours", 24)
    old_ms = int((datetime.now() - timedelta(hours=48)).timestamp() * 1000)
    assert flock_client._within_lookback({"lastMessageTime": old_ms}) is False


def test_within_lookback_drops_missing_time():
    assert flock_client._within_lookback({"lastMessageTime": None}) is False


# ─── sync() ─────────────────────────────────────────────────────────────

def test_sync_inserts_relevant_rows(conn, monkeypatch):
    fake_inbox = [
        {  # mention in a group — keep
            "jid": "grp1@groups.go.to", "name": "adserving-dev", "type": "group",
            "isGroup": True, "hasMention": True, "unreadCount": 3,
            "lastMessageTime": _recent_ms(0.5), "isMuted": False,
            "notifyOn": "MENTIONS", "bucket": "open",
        },
        {  # unread 1:1 — keep
            "jid": "user2@go.to", "name": "Nikhil", "type": "buddy",
            "isGroup": False, "hasMention": False, "unreadCount": 1,
            "lastMessageTime": _recent_ms(2), "isMuted": False,
            "notifyOn": "ALL_MESSAGES", "bucket": "open",
        },
        {  # muted noise channel — drop
            "jid": "grp2@groups.go.to", "name": "ci-bot", "type": "group",
            "isGroup": True, "hasMention": False, "unreadCount": 50,
            "lastMessageTime": _recent_ms(1), "isMuted": True,
            "notifyOn": "MENTIONS", "bucket": "muted",
        },
        {  # old mention — drop (outside lookback)
            "jid": "grp3@groups.go.to", "name": "ancient", "type": "group",
            "isGroup": True, "hasMention": True, "unreadCount": 1,
            "lastMessageTime": int((datetime.now() - timedelta(days=3)).timestamp() * 1000),
            "isMuted": False, "notifyOn": "MENTIONS", "bucket": "open",
        },
    ]
    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: fake_inbox)

    n = flock_client.sync(conn)
    assert n == 2
    rows = conn.execute(
        "SELECT jid, name, has_mention, is_group FROM flock_mentions_cache ORDER BY jid"
    ).fetchall()
    jids = [r["jid"] for r in rows]
    assert "grp1@groups.go.to" in jids
    assert "user2@go.to" in jids
    assert "grp2@groups.go.to" not in jids
    assert "grp3@groups.go.to" not in jids


def test_sync_replaces_previous_snapshot(conn, monkeypatch):
    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: [{
        "jid": "old@go.to", "name": "Old", "type": "buddy", "isGroup": False,
        "hasMention": False, "unreadCount": 1, "lastMessageTime": _recent_ms(1),
        "isMuted": False, "notifyOn": "", "bucket": "open",
    }])
    flock_client.sync(conn)

    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: [{
        "jid": "new@go.to", "name": "New", "type": "buddy", "isGroup": False,
        "hasMention": False, "unreadCount": 2, "lastMessageTime": _recent_ms(0.5),
        "isMuted": False, "notifyOn": "", "bucket": "open",
    }])
    flock_client.sync(conn)

    jids = [r["jid"] for r in conn.execute("SELECT jid FROM flock_mentions_cache")]
    assert jids == ["new@go.to"]


def test_sync_returns_zero_on_empty_inbox(conn, monkeypatch):
    """[] from _fetch_inbox means 'fetched successfully, no relevant items' —
    correct behavior is to wipe the cache. Distinct from None (fetch failed)."""
    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: [])
    assert flock_client.sync(conn) == 0


def test_sync_preserves_cache_on_fetch_failure(conn, monkeypatch):
    """None from _fetch_inbox means 'fetch failed' (subprocess timeout,
    sidebar not visible, non-JSON output, etc). sync() must keep the
    existing cache rather than blanking it — a transient Flock hiccup
    shouldn't wipe the user's Today section for the next hour.
    """
    # Seed the cache with a row from a previous successful sync.
    conn.execute(
        "INSERT INTO flock_mentions_cache (jid, name, is_group, has_mention,"
        " unread_count, last_message_time, is_muted, notify_on, bucket,"
        " mentions_json, synced_at) VALUES (?, 'Nikhil', 0, 1, 3, ?, 0, '', 'open', '[]', ?)",
        ("nikhil@go.to", "2026-07-10T09:00:00+05:30", "2026-07-10T09:00:00"),
    )
    conn.commit()
    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: None)
    # The cache stays readable, but callers must receive a safe failure signal
    # so structured health can show that the snapshot is stale.
    with pytest.raises(flock_client.FlockRefreshError):
        flock_client.sync(conn)
    # The row should still be there — cache was NOT blanked.
    rows = conn.execute("SELECT jid FROM flock_mentions_cache").fetchall()
    assert [r["jid"] for r in rows] == ["nikhil@go.to"]


# ─── contacts population from sidebar walk ─────────────────────────────

def test_sync_populates_contacts_from_buddy_rows(conn, monkeypatch):
    """Every 1:1 buddy row in the sidebar becomes a flock_contacts entry so
    webhook-sourced mentions can resolve sender JIDs → display names."""
    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: [
        {"jid": "u:alice", "name": "Alice A", "type": "buddy",
         "isGroup": False, "hasMention": False, "unreadCount": 1,
         "lastMessageTime": _recent_ms(1), "isMuted": False,
         "notifyOn": "", "bucket": "open"},
        {"jid": "u:bob", "name": "Bob B", "type": "buddy",
         "isGroup": False, "hasMention": True, "unreadCount": 0,
         "lastMessageTime": _recent_ms(2), "isMuted": False,
         "notifyOn": "", "bucket": "open"},
        {"jid": "g:some-group", "name": "Some Group", "type": "group",
         "isGroup": True, "hasMention": False, "unreadCount": 0,
         "lastMessageTime": _recent_ms(1), "isMuted": False,
         "notifyOn": "", "bucket": "open"},
    ])
    flock_client.sync(conn)
    rows = conn.execute("SELECT jid, name FROM flock_contacts ORDER BY jid").fetchall()
    assert [(r["jid"], r["name"]) for r in rows] == [
        ("u:alice", "Alice A"), ("u:bob", "Bob B"),
    ]  # groups deliberately excluded — only 1:1 buddies feed sender lookup


def test_sync_upserts_contact_when_name_changes(conn, monkeypatch):
    """A buddy renaming themselves in Flock should propagate on next sync."""
    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: [
        {"jid": "u:alice", "name": "Alice A", "type": "buddy",
         "isGroup": False, "hasMention": False, "unreadCount": 1,
         "lastMessageTime": _recent_ms(1), "isMuted": False,
         "notifyOn": "", "bucket": "open"},
    ])
    flock_client.sync(conn)
    monkeypatch.setattr(flock_client, "_fetch_inbox", lambda *a, **k: [
        {"jid": "u:alice", "name": "Alice Anderson", "type": "buddy",
         "isGroup": False, "hasMention": False, "unreadCount": 1,
         "lastMessageTime": _recent_ms(1), "isMuted": False,
         "notifyOn": "", "bucket": "open"},
    ])
    flock_client.sync(conn)
    row = conn.execute("SELECT name FROM flock_contacts WHERE jid = 'u:alice'").fetchone()
    assert row["name"] == "Alice Anderson"


def test_jid_to_name_map_returns_all_contacts(conn):
    conn.execute("INSERT INTO flock_contacts (jid, name, updated_at) VALUES ('u:x', 'Xavi', '2026-07-13')")
    conn.execute("INSERT INTO flock_contacts (jid, name, updated_at) VALUES ('u:y', 'Yasmin', '2026-07-13')")
    conn.commit()
    assert flock_client.jid_to_name_map(conn) == {"u:x": "Xavi", "u:y": "Yasmin"}


# ─── deep_link_for ──────────────────────────────────────────────────────

def test_deep_link_for_jid():
    assert flock_client.deep_link_for("abc@go.to").endswith("#/chat/abc@go.to")


def test_deep_link_for_empty_falls_back(monkeypatch):
    assert flock_client.deep_link_for("") == settings.flock_url


# ─── /api/today integration ─────────────────────────────────────────────

def test_today_endpoint_returns_flock(client, conn):
    conn.execute(
        "INSERT INTO flock_mentions_cache (jid, name, is_group, has_mention,"
        " unread_count, last_message_time, is_muted, notify_on, bucket, synced_at)"
        " VALUES (?, 'adserving', 1, 1, 3, ?, 0, 'MENTIONS', 'open', '2026-01-01')",
        ("grp@groups.go.to", datetime.now().isoformat()),
    )
    conn.commit()
    resp = client.get("/api/today")
    assert resp.status_code == 200
    flock = resp.json()["flock"]
    assert len(flock) == 1
    assert flock[0]["name"] == "adserving"
    assert flock[0]["has_mention"] is True
    assert flock[0]["url"].endswith("#/chat/grp@groups.go.to")


# ─── digest inclusion ───────────────────────────────────────────────────

def test_digest_hides_flock_even_with_cached_messages(conn):
    from app.services import digest
    conn.execute(
        "INSERT INTO flock_mentions_cache (jid, name, is_group, has_mention,"
        " unread_count, last_message_time, is_muted, notify_on, bucket, synced_at)"
        " VALUES (?, 'Nikhil', 0, 0, 2, ?, 0, '', 'open', '2026-01-01')",
        ("nikhil@go.to", datetime.now().isoformat()),
    )
    conn.commit()
    text = digest.build_digest_text(conn)
    assert "Flock" not in text
    assert "Nikhil" not in text
    assert "2 unread" not in text


def test_digest_shows_empty_state_when_no_flock(conn):
    from app.services import digest
    text = digest.build_digest_text(conn)
    assert "Flock" not in text
