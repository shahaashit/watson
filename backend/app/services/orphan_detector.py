"""Detect managed_tasks whose ClickUp task has been deleted.

If the user deletes a Watson-created ClickUp task from the ClickUp UI, the
local `managed_tasks` row is orphaned and the Work tab shows "(task not
found)". This service notices that and drafts a `close_orphan_card` pending
action so the user can clear the local row via the usual approve flow.

Signal: managed_task.clickup_task_id absent from clickup_tasks_cache AFTER a
fresh sync (the sync also re-fetches tasks referenced by managed_tasks, so
absence is strong) AND a direct live GET returns 404. Anything else (auth
errors, transient network) is treated as inconclusive and skipped.
"""
import json
import logging

import requests

from ..models import now_iso
from . import clickup_client, events, external_errors

log = logging.getLogger("watson.orphan_detector")


def _already_proposed(conn, managed_task_id) -> bool:
    """Don't re-propose an orphan close the user already saw — pending OR
    resolved both stick (matches the other proposers' dedup pattern)."""
    for r in conn.execute(
        "SELECT payload_json FROM pending_actions WHERE kind = 'close_orphan_card'"
    ):
        try:
            p = json.loads(r["payload_json"])
        except Exception:
            continue
        if p.get("managed_task_id") == managed_task_id:
            return True
    return False


def _task_is_deleted(clickup_task_id: str) -> bool:
    """Direct GET on the ClickUp task. Treat 404 as deleted; ANY other
    outcome (success, auth error, timeout) is inconclusive → skip."""
    try:
        clickup_client.get_task(clickup_task_id)
        return False
    except requests.HTTPError as exc:
        return exc.response is not None and exc.response.status_code == 404
    except Exception as exc:
        external_errors.log_failure(
            log, f"orphan check {clickup_task_id}", "clickup", exc
        )
        return False


def detect_orphans(conn) -> int:
    """Find open managed_tasks whose ClickUp task is gone, draft a
    close_orphan_card per orphan. Returns count drafted."""
    rows = conn.execute(
        "SELECT id, clickup_task_id, related_clickup_task_id FROM managed_tasks"
        " WHERE status = 'open'"
    ).fetchall()
    drafted = 0
    for r in rows:
        # cache present → ClickUp knows about it; not orphan
        if conn.execute(
            "SELECT 1 FROM clickup_tasks_cache WHERE task_id = ?", (r["clickup_task_id"],)
        ).fetchone():
            continue
        if _already_proposed(conn, r["id"]):
            continue
        if not _task_is_deleted(r["clickup_task_id"]):
            continue
        payload = {
            "managed_task_id": r["id"],
            "clickup_task_id": r["clickup_task_id"],
            "reason": "ClickUp task no longer exists (404)",
            "auto_proposed": True,
        }
        cur = conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " status, created_at) VALUES (NULL, 'close_orphan_card', ?, ?, 'pending', ?)",
            (r["clickup_task_id"], json.dumps(payload), now_iso()),
        )
        events.record(conn, "proposal",
                      f"Detected orphan task {r['clickup_task_id']} (deleted in ClickUp)",
                      details={"kind": "close_orphan_card"},
                      action_id=cur.lastrowid, task_id=r["clickup_task_id"])
        drafted += 1
    if drafted:
        conn.commit()
        log.info("orphan_detector: drafted %d orphan-close proposal(s)", drafted)
    return drafted


def apply_orphan_close(conn, payload: dict) -> dict:
    """Side-effect for an approved close_orphan_card. Local-only — the
    ClickUp task is already gone, so there's nothing to write back. Just
    flips the managed_task to status='closed' so the Work tab stops
    rendering '(task not found)'."""
    managed_task_id = payload["managed_task_id"]
    conn.execute(
        "UPDATE managed_tasks SET status = 'closed' WHERE id = ?",
        (managed_task_id,),
    )
    return {"managed_task_id": managed_task_id, "closed": True}
