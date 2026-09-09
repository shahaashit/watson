"""Flock Outgoing Webhook receiver.

Flock has no user-level REST API for reading channel messages (see
flock-api-capabilities memory). The one exception is Outgoing Webhooks:
configured at dev.flock.com/webhooks/outgoing per channel, they POST every
message in that channel to a callback URL — WITHOUT firing a read receipt
on the user's real Flock client (unlike our Playwright sidebar-walk fallback).

Setup burden: one Flock webhook per channel you want observed. All webhooks
can point at the same Watson endpoint — we use the `?token=<token>` query
param to look up which channel the message belongs to.

Filter: we only store messages where the text contains the user's Flock
handle (case-insensitive substring on "@<flock_user_handle>") OR @all / @online.
Everything else is 200-acked and dropped, so noisy channels don't fill the DB.
"""
import logging
import re
from datetime import datetime, timedelta

from ..config import settings
from ..models import now_iso
from . import flock_client

log = logging.getLogger("watson.flock_webhook")


# Flock JIDs come with an XMPP-style /resource suffix on `from`
# (e.g. "cc1ma89nnd4jm9vf@go.to/talk.to_MAC_1.0.0.147_nqKMZE").
# Strip the resource for storage — the base JID is what we key on.
_RESOURCE_RE = re.compile(r"/[^/]*$")


def _strip_resource(jid: str) -> str:
    return _RESOURCE_RE.sub("", jid or "")


# ─── channel registry ───────────────────────────────────────────────────

def register_channel(conn, token: str, channel_name: str) -> None:
    """Called by the CLI when the user creates a new Outgoing Webhook.
    Idempotent — re-registering the same token just refreshes the name.
    channel_jid is filled in on first webhook receipt."""
    conn.execute(
        "INSERT INTO flock_webhook_channels (token, channel_name, created_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(token) DO UPDATE SET channel_name = excluded.channel_name",
        (token, channel_name, now_iso()),
    )
    conn.commit()


def unregister_channel(conn, token: str) -> int:
    """Remove a token. Also purges cached mentions for that token so the
    Today view stops showing stale data from a channel we no longer watch."""
    cur = conn.execute("DELETE FROM flock_webhook_channels WHERE token = ?", (token,))
    conn.execute("DELETE FROM flock_webhook_mentions WHERE token = ?", (token,))
    conn.commit()
    return cur.rowcount


def list_channels(conn) -> list:
    return list(conn.execute(
        "SELECT token, channel_name, channel_jid, created_at, last_seen_at"
        " FROM flock_webhook_channels ORDER BY created_at"
    ).fetchall())


def _lookup_channel(conn, token: str):
    return conn.execute(
        "SELECT token, channel_name, channel_jid FROM flock_webhook_channels"
        " WHERE token = ?",
        (token,),
    ).fetchone()


# ─── message filter ─────────────────────────────────────────────────────

def _is_for_user(text: str, user_handle: str) -> bool:
    """Substring match, case-insensitive. Three positive matches:
      - @<flock_user_handle> — the user's own name (e.g. "@Taylor S")
      - @all
      - @online
    Empty flock_user_handle disables the substring check but @all/@online
    still land — the user opted into a firehose, we still honor those tags.
    """
    if not text:
        return False
    lower = text.lower()
    if "@all" in lower or "@online" in lower:
        return True
    handle = (user_handle or "").strip().lower()
    if handle and f"@{handle}" in lower:
        return True
    return False


# ─── receive path (called from the router) ──────────────────────────────

