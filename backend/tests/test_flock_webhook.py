"""Unit tests for the Flock Outgoing Webhook receive path."""
from app.config import settings
from app.services import flock_webhook_client


def _payload(text="hello @Taylor S can you look at this?",
             msg_id="msg-001",
             from_jid="user1@go.to/talk.to_X",
             to_jid="grp1@groups.go.to"):
    """Simple test payload — plain text mode (no @-mentions), so `text` is
    the primary content field. Real Flock payloads use `notification` when
    the message contains mentions (see _mention_payload below)."""
    return {"id": msg_id, "from": from_jid, "to": to_jid, "text": text}


def _mention_payload(notification="@all Heyy",
                     flockml="<flockml><user group=\"all\">@all</user> Heyy</flockml>",
                     msg_id="msg-mention-001",
                     from_jid="u:sender123",
                     to_jid="g:group456"):
    """Matches the payload shape Flock actually sends for messages with
    @-mentions. `text` is empty; `notification` carries the rendered display
    string. Verified from a real Outgoing Webhook POST on 2026-07-13."""
    return {
        "id": msg_id, "from": from_jid, "to": to_jid,
        "text": "", "notification": notification, "flockml": flockml,
        "mentions": ["u:dummyformentions"],
    }


# ─── channel registry ──────────────────────────────────────────────────

def test_register_channel_inserts_row(conn):
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    rows = conn.execute("SELECT * FROM flock_webhook_channels").fetchall()
    assert len(rows) == 1
    assert rows[0]["token"] == "tok-abc"
    assert rows[0]["channel_name"] == "CM Serving"
    assert rows[0]["channel_jid"] is None  # not seen yet


def test_register_channel_is_idempotent(conn):
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving (renamed)")
    rows = conn.execute("SELECT * FROM flock_webhook_channels").fetchall()
    assert len(rows) == 1
    assert rows[0]["channel_name"] == "CM Serving (renamed)"


def test_unregister_removes_channel_and_its_mentions(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    flock_webhook_client.receive(conn, "tok-abc", _payload())
    assert conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_mentions").fetchone()["n"] == 1
    n = flock_webhook_client.unregister_channel(conn, "tok-abc")
    assert n == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_channels").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_mentions").fetchone()["n"] == 0


# ─── receive path ──────────────────────────────────────────────────────

def test_receive_unknown_token_is_ignored(conn):
    result = flock_webhook_client.receive(conn, "tok-nope", _payload())
    assert result["ok"] is False
    assert result["reason"] == "unknown_token"
    assert conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_mentions").fetchone()["n"] == 0


def test_receive_missing_token_is_ignored(conn):
    result = flock_webhook_client.receive(conn, "", _payload())
    assert result["ok"] is False
    assert result["reason"] == "missing_token"


