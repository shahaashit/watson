"""Pending-action endpoints — the gatekeeper for ALL external writes.

CORE PRINCIPLE: APPROVE-BEFORE-WRITE.
Watson never auto-applies external side-effects (ClickUp comments, status
changes, task creation, task close, MR-task links). Every signal from the
classifier / completion checker / mr_review_tracker / link_proposer drops
into `pending_actions` as a draft. Only an authenticated user click on
`POST /api/actions/{id}/approve` actually executes the side-effect.

If you're adding a new "Watson noticed something" feature, the right shape is:
  1. Detection during sync → INSERT a row into pending_actions
  2. UI surfaces it in `Watson suggests` / `Waiting on you` / per-card
  3. User approves → `_execute_one` dispatches to clickup_client/local handlers
Do NOT skip step 2-3 even if "it's obviously safe" — the user explicitly
designed Watson around this invariant. See CLAUDE.md (Core principles)."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import action_dict, now_iso
from ..schemas import ActionPatch, BulkActionIn
from ..services import clickup_client, completion, events, external_errors

log = logging.getLogger("watson.actions")

router = APIRouter(prefix="/api", tags=["actions"])


def _get(conn, action_id: int):
    row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        raise HTTPException(404, "action not found")
    return row


def _action_subject(row, verb: str) -> str:
    """Short human line for the Log audit trail: 'Approved: Review - foo (Discussion)'."""
    try:
        payload = json.loads(row["payload_json"])
    except Exception:
        payload = {}
    draft = payload.get("draft")
    label = None
    if isinstance(draft, dict):
        label = draft.get("name")
    if not label:
        label = payload.get("task_name") or payload.get("mr_title") or row["kind"]
    return f"{verb}: {label}"


def _category_from_payload(payload: dict):
    draft = payload.get("draft")
    if isinstance(draft, dict):
        labels = draft.get("labels") or []
        if labels:
            return labels[0]
        name = draft.get("name", "")
        if " - " in name:
            return name.split(" - ", 1)[0]
    return None


def _record_review_resolution(conn, row, payload: dict, decision: str) -> None:
    title = str(payload.get("suggested_title") or "review MRs").strip()
    subject = (
        f"Merged review grouping: {title}"
        if decision == "merge"
        else f"Kept review MRs separate: {title}"
    )
    events.record(
        conn,
        "review_resolution",
        subject,
        details={
            "decision": decision,
            "mr_count": len(payload.get("member_mr_ids") or []),
        },
        action_id=row["id"],
    )


def _execute_one(conn, row) -> Optional[dict[str, str]]:
    """Execute an approved action + its side-effects. Returns None on success,
    or an error string on failure (and marks the action failed)."""
    payload = json.loads(row["payload_json"])
    try:
        if row["kind"] == "group_review_mrs":
            from ..services import review_groups
            result = review_groups.apply_grouping_action(conn, payload)
            _record_review_resolution(conn, row, payload, "merge")
        elif row["kind"] == "gcal_create_event":
            from ..services import gcal_client
            result = gcal_client.execute_action(row["kind"], row["target_id"], payload)
        else:
            result = clickup_client.execute_action(row["kind"], row["target_id"], payload)
    except Exception as exc:
        source = (
            "local"
            if row["kind"] == "group_review_mrs"
            else "google-calendar"
            if row["kind"] == "gcal_create_event"
            else "clickup"
        )
        failure = external_errors.details(source, exc)
        external_errors.log_failure(log, "approved action execution", source, exc)
        conn.execute(
            "UPDATE pending_actions SET status = 'failed', error = ?, resolved_at = ?"
            " WHERE id = ?",
            (failure["error"], now_iso(), row["id"]),
        )
        conn.commit()
        return failure

    # side-effects that let Watson keep these tasks in sync later
    if row["kind"] == "clickup_create_task" and isinstance(result, dict) and result.get("id"):
        completion.record_managed_task(
            conn, result["id"], row["capture_id"],
            payload.get("related_task_id"), _category_from_payload(payload),
            payload.get("related_mr_id"),
            payload.get("additional_mr_ids"),
        )
        # cache the just-created task + its related task so the Work view shows
        # them immediately (otherwise we'd wait up to 30 min for the next sync)
        try:
            clickup_client.refresh_tasks(conn, [
                result["id"], payload.get("related_task_id"),
            ])
        except Exception as exc:
            external_errors.log_failure(
                log, "refresh_tasks after approve", "clickup", exc
            )
    elif row["kind"] == "clickup_close_task" and row["target_id"]:
        completion.mark_managed_closed(conn, row["target_id"])
    elif row["kind"] == "link_mr_to_task":
        # local-only: append MR to the managed task's additional_mr_ids
        from ..services import link_proposer
        try:
            link_proposer.apply_link_approval(conn, payload)
        except Exception as exc:
            external_errors.log_failure(
                log, "apply link approval", "external-service", exc
            )
    elif row["kind"] == "merge_managed_tasks":
        # local-only: collapse a twin into its survivor + draft a ClickUp close
        from ..services import merge_proposer
        try:
            merge_proposer.apply_merge_approval(conn, payload)
        except Exception as exc:
            external_errors.log_failure(
                log, "apply merge approval", "external-service", exc
            )
    elif row["kind"] == "close_orphan_card":
        # local-only: ClickUp task is gone, just flip the managed_task closed
        from ..services import orphan_detector
        try:
            orphan_detector.apply_orphan_close(conn, payload)
        except Exception as exc:
            external_errors.log_failure(
                log, "apply orphan close", "external-service", exc
            )
    elif row["kind"] == "gcal_create_event" and isinstance(result, dict):
        # Stash the created event's id + link in the payload so the log entry
        # can point at the actual calendar event afterwards, then refresh the
        # local gcal cache so the new meeting shows on the Today timeline
        # immediately (otherwise we'd wait up to 30 min for the next sync).
        payload.setdefault("created", {})["event_id"] = result.get("id")
        payload["created"]["html_link"] = result.get("htmlLink")
        payload["created"]["hangout_link"] = result.get("hangoutLink")
        conn.execute(
            "UPDATE pending_actions SET payload_json = ? WHERE id = ?",
            (json.dumps(payload), row["id"]),
        )
        try:
            from ..services import gcal_client
            gcal_client.sync(conn)
        except Exception as exc:
            external_errors.log_failure(
                log, "gcal sync after event create", "google-calendar", exc
            )

    conn.execute(
        "UPDATE pending_actions SET status = 'executed', resolved_at = ?, error = NULL"
        " WHERE id = ?",
        (now_iso(), row["id"]),
    )
    events.record(conn, "approval", _action_subject(row, "Approved"),
                  details={"kind": row["kind"]}, action_id=row["id"],
                  task_id=row["target_id"])
    conn.commit()
    return None


@router.get("/actions")
def list_actions(status: Optional[str] = "pending", conn=Depends(get_db)):
    if status:
        rows = conn.execute(
            "SELECT * FROM pending_actions WHERE status = ? ORDER BY id DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM pending_actions ORDER BY id DESC").fetchall()
    return {"actions": [action_dict(r) for r in rows]}


@router.post("/actions/{action_id}/approve")
def approve(action_id: int, conn=Depends(get_db)):
    row = _get(conn, action_id)
    if row["status"] != "pending":
        raise HTTPException(409, f"action is already {row['status']}")
    error = _execute_one(conn, row)
    if error:
        raise HTTPException(502, error)
    return {"action": action_dict(_get(conn, action_id))}


@router.post("/actions/{action_id}/reject")
def reject(action_id: int, conn=Depends(get_db)):
    row = _get(conn, action_id)
    if row["status"] != "pending":
        raise HTTPException(409, f"action is already {row['status']}")
    if row["kind"] == "group_review_mrs":
        from ..services import review_groups
        payload = json.loads(row["payload_json"])
        review_groups.record_separation(conn, payload["fingerprint"])
        _record_review_resolution(conn, row, payload, "keep_separate")
    conn.execute(
        "UPDATE pending_actions SET status = 'rejected', resolved_at = ? WHERE id = ?",
        (now_iso(), action_id),
    )
    events.record(conn, "rejection", _action_subject(row, "Rejected"),
                  details={"kind": row["kind"]}, action_id=action_id,
                  task_id=row["target_id"])
    return {"action": action_dict(_get(conn, action_id))}


@router.post("/actions/bulk")
def bulk(payload: BulkActionIn, conn=Depends(get_db)):
    """Approve or reject many pending actions at once. Per-item failures don't
    abort the batch."""
    executed, rejected, failed = [], [], []
    for action_id in payload.ids:
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE id = ? AND status = 'pending'", (action_id,)
        ).fetchone()
        if not row:
            continue  # gone or already resolved — skip silently
        if payload.op == "approve":
            error = _execute_one(conn, row)
            if error:
                failed.append(action_id)
            else:
                executed.append(action_id)
        else:  # reject
            if row["kind"] == "group_review_mrs":
                from ..services import review_groups
                action_payload = json.loads(row["payload_json"])
                review_groups.record_separation(conn, action_payload["fingerprint"])
                _record_review_resolution(
                    conn, row, action_payload, "keep_separate"
                )
            conn.execute(
                "UPDATE pending_actions SET status = 'rejected', resolved_at = ? WHERE id = ?",
                (now_iso(), action_id),
            )
            events.record(conn, "rejection", _action_subject(row, "Rejected"),
                          details={"kind": row["kind"]}, action_id=action_id,
                          task_id=row["target_id"])
            rejected.append(action_id)
    conn.commit()
    return {"executed": executed, "rejected": rejected, "failed": failed}


@router.patch("/actions/{action_id}")
def edit(action_id: int, patch: ActionPatch, conn=Depends(get_db)):
    row = _get(conn, action_id)
    if row["status"] != "pending":
        raise HTTPException(409, f"only pending actions can be edited (is {row['status']})")
    if patch.payload is not None:
        payload = json.loads(row["payload_json"])
        payload.update(patch.payload)
        conn.execute(
            "UPDATE pending_actions SET payload_json = ? WHERE id = ?",
            (json.dumps(payload), action_id),
        )
    if patch.target_id is not None:
        conn.execute(
            "UPDATE pending_actions SET target_id = ?, match_confidence = 1.0 WHERE id = ?",
            (patch.target_id or None, action_id),
        )
    return {"action": action_dict(_get(conn, action_id))}
