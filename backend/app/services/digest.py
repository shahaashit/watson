"""Morning digest: today's meetings, due reminders, stale MRs, pending approvals."""
import json
import logging
from datetime import datetime, timedelta

import requests

from ..config import settings
from ..models import now_iso
from . import external_errors, flock_client, gcal_client

log = logging.getLogger("watson.digest")

STALE_MR_DAYS = 2


def due_reminders(conn) -> list:
    end_of_today = datetime.now().replace(hour=23, minute=59, second=59).isoformat(timespec="seconds")
    now = datetime.now().isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM reminders WHERE"
        " (status = 'pending' AND due_at <= ?)"
        " OR (status = 'snoozed' AND snoozed_until <= ?)"
        " ORDER BY due_at",
        (end_of_today, now),
    ).fetchall()


def stale_mrs(conn) -> list:
    cutoff = (datetime.now() - timedelta(days=STALE_MR_DAYS)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM gitlab_mrs_cache WHERE role IN ('reviewer','engaged') AND state = 'opened'"
        " AND updated_at <= ? ORDER BY updated_at",
        (cutoff,),
    ).fetchall()


def pending_actions_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM pending_actions WHERE status = 'pending'"
    ).fetchone()["n"]


def _flock_deep_mentions(row) -> list:
    """Extract the deep-read mentions list from a flock_mentions_cache row.
    The column is JSON-encoded; returns an empty list on any decode issue
    or when the column is missing (e.g., rows from before the migration)."""
    keys = row.keys() if hasattr(row, "keys") else set()
    raw = row["mentions_json"] if "mentions_json" in keys else None
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _fmt_meeting(row) -> str:
    """One digest bullet for a calendar event. All-day events show 'All-day',
    timed events show HH:MM in the configured TZ (start_at is already in that
    TZ from gcal_client._day_bounds)."""
    if row["all_day"]:
        when = "All-day"
    else:
        # start_at is ISO with offset; slice the HH:MM off the T portion.
        raw = row["start_at"] or ""
        when = raw[11:16] if len(raw) >= 16 else raw
    title = row["title"] or "(no title)"
    return f"- {when} — {title}"


def build_digest_text(conn) -> str:
    today = datetime.now().strftime("%A, %d %b %Y")
    lines = [f"Morning digest — {today}", ""]

    meetings = gcal_client.todays_events(conn)
    lines.append(f"**Today's meetings ({len(meetings)})**")
    if meetings:
        for m in meetings:
            lines.append(_fmt_meeting(m))
    else:
        lines.append("- no meetings today")
    lines.append("")

    reminders = due_reminders(conn)
    lines.append(f"**Reminders due ({len(reminders)})**")
    if reminders:
        for r in reminders:
            lines.append(f"- {r['text']} (due {r['due_at'][:16]})")
    else:
        lines.append("- nothing due, clear runway")
    lines.append("")

    mrs = stale_mrs(conn)
    lines.append(f"**MRs awaiting your review >{STALE_MR_DAYS} days ({len(mrs)})**")
    if mrs:
        for m in mrs:
            days = (datetime.now() - datetime.fromisoformat(m["updated_at"])).days
            lines.append(f"- [{m['project']}] {m['title']} — {days}d stale — {m['url']}")
    else:
        lines.append("- review queue is clean")
    lines.append("")

    pending = pending_actions_count(conn)
    lines.append(f"**Pending approvals**: {pending} draft(s) waiting in Watson")
    return "\n".join(lines)


def run_digest(conn) -> int:
    """Store the digest as a note entry tagged 'digest'; optionally webhook it."""
    text = build_digest_text(conn)
    title = f"Morning digest — {datetime.now().strftime('%Y-%m-%d')}"
    cur = conn.execute(
        "INSERT INTO entries (capture_id, type, title, body, people, tags, created_at)"
        " VALUES (NULL, 'note', ?, ?, '[]', ?, ?)",
        (title, text, json.dumps(["digest"]), now_iso()),
    )
    conn.commit()

    if settings.digest_webhook_url:
        try:
            requests.post(settings.digest_webhook_url, json={"text": text}, timeout=10)
        except requests.RequestException as exc:
            external_errors.log_failure(
                log, "digest webhook", "external-service", exc
            )
    return cur.lastrowid
