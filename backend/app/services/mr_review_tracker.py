"""Auto-propose Watson task drafts for MRs assigned to the user as reviewer
that they haven't yet captured. Runs during sync — NEVER auto-applies; each
proposal goes through the existing pending-action approve flow.

Multi-repo grouping: when several reviewer MRs belong to the same requirement
(common for cross-repo changes), Watson collapses them so the user gets ONE
task to approve, not N. Signals — in priority order:
  1. Branch tag — both MRs encode the same `_clickup<id>` in their source
     branch. High precision; the user's own convention.
  2. Fuzzy title — `fuzz.token_set_ratio` ≥ 70 against either an existing
     managed_task's related ClickUp name OR a pending create-task draft's
     name. Mirrors `link_proposer`'s threshold for consistency.

If an existing managed_task hosts the requirement → draft `link_mr_to_task`.
If a pending create-task draft hosts it → fold this MR into that draft's
`additional_mr_ids` (no second draft). Else → fresh create-task draft.
"""
import json
import logging

from rapidfuzz import fuzz

from ..models import now_iso
from . import events, gitlab_client

log = logging.getLogger("watson.mr_tracker")

# Same threshold as link_proposer — strict enough to reject project-token noise,
# loose enough to catch real reorderings and bracketed prefixes.
MATCH_THRESHOLD = 70


def _already_tracked(conn, mr_id: str) -> bool:
    """An MR is 'already tracked' if any managed_task references it (as
    related_mr_id OR in additional_mr_ids), any durable review group contains
    it, OR any pending_action — pending, approved, rejected, or executed —
    already references it. That way a user's reject sticks: we never
    re-propose what they dismissed."""
    from . import work_suppression
    if work_suppression.mr_removed(conn, mr_id):
        return True
    if conn.execute(
        "SELECT 1 FROM managed_tasks WHERE related_mr_id = ?", (mr_id,)
    ).fetchone():
        return True
    for row in conn.execute(
        "SELECT additional_mr_ids FROM managed_tasks WHERE additional_mr_ids IS NOT NULL"
    ):
        try:
            if mr_id in (json.loads(row["additional_mr_ids"]) or []):
                return True
        except Exception:
            pass
    if conn.execute(
        "SELECT 1 FROM review_group_mrs WHERE mr_id = ?", (mr_id,)
    ).fetchone():
        return True
    # coarse LIKE filter, verified by JSON parse — avoids substring false positives
    for row in conn.execute(
        "SELECT payload_json FROM pending_actions "
        "WHERE status <> 'inactive' AND payload_json LIKE ?",
        (f"%{mr_id}%",),
    ):
        try:
            p = json.loads(row["payload_json"])
        except Exception:
            continue
        if p.get("related_mr_id") == mr_id:
            return True
        if mr_id in (p.get("additional_mr_ids") or []):
            return True
    return False


def deactivate_unselected_proposals(conn) -> int:
    """Hide pending auto-proposals whose MRs are absent from a successful sync.

    Rows stay in the audit history. ``_already_tracked`` deliberately ignores
    this internal status so re-enabling a repository can propose the MR again.
    """
    cached_ids = {
        row["mr_id"] for row in conn.execute("SELECT mr_id FROM gitlab_mrs_cache")
    }
    deactivated = 0
    for row in conn.execute(
        "SELECT id, payload_json FROM pending_actions "
        "WHERE status='pending' ORDER BY id"
    ):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not payload.get("auto_proposed"):
            continue
        mr_ids = {
            mr_id
            for mr_id in [
                payload.get("related_mr_id"),
                *(payload.get("additional_mr_ids") or []),
            ]
            if isinstance(mr_id, str) and mr_id
        }
        if not mr_ids or mr_ids.issubset(cached_ids):
            continue
        conn.execute(
            "UPDATE pending_actions SET status='inactive', resolved_at=? "
            "WHERE id=? AND status='pending'",
            (now_iso(), row["id"]),
        )
        deactivated += 1
    if deactivated:
        conn.commit()
    return deactivated


