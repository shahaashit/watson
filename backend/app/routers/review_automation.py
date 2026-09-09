"""Review-automation decisions surfaced inside My Work."""

import json

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import action_dict
from ..services import review_automation, review_groups


router = APIRouter(prefix="/api/review-automation", tags=["review-automation"])


def _is_review_suggestion(row) -> bool:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    if row["kind"] == "group_review_mrs":
        return row["status"] == "pending"
    if row["kind"] == "clickup_create_task":
        return row["status"] == "failed" and payload.get("auto_proposed") is True
    return (
        row["kind"] == "clickup_close_task"
        and row["status"] == "pending"
        and payload.get("reason") == "Duplicate after merge approval"
    )


def _group_suggestion(group: dict) -> dict:
    state = group["creation_state"]
    return {
        "id": f"review-group-{group['id']}",
        "group_id": group["id"],
        "kind": "review_group_reconciliation",
        "status": state,
        "payload": {
            "suggested_title": group["title"],
            "member_mr_ids": group["mr_ids"],
            "creation_state": state,
        },
        "error": (
            "ClickUp review task creation failed"
            if state == "failed"
            else "ClickUp review task creation was deferred"
            if state == "deferred"
            else "ClickUp review task creation failed"
        ),
        "retryable": state in ("failed", "deferred"),
    }


@router.get("/suggestions")
def suggestions(conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT * FROM pending_actions "
        "WHERE status IN ('pending','failed') ORDER BY id DESC"
    ).fetchall()
    items = []
    referenced_group_ids = set()
    groups = {group["id"]: group for group in review_groups.open_groups(conn)}
    for row in rows:
        if not _is_review_suggestion(row):
            continue
        item = action_dict(row)
        group_id = item["payload"].get("review_group_id")
        if isinstance(group_id, int):
            group = groups.get(group_id)
            if group is not None and group["creation_state"] not in (
                "failed",
                "deferred",
            ):
                continue
            item["group_id"] = group_id
            if group is not None:
                item["status"] = group["creation_state"]
            item["retryable"] = item["status"] in ("failed", "deferred")
            referenced_group_ids.add(group_id)
        items.append(item)

    for group in groups.values():
        if (
            group["creation_state"] in ("failed", "deferred", "uncertain")
            and group["id"] not in referenced_group_ids
        ):
            items.append(_group_suggestion(group))
    return {"suggestions": items}


@router.post("/groups/{group_id}/retry")
def retry_group(group_id: int, conn=Depends(get_db)):
    try:
        group = review_automation.retry_group_creation(conn, group_id)
    except ValueError as exc:
        if str(exc) == "review group not found":
            raise HTTPException(404, "review group not found") from None
        raise HTTPException(409, "Only failed or deferred review groups can be retried.") from None
    return {"group": group}
