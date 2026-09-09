"""Post-hoc twin detector for managed_tasks.

If two reviewer MRs across repos for the same requirement get drafted/approved
BEFORE `mr_review_tracker`'s grouping fires (e.g. drafts that existed before
the grouping feature shipped, or any timing window where the branch-tag signal
wasn't visible), the user ends up with two managed_tasks sharing the same
`related_clickup_task_id`. This service finds those twins each sync and drafts
a `merge_managed_tasks` pending action so the user can collapse them with one
click — approve-before-write preserved.
"""
import json
import logging

from ..models import now_iso
from . import events

log = logging.getLogger("watson.merge_proposer")


def _work_item_for_clickup(conn, task_id):
    if not task_id:
        return None
    row = conn.execute(
        "SELECT work_item_id FROM work_links "
        "WHERE source_type='clickup' AND external_id=?",
        (task_id,),
    ).fetchone()
    return row["work_item_id"] if row else None


def _already_proposed(conn, survivor_id, duplicate_id) -> bool:
    """Don't re-propose what the user already saw — pending OR resolved
    (rejected/executed) both stick, matching the other proposers' pattern."""
    for r in conn.execute(
        "SELECT payload_json FROM pending_actions WHERE kind = 'merge_managed_tasks'"
    ):
        try:
            p = json.loads(r["payload_json"])
        except Exception:
            continue
        if (p.get("survivor_managed_id") == survivor_id
                and p.get("duplicate_managed_id") == duplicate_id):
            return True
    return False


def _task_name(conn, clickup_task_id):
    row = conn.execute(
        "SELECT name FROM clickup_tasks_cache WHERE task_id = ?",
        (clickup_task_id,),
    ).fetchone()
    return (row["name"] if row else None) or clickup_task_id


def propose_merges(conn) -> int:
    """Find groups of open managed_tasks sharing a non-null
    related_clickup_task_id. For each group with ≥ 2 tasks, propose merging
    each non-survivor into the survivor (lowest id = first created)."""
    groups = {}
    for r in conn.execute(
        "SELECT id, clickup_task_id, related_clickup_task_id, related_mr_id"
        " FROM managed_tasks"
        " WHERE status = 'open' AND related_clickup_task_id IS NOT NULL"
        " ORDER BY id"
    ):
        groups.setdefault(r["related_clickup_task_id"], []).append(dict(r))

    drafted = 0
    for parent_id, members in groups.items():
        if len(members) < 2:
            continue
        survivor = members[0]
        survivor_name = _task_name(conn, survivor["clickup_task_id"])

        for dup in members[1:]:
            if _already_proposed(conn, survivor["id"], dup["id"]):
                continue
            payload = {
                "survivor_managed_id": survivor["id"],
                "survivor_clickup_task_id": survivor["clickup_task_id"],
                "survivor_name": survivor_name,
                "duplicate_managed_id": dup["id"],
                "duplicate_clickup_task_id": dup["clickup_task_id"],
                "duplicate_name": _task_name(conn, dup["clickup_task_id"]),
                "duplicate_mr_id": dup["related_mr_id"],
                "parent_clickup_task_id": parent_id,
                "auto_proposed": True,
            }
            cur = conn.execute(
                "INSERT INTO pending_actions (capture_id, kind, target_id,"
                " payload_json, status, created_at)"
                " VALUES (NULL, 'merge_managed_tasks', ?, ?, 'pending', ?)",
                (str(dup["id"]), json.dumps(payload), now_iso()),
            )
            events.record(conn, "proposal",
                          f"Twin detected: merge {payload['duplicate_name']} into {survivor_name}",
                          details={"kind": "merge_managed_tasks"},
                          action_id=cur.lastrowid, task_id=dup["clickup_task_id"])
            drafted += 1

    if drafted:
        conn.commit()
        log.info("merge_proposer: drafted %d merge proposal(s)", drafted)
    return drafted


def apply_merge_approval(conn, payload: dict) -> dict:
    """Side-effect for an approved merge_managed_tasks action.

    Local-only:
      1. Append the duplicate's MR to the survivor's additional_mr_ids.
      2. Mark the duplicate managed_task as closed (hides it from Work).
      3. Auto-draft a `clickup_close_task` for the duplicate ClickUp task —
         user approves it separately so the external write keeps its own
         confirmation.
    Idempotent."""
    survivor_id = payload["survivor_managed_id"]
    dup_id = payload["duplicate_managed_id"]
    dup_clickup = payload.get("duplicate_clickup_task_id")
    dup_mr = payload.get("duplicate_mr_id")

    survivor_work_item_id = _work_item_for_clickup(
        conn, payload.get("survivor_clickup_task_id")
    )
    duplicate_work_item_id = _work_item_for_clickup(conn, dup_clickup)
    if (
        survivor_work_item_id is not None
        and duplicate_work_item_id is not None
        and survivor_work_item_id != duplicate_work_item_id
    ):
        from . import work_items
        work_items.merge_items(
            conn, survivor_work_item_id, [duplicate_work_item_id]
        )

    row = conn.execute(
        "SELECT additional_mr_ids, related_mr_id FROM managed_tasks WHERE id = ?",
        (survivor_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"survivor managed_task {survivor_id} not found")

    current = json.loads(row["additional_mr_ids"] or "[]")
    if dup_mr and dup_mr != row["related_mr_id"] and dup_mr not in current:
        current.append(dup_mr)
        conn.execute(
            "UPDATE managed_tasks SET additional_mr_ids = ? WHERE id = ?",
            (json.dumps(current), survivor_id),
        )

    conn.execute(
        "UPDATE managed_tasks SET status = 'closed' WHERE id = ?", (dup_id,),
    )

    if dup_clickup and not conn.execute(
        "SELECT 1 FROM pending_actions WHERE kind = 'clickup_close_task'"
        " AND target_id = ? AND status IN ('pending', 'executed')",
        (dup_clickup,),
    ).fetchone():
        close_payload = {
            "draft": (
                f"Close this task — it duplicates Watson task"
                f" {payload.get('survivor_clickup_task_id')}. Local merge has"
                " already attached its MR to the survivor."
            ),
            "reason": "Duplicate after merge approval",
            "duplicate_of": payload.get("survivor_clickup_task_id"),
            "auto_proposed": True,
        }
        conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " status, created_at) VALUES (NULL, 'clickup_close_task', ?, ?, 'pending', ?)",
            (dup_clickup, json.dumps(close_payload), now_iso()),
        )

    return {"survivor_managed_id": survivor_id, "duplicate_managed_id": dup_id,
            "linked_mrs": current}
