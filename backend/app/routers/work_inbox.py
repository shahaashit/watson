"""Endpoints for explicitly resolving or dismissing local capture Inbox rows."""
from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..schemas import WorkInboxResolve
from ..services import work_inbox


router = APIRouter(prefix="/api/work-inbox", tags=["work-inbox"])


def _map_error(error: ValueError) -> None:
    if str(error) in {"inbox item not found", "work item not found"}:
        raise HTTPException(404, str(error)) from error
    raise HTTPException(409, str(error)) from error


@router.get("")
def get_work_inbox(conn=Depends(get_db)):
    return {"inbox": work_inbox.list_inbox(conn)}


@router.post("/{inbox_id}/resolve")
def resolve_work_inbox(inbox_id: int, payload: WorkInboxResolve, conn=Depends(get_db)):
    try:
        inbox = work_inbox.resolve_inbox_item(conn, inbox_id, payload.work_item_id)
    except ValueError as error:
        _map_error(error)
    return {"inbox": inbox}


@router.post("/{inbox_id}/dismiss")
def dismiss_work_inbox(inbox_id: int, conn=Depends(get_db)):
    try:
        inbox = work_inbox.dismiss_inbox_item(conn, inbox_id)
    except ValueError as error:
        _map_error(error)
    return {"inbox": inbox}
