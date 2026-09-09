"""Keep Watson-created ClickUp tasks in sync with the work they represent.

Phase 1: when a managed task's linked ClickUp task is closed/done, draft a
pending action to close the user's own task (approved in the UI — never auto).

Phase 2: also detect when an MR whose branch encodes the linked ClickUp task id
(via the `_clickup<id>` tag convention) has been merged. Same approve-in-UI flow.
"""
import json
import logging

from ..models import now_iso
from . import clickup_client, events, external_errors, gitlab_client

log = logging.getLogger("watson.completion")


def record_managed_task(conn, clickup_task_id, capture_id, related_clickup_task_id,
                        category, related_mr_id=None, additional_mr_ids=None,
                        *, commit=True):
    """Remember a task Watson just created, so it can be synced later.
    `additional_mr_ids` covers the multi-repo grouping case where one task
    represents work spread across several MRs (see mr_review_tracker)."""
    extras = [m for m in (additional_mr_ids or []) if m and m != related_mr_id]
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
        " related_clickup_task_id, related_mr_id, additional_mr_ids,"
        " category, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
        (clickup_task_id, capture_id, related_clickup_task_id, related_mr_id,
         json.dumps(extras) if extras else None, category, now_iso()),
    )
    if commit:
        conn.commit()


def _has_open_close_action(conn, my_task_id) -> bool:
    """Already a pending/executed close drafted for this task? Avoid duplicates."""
    return conn.execute(
        "SELECT 1 FROM pending_actions WHERE kind = 'clickup_close_task'"
        " AND target_id = ? AND status IN ('pending', 'executed')",
        (my_task_id,),
    ).fetchone() is not None


def _completion_signal(conn, related_clickup_id, related_mr_id=None):
    """Return a (reason, payload-extras) tuple if the managed task should close,
    else None. Signals: linked ClickUp task done, OR an MR (linked directly or
    via the `_clickup<id>` branch tag) is merged."""
    # the linked ClickUp task itself is done/closed
    if related_clickup_id:
        try:
            related = clickup_client.get_task(related_clickup_id)
            if clickup_client.task_is_done(related):
                name = related.get("name", related_clickup_id)
                status = (related.get("status") or {}).get("status", "closed")
                return (
                    f"Close this task — linked task “{name}” is {status}.",
                    {"reason": f"Linked task is {status}",
                     "related_task_id": related_clickup_id, "related_task_name": name},
                )
        except Exception as exc:
            external_errors.log_failure(
                log, f"fetch related task {related_clickup_id}", "clickup", exc
            )

    # the directly-linked MR was merged
    if related_mr_id:
        row = conn.execute(
            "SELECT mr_id, title, state, url FROM gitlab_mrs_cache WHERE mr_id = ?",
            (related_mr_id,),
        ).fetchone()
        if row and row["state"] == "merged":
            return (
                f"Close this task — MR “{row['title']}” was merged.",
                {"reason": "Linked MR was merged",
                 "merged_mr_id": row["mr_id"], "merged_mr_title": row["title"],
                 "merged_mr_url": row["url"]},
            )

    # an MR whose branch (or description, as fallback) encodes the ClickUp id
    # has been merged
    if related_clickup_id:
        for m in conn.execute(
            "SELECT mr_id, title, source_branch, description, url FROM gitlab_mrs_cache"
            " WHERE state = 'merged'"
        ):
            if gitlab_client.clickup_id_from_mr(m) == related_clickup_id:
                return (
                    f"Close this task — MR “{m['title']}” was merged.",
                    {"reason": "Linked MR was merged via branch tag",
                     "merged_mr_id": m["mr_id"], "merged_mr_title": m["title"],
                     "merged_mr_url": m["url"]},
                )
    return None


def check_completions(conn) -> int:
    """Scan open managed tasks; draft close actions for any whose linked work
    (ClickUp task closed OR MR merged) is done. Returns count drafted."""
    rows = conn.execute(
        "SELECT * FROM managed_tasks WHERE status = 'open'"
        " AND (related_clickup_task_id IS NOT NULL OR related_mr_id IS NOT NULL)"
    ).fetchall()
    drafted = 0
    for r in rows:
        my_task = r["clickup_task_id"]
        if _has_open_close_action(conn, my_task):
            continue
        signal = _completion_signal(conn, r["related_clickup_task_id"],
                                    r["related_mr_id"] if "related_mr_id" in r.keys() else None)
        if not signal:
            continue
        draft_text, extras = signal
        payload = {"draft": draft_text, **extras}
        cur = conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " status, created_at) VALUES (?, 'clickup_close_task', ?, ?, 'pending', ?)",
            (r["capture_id"], my_task, json.dumps(payload), now_iso()),
        )
        events.record(conn, "proposal",
                      f"Drafted close: {extras.get('reason', 'linked work done')}",
                      details={"kind": "clickup_close_task", **extras},
                      action_id=cur.lastrowid, task_id=my_task)
        drafted += 1
    if drafted:
        conn.commit()
        log.info("completion check: drafted %d close action(s)", drafted)
    return drafted


def mark_managed_closed(conn, my_task_id):
    conn.execute(
        "UPDATE managed_tasks SET status = 'closed' WHERE clickup_task_id = ?",
        (my_task_id,),
    )
