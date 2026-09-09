"""Emit system_events rows — the audit trail that powers the unified Log tab.

Any code that changes user-visible state (a draft was created, an approval ran,
an MR merged, a task closed) should record a row here so the Log can surface it.
"""
import json
import logging

from ..models import now_iso

log = logging.getLogger("watson.events")


def record(conn, kind: str, subject: str, *,
           details: dict = None,
           action_id: int = None,
           mr_id: str = None,
           task_id: str = None) -> int:
    """Insert an event row. Returns the new event id. Never raises — audit
    failures must not crash the caller. Caller is responsible for committing."""
    try:
        cur = conn.execute(
            "INSERT INTO system_events (kind, subject, details_json,"
            " related_action_id, related_mr_id, related_task_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, subject, json.dumps(details) if details else None,
             action_id, mr_id, task_id, now_iso()),
        )
        return cur.lastrowid
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        log.warning("could not record event %s: %s", kind, exc)
        return 0