def _find_host_managed_task(conn, mr_title: str, related_clickup):
    """Find an existing OPEN managed_task this MR belongs to.

    Branch tag wins (precise). Else fuzzy title match against the managed
    task's related ClickUp name, ≥ MATCH_THRESHOLD. Returns
    (managed_task_id, score, task_name) or None."""
    if related_clickup:
        row = conn.execute(
            "SELECT mt.id, ct.name AS related_name"
            " FROM managed_tasks mt"
            " LEFT JOIN clickup_tasks_cache ct ON mt.related_clickup_task_id = ct.task_id"
            " WHERE mt.status = 'open' AND mt.related_clickup_task_id = ?",
            (related_clickup,),
        ).fetchone()
        if row:
            return (row["id"], 100, row["related_name"] or related_clickup)

    title = (mr_title or "").strip()
    if not title:
        return None
    best = None
    for r in conn.execute(
        "SELECT mt.id, ct.name AS related_name FROM managed_tasks mt"
        " JOIN clickup_tasks_cache ct ON mt.related_clickup_task_id = ct.task_id"
        " WHERE mt.status = 'open' AND ct.name IS NOT NULL AND ct.name != ''"
    ).fetchall():
        score = fuzz.token_set_ratio(r["related_name"], title)
        if score >= MATCH_THRESHOLD and (best is None or score > best[1]):
            best = (r["id"], score, r["related_name"])
    return best


def _strip_review_prefix(name: str) -> str:
    """Pull the topic out of a draft name like
    'Review - <topic> (<author>)' so fuzzy match compares topic↔title."""
    s = (name or "").strip()
    if s.startswith("Review - "):
        s = s[len("Review - "):]
    if s.endswith(")") and " (" in s:
        s = s.rsplit(" (", 1)[0]
    return s


def _find_groupable_pending_create(conn, mr_title: str, related_clickup):
    """Find a pending clickup_create_task draft this MR can fold into.

    Same-sync grouping: branch tag match wins; else fuzzy title match
    against the draft's stripped name. Returns (action_id, payload, score)
    or None."""
    rows = conn.execute(
        "SELECT id, payload_json FROM pending_actions"
        " WHERE kind = 'clickup_create_task' AND status = 'pending'"
    ).fetchall()
    parsed = []
    for r in rows:
        try:
            parsed.append((r["id"], json.loads(r["payload_json"])))
        except Exception:
            continue

    if related_clickup:
        for action_id, p in parsed:
            if p.get("related_task_id") == related_clickup:
                return (action_id, p, 100)

    title = (mr_title or "").strip()
    if not title:
        return None
    best = None
    for action_id, p in parsed:
        draft = p.get("draft")
        if not isinstance(draft, dict):
            continue
        topic = _strip_review_prefix(draft.get("name", ""))
        if not topic:
            continue
        score = fuzz.token_set_ratio(topic, title)
        if score >= MATCH_THRESHOLD and (best is None or score > best[2]):
            best = (action_id, p, score)
    return best


def _draft_link_to_managed(conn, mr, mt_id, task_name, score):
    """Mirror link_proposer's payload shape so the existing UI just works."""
    payload = {
        "managed_task_id": mt_id,
        "related_mr_id": mr["mr_id"],
        "mr_title": (mr["title"] or mr["mr_id"]).strip(),
        "task_name": task_name or "",
        "match_score": score,
        "auto_proposed": True,
    }
    conn.execute(
        "INSERT INTO pending_actions (capture_id, kind, target_id,"
        " payload_json, match_confidence, status, created_at)"
        " VALUES (NULL, 'link_mr_to_task', ?, ?, ?, 'pending', ?)",
        (str(mt_id), json.dumps(payload), score / 100.0, now_iso()),
    )


