"""Fuzzy MR↔managed-task linker. For each cached open MR not already linked
to any work card, find the best-matching managed task (by title similarity to
its related ClickUp task name) and propose a link. Approve-before-write —
nothing happens until the user accepts in the UI."""
import json
import logging

from rapidfuzz import fuzz

from ..models import now_iso
from . import events, gitlab_client, work_suppression

log = logging.getLogger("watson.link_proposer")

# fuzz.token_set_ratio is tolerant of word reorderings and bracketed prefixes
# (so "[Video module] Fix X with Y" still scores well against "Fix Y in Video module X").
# 70 is the sweet spot in testing — strict enough to suppress noise from
# common project tokens, loose enough to catch real reorderings.
MATCH_THRESHOLD = 70


def _has_any_pending_for_mr(conn, mr_id) -> bool:
    """True if any pending_action (any status) already references this MR.
    Same dedup pattern as mr_review_tracker — rejection sticks across syncs."""
    return conn.execute(
        "SELECT 1 FROM pending_actions "
        "WHERE status <> 'inactive' AND payload_json LIKE ?",
        (f'%"related_mr_id": "{mr_id}"%',),
    ).fetchone() is not None


def _already_linked(conn, managed_task_id, mr_id) -> bool:
    """True if this MR is already on this task — via direct related_mr_id,
    additional_mr_ids JSON array, OR via the _clickup<id> branch convention."""
    mt = conn.execute(
        "SELECT related_mr_id, additional_mr_ids, related_clickup_task_id"
        " FROM managed_tasks WHERE id = ?", (managed_task_id,)
    ).fetchone()
    if not mt:
        return True  # safe default: don't propose for missing task
    if mt["related_mr_id"] == mr_id:
        return True
    additional = json.loads(mt["additional_mr_ids"] or "[]") \
        if "additional_mr_ids" in mt.keys() and mt["additional_mr_ids"] else []
    if mr_id in additional:
        return True
    if mt["related_clickup_task_id"]:
        branch_row = conn.execute(
            "SELECT source_branch FROM gitlab_mrs_cache WHERE mr_id = ?", (mr_id,)
        ).fetchone()
        if branch_row:
            bid = gitlab_client.clickup_id_from_branch(branch_row["source_branch"])
            if bid and bid == mt["related_clickup_task_id"]:
                return True
    return False


def propose_mr_task_links(conn) -> int:
    """For each open MR not yet referenced by any pending_action, find its
    best-matching managed task by title similarity to that task's related
    ClickUp task name. Draft one link proposal per MR (against the best
    match only). Returns count drafted."""
    managed = conn.execute(
        "SELECT mt.id, mt.related_clickup_task_id,"
        "  ct.name AS related_name"
        " FROM managed_tasks mt"
        " JOIN clickup_tasks_cache ct ON mt.related_clickup_task_id = ct.task_id"
        " WHERE mt.status = 'open' AND ct.name IS NOT NULL AND ct.name != ''"
    ).fetchall()
    if not managed:
        return 0

    mrs = conn.execute(
        "SELECT mr_id, title, url FROM gitlab_mrs_cache WHERE state = 'opened'"
    ).fetchall()

    proposed = 0
    for mr in mrs:
        if work_suppression.mr_removed(conn, mr['mr_id']):
            continue
        title = (mr["title"] or "").strip()
        if not title:
            continue
        if _has_any_pending_for_mr(conn, mr["mr_id"]):
            continue  # reviewer-tracker or a prior link proposal already covered this

        # find best matching managed_task above threshold
        best_mt, best_score = None, 0
        for mt in managed:
            score = fuzz.token_set_ratio(mt["related_name"], title)
            if score >= MATCH_THRESHOLD and score > best_score:
                best_mt, best_score = mt, score
        if not best_mt:
            continue
        if _already_linked(conn, best_mt["id"], mr["mr_id"]):
            continue

        payload = {
            "managed_task_id": best_mt["id"],
            "related_mr_id": mr["mr_id"],  # keyed so other proposers' dedup sees it
            "mr_title": title,
            "task_name": best_mt["related_name"],
            "match_score": best_score,
        }
        cur = conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id,"
            " payload_json, match_confidence, status, created_at)"
            " VALUES (NULL, 'link_mr_to_task', ?, ?, ?, 'pending', ?)",
            (str(best_mt["id"]), json.dumps(payload), best_score / 100.0, now_iso()),
        )
        events.record(conn, "proposal",
                      f"Fuzzy link proposed: {title} → {best_mt['related_name']}",
                      details={"kind": "link_mr_to_task", "score": best_score, "mr_url": mr["url"]},
                      action_id=cur.lastrowid, mr_id=mr["mr_id"])
        proposed += 1

    if proposed:
        conn.commit()
        log.info("proposed %d MR↔task link(s)", proposed)
    return proposed


def apply_link_approval(conn, payload: dict) -> dict:
    """Side-effect for an approved link_mr_to_task action: append the MR id to
    the managed task's additional_mr_ids. Idempotent."""
    mt_id = payload["managed_task_id"]
    mr_id = payload["related_mr_id"]
    work_suppression.require_sources(conn, [mr_id])
    row = conn.execute(
        "SELECT additional_mr_ids FROM managed_tasks WHERE id = ?", (mt_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"managed_task {mt_id} not found")
    current = json.loads(row["additional_mr_ids"] or "[]")
    if mr_id not in current:
        current.append(mr_id)
        conn.execute(
            "UPDATE managed_tasks SET additional_mr_ids = ? WHERE id = ?",
            (json.dumps(current), mt_id),
        )
    return {"managed_task_id": mt_id, "linked_mrs": current}