def receive(conn, token: str, payload: dict) -> dict:
    """Handle one webhook POST. Returns a small status dict for logging /
    debug endpoints; never raises — Flock retries on non-2xx, and a hot
    retry loop over a broken filter is worse than a silently dropped msg.
    """
    if not token:
        log.warning("flock webhook: no token in query")
        return {"ok": False, "reason": "missing_token"}

    channel = _lookup_channel(conn, token)
    if channel is None:
        # Someone (or Flock, in a mis-config) hit our endpoint with a token
        # we don't recognize. Don't 4xx — that'd make Flock retry forever.
        log.warning("flock webhook: unknown token %s...", token[:8])
        return {"ok": False, "reason": "unknown_token"}

    config = flock_client.flock_config()
    if config["disabled"]:
        return {"ok": False, "reason": "disabled"}

    # Note: the docs example includes a `type: "message"` field, but real
    # Flock webhook payloads DON'T carry that field. Do NOT require it — an
    # earlier version did and silently swallowed every real message.

    msg_id = payload.get("id") or ""
    sender = _strip_resource(payload.get("from") or "")
    channel_jid = payload.get("to") or ""

    # Effective text: real payloads leave `text` empty when the message
    # contains ONLY an @-mention. `notification` is the rendered display
    # string (e.g. "@all Heyy") and is set in that case. Fall back
    # further to a naive flockml strip so we always store something.
    text = (payload.get("notification") or payload.get("text") or "").strip()
    if not text:
        flockml = payload.get("flockml") or ""
        # Cheap tag strip — a full flockml parse isn't worth it just to
        # rescue the "no plain text at all" case.
        text = re.sub(r"<[^>]+>", " ", flockml).strip()
        text = re.sub(r"\s+", " ", text)

    if not msg_id or not sender or not channel_jid:
        log.warning("flock webhook: malformed payload for %s (missing id/from/to)",
                    channel["channel_name"])
        return {"ok": False, "reason": "malformed"}

    now = now_iso()

    # Opportunistically capture the channel's JID + last-seen on the registry
    # row. First receipt teaches us which JID this token is bound to; every
    # receipt refreshes last_seen so `list` shows recency at a glance.
    if channel["channel_jid"] != channel_jid:
        conn.execute(
            "UPDATE flock_webhook_channels SET channel_jid = ?, last_seen_at = ?"
            " WHERE token = ?",
            (channel_jid, now, token),
        )
    else:
        conn.execute(
            "UPDATE flock_webhook_channels SET last_seen_at = ? WHERE token = ?",
            (now, token),
        )

    if not _is_for_user(text, config["user_handle"]):
        conn.commit()
        return {"ok": True, "matched": False, "channel": channel["channel_name"]}

    # INSERT OR IGNORE — Flock may retry a POST if it doesn't get a 200 in
    # time, and the retry would carry the same `id`. Dedup silently.
    conn.execute(
        "INSERT OR IGNORE INTO flock_webhook_mentions"
        " (id, token, channel_jid, channel_name, sender_jid, text, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, token, channel_jid, channel["channel_name"], sender, text, now),
    )
    conn.commit()
    log.info("flock webhook: stored mention in %s from %s (len=%d)",
             channel["channel_name"], sender, len(text))
    return {"ok": True, "matched": True, "channel": channel["channel_name"]}


# ─── read path (called from /api/today) ─────────────────────────────────

def recent_mentions(conn, hours: int | None = None) -> list:
    """Return recent stored mentions, newest first, within the lookback."""
    lookback_h = hours if hours is not None else settings.flock_lookback_hours
    cutoff = (datetime.now() - timedelta(hours=lookback_h)).isoformat()
    return list(conn.execute(
        "SELECT id, channel_jid, channel_name, sender_jid, text, received_at"
        " FROM flock_webhook_mentions"
        " WHERE received_at >= ?"
        " ORDER BY received_at DESC",
        (cutoff,),
    ).fetchall())


# ─── housekeeping ───────────────────────────────────────────────────────

def prune_old(conn, keep_hours: int = 168) -> int:
    """Delete mentions older than the retention window (default 7 days).
    Called opportunistically on receipt so the table doesn't grow forever."""
    cutoff = (datetime.now() - timedelta(hours=keep_hours)).isoformat()
    cur = conn.execute(
        "DELETE FROM flock_webhook_mentions WHERE received_at < ?", (cutoff,)
    )
    return cur.rowcount


def dismiss_channel_mentions(conn, channel_jid: str) -> int:
    """Clear every cached mention for a channel — the "Dismiss" action on
    the Today card. The token registration stays intact so future incoming
    messages still land; only the current batch is cleared from the view.
    """
    if not channel_jid:
        return 0
    cur = conn.execute(
        "DELETE FROM flock_webhook_mentions WHERE channel_jid = ?", (channel_jid,)
    )
    conn.commit()
    return cur.rowcount