def _fold_into_pending_draft(conn, action_id, payload, mr):
    """Append this MR to a pending create_task draft's additional_mr_ids and
    add a line to the description so the user sees all MRs being grouped."""
    additional = list(payload.get("additional_mr_ids") or [])
    if mr["mr_id"] == payload.get("related_mr_id") or mr["mr_id"] in additional:
        return
    additional.append(mr["mr_id"])
    payload["additional_mr_ids"] = additional

    draft = payload.get("draft")
    if isinstance(draft, dict):
        existing = (draft.get("description") or "").rstrip()
        title = (mr["title"] or mr["mr_id"]).strip()
        line = f"Also reviewing: {title}\nMR url: {mr['url']}"
        draft["description"] = f"{existing}\n\n{line}" if existing else line
        payload["draft"] = draft

    conn.execute(
        "UPDATE pending_actions SET payload_json = ? WHERE id = ?",
        (json.dumps(payload), action_id),
    )


def propose_review_tasks(conn) -> int:
    """For each open reviewer MR not already tracked, either link it to an
    existing managed_task, fold it into a pending draft, or draft a fresh
    `clickup_create_task`. Returns total proposals made/updated."""
    rows = conn.execute(
        "SELECT mr_id, project, title, url, author, source_branch, description"
        " FROM gitlab_mrs_cache WHERE role IN ('reviewer','engaged') AND state = 'opened'"
        " ORDER BY mr_id"  # deterministic so within-batch grouping is stable
    ).fetchall()
    drafted = grouped = linked = 0
    for r in rows:
        if _already_tracked(conn, r["mr_id"]):
            continue
        title = (r["title"] or r["mr_id"]).strip()
        author = (r["author"] or "unknown").strip()

        # if the MR's branch (or its description, when the branch tag is missing
        # or typoed) points at a known ClickUp task id, use that as the strongest
        # grouping signal AND link it on the resulting task
        related_clickup = gitlab_client.clickup_id_from_mr(r)
        if related_clickup and not conn.execute(
            "SELECT 1 FROM clickup_tasks_cache WHERE task_id = ?", (related_clickup,),
        ).fetchone():
            related_clickup = None

        # 1. existing managed_task hosts this requirement → propose link
        host = _find_host_managed_task(conn, title, related_clickup)
        if host:
            mt_id, score, task_name = host
            _draft_link_to_managed(conn, r, mt_id, task_name, score)
            events.record(conn, "proposal",
                          f"Drafted link: {title} → {task_name}",
                          details={"kind": "link_mr_to_task", "mr_url": r["url"], "score": score},
                          mr_id=r["mr_id"])
            linked += 1
            continue

        # 2. a pending create-task draft already covers this requirement → fold in
        pending = _find_groupable_pending_create(conn, title, related_clickup)
        if pending:
            action_id, payload, _score = pending
            _fold_into_pending_draft(conn, action_id, payload, r)
            events.record(conn, "proposal",
                          f"Grouped MR into existing draft: {title}",
                          details={"kind": "clickup_create_task_fold", "mr_url": r["url"]},
                          action_id=action_id, mr_id=r["mr_id"])
            grouped += 1
            continue

        # 3. fresh create-task draft
        draft = {
            "name": f"Review - {title} ({author})",
            "description": (
                f"Reviewing {author}'s MR.\n"
                f"MR: {title}\n"
                f"MR url: {r['url']}"
            ),
            "labels": ["Review"],
        }
        payload = {
            "draft": draft,
            "task_match_query": title,
            "related_task_id": related_clickup,
            "related_mr_id": r["mr_id"],
            "additional_mr_ids": [],
            "auto_proposed": True,
        }
        cur = conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id,"
            " payload_json, match_confidence, status, created_at)"
            " VALUES (NULL, 'clickup_create_task', NULL, ?, NULL, 'pending', ?)",
            (json.dumps(payload), now_iso()),
        )
        events.record(conn, "proposal",
                      f"Drafted review task: {draft['name']}",
                      details={"kind": "clickup_create_task", "mr_url": r["url"]},
                      action_id=cur.lastrowid, mr_id=r["mr_id"])
        drafted += 1

    total = drafted + grouped + linked
    if total:
        conn.commit()
        log.info(
            "review proposals: %d new draft(s), %d grouped into existing draft(s),"
            " %d linked to existing managed task(s)",
            drafted, grouped, linked,
        )
    return total
