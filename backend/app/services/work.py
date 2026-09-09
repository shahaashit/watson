"""Managed-task backfill helpers used by the sync pipeline.

Historically this module also served the Work-tab view; that surface is gone
and the aggregation code with it. What remains are the two backfill routines
sync_pipeline calls each tick to keep managed_tasks in sync with the world
(recover missing related_clickup_task_id from MR descriptions; adopt any
pre-existing Watson-authored tasks into managed_tasks).
"""
import logging

from ..models import now_iso
from . import clickup_client, external_errors, gitlab_client

log = logging.getLogger("watson.work")


# ── backfill: recover related_clickup_task_id when the branch tag was bad ─

def backfill_related_clickup_ids(conn) -> int:
    """For managed_tasks that never got a `related_clickup_task_id` (e.g. because
    the MR's branch tag was typoed — `_clickvp` instead of `_clickup` — so the
    link wasn't resolvable at draft time), retry using the MR's description URL
    as a fallback signal. Local-only reconciliation — no external writes.
    Idempotent: a filled row is skipped on the next tick."""
    updated = 0
    rows = conn.execute(
        "SELECT mt.id, mt.related_mr_id, mr.source_branch, mr.description"
        " FROM managed_tasks mt"
        " JOIN gitlab_mrs_cache mr ON mt.related_mr_id = mr.mr_id"
        " WHERE mt.related_clickup_task_id IS NULL"
        "   AND mt.related_mr_id IS NOT NULL"
    ).fetchall()
    for r in rows:
        candidate = gitlab_client.clickup_id_from_mr(r)
        if not candidate:
            continue
        # accept only ids that actually exist in the ClickUp cache — rejects
        # noise like a false-positive alphanumeric match in the description.
        if not conn.execute(
            "SELECT 1 FROM clickup_tasks_cache WHERE task_id = ?", (candidate,)
        ).fetchone():
            continue
        conn.execute(
            "UPDATE managed_tasks SET related_clickup_task_id = ? WHERE id = ?",
            (candidate, r["id"]),
        )
        updated += 1
    if updated:
        conn.commit()
        log.info("backfill_related_clickup_ids: filled %d row(s)", updated)
    return updated


# ── backfill: turn pre-existing Watson-created tasks into managed_tasks ────

def _likely_watson_authored(task: dict, my_id) -> bool:
    """Heuristic: a task is Watson-created if it's in our create-list, was
    assigned to the user, and its name starts with one of Watson's category
    prefixes ('Discussion - ', 'Review - ', 'Work - ', 'Meeting - ', 'Status - ').
    The prefix is the strongest signal — other tasks in the same list won't have it."""
    assignees = [a.get("id") for a in (task.get("assignees") or [])]
    if my_id not in assignees:
        return False
    name = task.get("name", "")
    return any(name.startswith(p) for p in ("Discussion - ", "Review - "))


def _related_id_from_task(task: dict) -> str:
    """Watson writes 'Related to: <name> (<task_id>)' into the description; pull
    that id back out for managed_tasks.related_clickup_task_id."""
    import re
    desc = task.get("description") or ""
    m = re.search(r"Related to:.*?\(([A-Za-z0-9]{4,})\)", desc)
    return m.group(1) if m else None


def _category_from_name(name: str):
    if " - " in name:
        return name.split(" - ", 1)[0]
    return None


def backfill_managed_tasks(conn) -> dict:
    """Scan the configured ClickUp create-list, identify tasks Watson plausibly
    authored (prefix + assignee), and record any that aren't already managed.
    Idempotent — safe to run repeatedly."""
    from ..config import settings
    list_id = settings.clickup_create_list
    if not list_id:
        return {"added": 0, "skipped": 0, "reason": "no CLICKUP_CREATE_LIST_ID configured"}
    try:
        my_id = clickup_client.current_user_id()
    except Exception:
        return {
            "added": 0,
            "skipped": 0,
            "reason": external_errors.message("clickup"),
        }

    existing = {r["clickup_task_id"] for r in conn.execute(
        "SELECT clickup_task_id FROM managed_tasks")}

    added, skipped, scanned = 0, 0, 0
    import requests
    page = 0
    while True:
        resp = requests.get(
            f"{clickup_client.BASE}/list/{list_id}/task",
            headers={"Authorization": settings.clickup_api_token},
            params={"page": page, "include_closed": "true",
                    "assignees[]": str(my_id)},
            timeout=clickup_client.TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json().get("tasks", [])
        for t in batch:
            scanned += 1
            if t["id"] in existing or not _likely_watson_authored(t, my_id):
                skipped += 1
                continue
            related = _related_id_from_task(t)
            category = _category_from_name(t.get("name", ""))
            status = (t.get("status") or {}).get("type", "open")
            mt_status = "closed" if status in ("closed", "done") else "open"
            conn.execute(
                "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
                " related_clickup_task_id, category, status, created_at)"
                " VALUES (?, NULL, ?, ?, ?, ?)",
                (t["id"], related, category, mt_status, now_iso()),
            )
            existing.add(t["id"])
            added += 1
        if len(batch) < 100:
            break
        page += 1
    conn.commit()
    log.info("backfill_managed_tasks: scanned=%d added=%d skipped=%d",
             scanned, added, skipped)
    return {"added": added, "skipped": skipped, "scanned": scanned}