def test_receive_stores_mention_when_handle_matches(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    result = flock_webhook_client.receive(conn, "tok-abc", _payload())
    assert result["ok"] is True
    assert result["matched"] is True
    row = conn.execute("SELECT * FROM flock_webhook_mentions").fetchone()
    assert row["id"] == "msg-001"
    assert row["channel_name"] == "CM Serving"
    assert row["channel_jid"] == "grp1@groups.go.to"
    assert row["sender_jid"] == "user1@go.to"  # resource stripped
    assert "@Taylor S" in row["text"]


def test_receive_drops_message_without_handle(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    result = flock_webhook_client.receive(conn, "tok-abc", _payload(text="just a chat message"))
    assert result["ok"] is True
    assert result["matched"] is False
    assert conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_mentions").fetchone()["n"] == 0


def test_receive_matches_at_all(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    result = flock_webhook_client.receive(conn, "tok-abc", _payload(text="@all standup in 5"))
    assert result["matched"] is True


def test_receive_matches_at_online(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    result = flock_webhook_client.receive(conn, "tok-abc", _payload(text="@online who has a min"))
    assert result["matched"] is True


def test_receive_handle_match_is_case_insensitive(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    result = flock_webhook_client.receive(conn, "tok-abc", _payload(text="hey @TAYLOR S check"))
    assert result["matched"] is True


def test_receive_dedups_on_message_id(conn, monkeypatch):
    """Flock may retry a POST if it doesn't get a 200 in time. The retry
    carries the same message id; we must not insert twice."""
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    flock_webhook_client.receive(conn, "tok-abc", _payload(msg_id="msg-dup"))
    flock_webhook_client.receive(conn, "tok-abc", _payload(msg_id="msg-dup"))
    assert conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_mentions").fetchone()["n"] == 1


def test_receive_populates_channel_jid_on_first_receipt(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    assert conn.execute("SELECT channel_jid FROM flock_webhook_channels").fetchone()["channel_jid"] is None
    flock_webhook_client.receive(conn, "tok-abc", _payload(to_jid="grp1@groups.go.to"))
    row = conn.execute("SELECT channel_jid, last_seen_at FROM flock_webhook_channels").fetchone()
    assert row["channel_jid"] == "grp1@groups.go.to"
    assert row["last_seen_at"] is not None


def test_receive_uses_notification_field_when_text_is_empty(conn, monkeypatch):
    """Real Flock payloads: `text` is "" when the message contains only an
    @-mention; the rendered display string lives in `notification`.
    Verified from a real payload capture on 2026-07-13."""
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    result = flock_webhook_client.receive(conn, "tok-abc", _mention_payload())
    assert result["matched"] is True  # @all triggers unconditionally
    row = conn.execute("SELECT text FROM flock_webhook_mentions").fetchone()
    assert row["text"] == "@all Heyy"


def test_receive_falls_back_to_flockml_strip_when_text_and_notification_empty(conn, monkeypatch):
    """Defense-in-depth: some future payload might have neither `text` nor
    `notification`. Naive flockml tag-strip keeps a text anchor for the DB
    row instead of storing an empty string."""
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    payload = _mention_payload(notification="")
    payload["flockml"] = "<flockml><user data-value=\"@all\">@all</user> from flockml</flockml>"
    result = flock_webhook_client.receive(conn, "tok-abc", payload)
    assert result["matched"] is True
    row = conn.execute("SELECT text FROM flock_webhook_mentions").fetchone()
    assert "@all" in row["text"] and "from flockml" in row["text"]


def test_receive_handles_malformed_payload_without_raising(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    result = flock_webhook_client.receive(conn, "tok-abc", {"type": "message"})
    assert result["ok"] is False
    assert result["reason"] == "malformed"


def test_receive_empty_handle_matches_only_broadcast_tags(conn, monkeypatch):
    """If the user hasn't configured flock_user_handle, we still honor
    @all/@online but never match on names — otherwise every message would
    be surfaced from every channel."""
    monkeypatch.setattr(settings, "flock_user_handle", "")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    assert flock_webhook_client.receive(conn, "tok-abc", _payload(text="@all"))["matched"] is True
    assert flock_webhook_client.receive(
        conn, "tok-abc", _payload(text="hey @Taylor S", msg_id="msg-2"),
    )["matched"] is False


# ─── read path ─────────────────────────────────────────────────────────

def test_recent_mentions_respects_lookback(conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    # store one recent
    flock_webhook_client.receive(conn, "tok-abc", _payload(msg_id="msg-recent"))
    # manually backdate a second one beyond the 24h window
    conn.execute(
        "INSERT INTO flock_webhook_mentions"
        " (id, token, channel_jid, channel_name, sender_jid, text, received_at)"
        " VALUES ('msg-old', 'tok-abc', 'grp1@groups.go.to', 'CM Serving',"
        " 'someone@go.to', '@Taylor S old thing', '2000-01-01T00:00:00')"
    )
    conn.commit()
    rows = flock_webhook_client.recent_mentions(conn, hours=24)
    assert [r["id"] for r in rows] == ["msg-recent"]


# ─── /api/today integration ────────────────────────────────────────────

def test_today_endpoint_includes_webhook_mentions(client, conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    flock_webhook_client.receive(conn, "tok-abc", _payload(text="@Taylor S ping"))
    resp = client.get("/api/today")
    assert resp.status_code == 200
    flock = resp.json()["flock"]
    assert any(
        e["bucket"] == "webhook" and e["name"] == "CM Serving"
        for e in flock
    ), f"expected a webhook-sourced flock entry, got: {flock}"


def test_today_endpoint_aggregates_multiple_messages_per_channel(client, conn, monkeypatch):
    """Three @-mentions in the same channel should become ONE inbox entry
    with three items in `mentions[]` — mirrors the sidebar-DM shape and
    stops the frontend from rendering three noise cards per channel."""
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    for i in range(3):
        flock_webhook_client.receive(conn, "tok-abc", {
            "id": f"msg-{i}", "from": "u:sender", "to": "g:cm-serving",
            "notification": f"@all msg-{i}", "text": "",
        })
    resp = client.get("/api/today")
    webhook_entries = [e for e in resp.json()["flock"] if e["bucket"] == "webhook"]
    assert len(webhook_entries) == 1, "expected one entry per channel, got separate rows"
    assert len(webhook_entries[0]["mentions"]) == 3
    assert webhook_entries[0]["unread_count"] == 3


def test_today_endpoint_resolves_sender_jid_to_display_name(client, conn, monkeypatch):
    """The webhook payload only gives us an opaque sender JID. The today
    router uses flock_contacts (harvested from the sidebar walk) to swap it
    for a display name at render time."""
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    conn.execute(
        "INSERT INTO flock_contacts (jid, name, updated_at)"
        " VALUES ('u:2thc2ap221c2ztc2', 'Nikhil Kumar', '2026-07-13')"
    )
    conn.commit()
    flock_webhook_client.receive(conn, "tok-abc", {
        "id": "msg-x", "from": "u:2thc2ap221c2ztc2", "to": "g:group",
        "notification": "@Taylor S check this", "text": "",
    })
    resp = client.get("/api/today")
    webhook_entries = [e for e in resp.json()["flock"] if e["bucket"] == "webhook"]
    assert len(webhook_entries) == 1
    assert webhook_entries[0]["mentions"][0]["sender_name"] == "Nikhil Kumar"


def test_dismiss_endpoint_clears_channel_mentions_only(client, conn, monkeypatch):
    """Dismissing a channel wipes its cached mentions but leaves the token
    registration intact — future incoming messages should still land."""
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    flock_webhook_client.register_channel(conn, "tok-xyz", "Other Channel")
    flock_webhook_client.receive(conn, "tok-abc", {
        "id": "m-1", "from": "u:s", "to": "g:cm", "notification": "@all one", "text": "",
    })
    flock_webhook_client.receive(conn, "tok-xyz", {
        "id": "m-2", "from": "u:s", "to": "g:other", "notification": "@all two", "text": "",
    })
    resp = client.request("DELETE", "/api/flock/webhook/mentions?channel_jid=g:cm")
    assert resp.status_code == 200
    assert resp.json()["cleared"] == 1
    remaining = conn.execute("SELECT id, channel_jid FROM flock_webhook_mentions").fetchall()
    assert [r["channel_jid"] for r in remaining] == ["g:other"]
    tokens = conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_channels").fetchone()["n"]
    assert tokens == 2, "tokens must remain registered so future msgs still land"


def test_dismiss_endpoint_is_noop_for_unknown_channel(client, conn):
    """Never 4xx on unknown channel_jid — the UI may race between fetch and
    dismiss; a silent zero-clear is the friendly behavior."""
    resp = client.request("DELETE", "/api/flock/webhook/mentions?channel_jid=g:nope")
    assert resp.status_code == 200
    assert resp.json()["cleared"] == 0


def test_today_endpoint_falls_back_to_jid_when_sender_unknown(client, conn, monkeypatch):
    """A sender we haven't seen in the sidebar still surfaces — the JID
    just isn't pretty. Better than dropping the message entirely."""
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    flock_webhook_client.receive(conn, "tok-abc", {
        "id": "msg-y", "from": "u:stranger-jid", "to": "g:group",
        "notification": "@all announcement", "text": "",
    })
    resp = client.get("/api/today")
    entry = [e for e in resp.json()["flock"] if e["bucket"] == "webhook"][0]
    assert entry["mentions"][0]["sender_name"] == "u:stranger-jid"


# ─── endpoint ──────────────────────────────────────────────────────────

def test_flock_webhook_endpoint_accepts_and_stores(client, conn, monkeypatch):
    monkeypatch.setattr(settings, "flock_user_handle", "Taylor S")
    flock_webhook_client.register_channel(conn, "tok-abc", "CM Serving")
    resp = client.post(
        "/api/flock/webhook?token=tok-abc",
        json=_payload(text="hi @Taylor S"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["matched"] is True
    assert conn.execute("SELECT COUNT(*) AS n FROM flock_webhook_mentions").fetchone()["n"] == 1


def test_flock_webhook_endpoint_unknown_token_still_200(client, conn):
    """Flock retries on non-2xx. We must not 4xx on an unknown token."""
    resp = client.post("/api/flock/webhook?token=tok-nope", json=_payload())
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_channels_endpoint_returns_registered_list(client, conn):
    flock_webhook_client.register_channel(conn, "tok-abc-1234567", "CM Serving")
    resp = client.get("/api/flock/webhook/channels")
    assert resp.status_code == 200
    channels = resp.json()["channels"]
    assert len(channels) == 1
    assert channels[0]["token_prefix"] == "tok-abc-"
    assert channels[0]["channel_name"] == "CM Serving"
