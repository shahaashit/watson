import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import (
    action_dict, entry_dict, flock_mention_dict, flock_webhook_mention_dict,
    gcal_event_dict, mr_dict, reminder_dict,
)
from ..services import (
    digest, external_errors, flock_client, flock_webhook_client, gcal_client, user_meta,
)

log = logging.getLogger("watson.today.route")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

router = APIRouter(prefix="/api", tags=["today"])


@router.get("/today")
def today_view(conn=Depends(get_db)):
    reminders = [reminder_dict(r) for r in digest.due_reminders(conn)]
    stale = [mr_dict(r) for r in digest.stale_mrs(conn)]
    all_mrs = [
        mr_dict(r) for r in conn.execute(
            "SELECT * FROM gitlab_mrs_cache WHERE state = 'opened' ORDER BY updated_at"
        )
    ]
    pending = [
        action_dict(r) for r in conn.execute(
            "SELECT * FROM pending_actions WHERE status = 'pending' ORDER BY id DESC"
        )
    ]
    # Enrich task-targeted actions (close, comment, status change) with the
    # target task's name — the raw target_id is an opaque ClickUp id and the
    # UI otherwise renders "Linked task is Closed" with no clue WHICH task.
    target_ids = {p["target_id"] for p in pending
                  if p.get("target_id") and p.get("kind") in
                  ("clickup_close_task", "clickup_comment", "clickup_status")}
    if target_ids:
        marks = ",".join("?" * len(target_ids))
        names = {
            r["task_id"]: r["name"]
            for r in conn.execute(
                f"SELECT task_id, name FROM clickup_tasks_cache WHERE task_id IN ({marks})",
                list(target_ids),
            )
        }
        for p in pending:
            n = names.get(p.get("target_id"))
            if n:
                p["target_task_name"] = n
    meetings = [gcal_event_dict(r) for r in gcal_client.todays_events(conn)]
    flock = []
    for r in flock_client.todays_mentions(conn):
        d = flock_mention_dict(r)
        d["url"] = flock_client.deep_link_for(d["jid"])
        flock.append(d)
    # Merge Outgoing-Webhook-sourced @-mentions (channel messages that name you)
    # into the same array. These come from real-time Flock webhooks — see
    # flock_webhook_client.py — and complement the sidebar-DM entries above.
    #
    # Aggregate by channel: N messages in one channel become ONE inbox entry
    # with N items in `mentions[]`, mirroring the sidebar-DM shape. Rendering
    # them as N separate cards clutters the "Pulling on you" list, and the
    # collapse-by-channel path is already how the frontend expects things.
    #
    # Sender JIDs are opaque (u:2thc2ap...); we resolve them to display names
    # via the sidebar-harvested contacts map. NOTE: the sidebar fiber walk
    # returns JIDs in the "xxx@go.to" format, while webhook payloads use the
    # "u:xxx" format — they're the same user under two different identifiers
    # but there's no known mapping between them without a Flock API call.
    # So lookups mostly miss today; unknown senders fall back to their JID.
    jid_to_name = flock_client.jid_to_name_map(conn)
    by_channel = {}
    for r in flock_webhook_client.recent_mentions(conn):
        key = r["channel_jid"]
        if key not in by_channel:
            base = flock_webhook_mention_dict(r)
            base["mentions"] = []           # replace the single-item list; we'll fill below
            base["unread_count"] = 0        # incremented per message
            by_channel[key] = base
        entry = by_channel[key]
        sender = r["sender_jid"]
        entry["mentions"].append({
            "id": r["id"],
            "sender": sender,
            "sender_name": jid_to_name.get(sender, sender),
            "text": r["text"],
        })
        entry["unread_count"] += 1
        # Track newest last_message_time across all messages in this channel
        if r["received_at"] > (entry["last_message_time"] or ""):
            entry["last_message_time"] = r["received_at"]
    for d in by_channel.values():
        d["url"] = flock_client.deep_link_for(d["jid"])
        flock.append(d)
    digest_row = conn.execute(
        "SELECT * FROM entries WHERE tags LIKE '%\"digest\"%' AND created_at LIKE ?"
        " ORDER BY id DESC LIMIT 1",
        (datetime.now().strftime("%Y-%m-%d") + "%",),
    ).fetchone()
    return {
        "reminders_due": reminders,
        "stale_mrs": stale,
        "mrs": all_mrs,
        "meetings": meetings,
        "flock": flock,
        "pending_actions": pending,
        "pending_count": len(pending),
        "digest": entry_dict(digest_row) if digest_row else None,
        # ISO timestamp of the last completed sync pipeline. The frontend
        # uses this to auto-trigger a sync when a stale tab regains focus
        # (Mac-was-asleep case). Empty string on a fresh install.
        "last_sync_at": user_meta.last_sync_at(conn),
        # Structured, redacted connector health. Cached content remains usable
        # while a source is degraded; Settings owns remediation.
        "integration_health": user_meta.integration_health(conn),
    }


@router.get("/meetings")
def meetings_for_date(date: str):
    """Ad-hoc calendar fetch for a specific YYYY-MM-DD, live from Google
    (not the today-only sqlite cache). Powers the Today timeline's
    back/forward date navigation. TODAY comes from /api/today's `meetings`
    field which reads from cache — hitting this endpoint for today would
    just add a redundant network call.
    """
    if not _DATE_RE.match(date or ""):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    try:
        events = gcal_client.events_for_date(date)
    except Exception as exc:  # noqa: BLE001
        external_errors.log_failure(
            log, f"meetings fetch for {date}", "google-calendar", exc
        )
        # Never 5xx — the timeline should render a helpful empty state
        # rather than a browser error. The client can distinguish "no
        # events" from "fetch failed" via the `error` field.
        return {
            "date": date,
            "meetings": [],
            "error": external_errors.details("google-calendar", exc),
        }
    return {"date": date, "meetings": events}
