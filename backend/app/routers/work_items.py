"""Local work-item routes.

These endpoints only mutate Watson's SQLite work model.  They intentionally do
not import connector clients: moving, annotating, or linking a card never
causes a ClickUp or GitLab write.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..db import get_db
from ..schemas import (
    WorkActivityCreate,
    WorkItemCreate,
    WorkItemMove,
    WorkItemPatch,
    WorkLinkCreate,
)
from ..services import settings_service, work_items


router = APIRouter(prefix="/api/work-items", tags=["work-items"])


def _item_or_404(conn, work_item_id: int) -> dict:
    item = work_items.get_work_detail(conn, work_item_id)
    if item is None:
        raise HTTPException(404, "work item not found")
    return item


def _conflict(error: ValueError) -> None:
    raise HTTPException(409, str(error)) from error


@router.post("", status_code=201)
def create_work_item(payload: WorkItemCreate, conn=Depends(get_db)):
    fields_set = payload.model_fields_set
    owner_person_id = payload.owner_person_id
    # New cards belong to the local user unless the caller explicitly chose an
    # owner (including null) or supplied a fallback display-only owner.
    if "owner_person_id" not in fields_set and "owner_display" not in fields_set:
        owner_person_id = work_items.ensure_self_person(conn, settings.watson_user_name)
    try:
        item = work_items.create_work_item(
            conn,
            title=payload.title,
            description=payload.description,
            state=payload.state,
            owner_person_id=owner_person_id,
            owner_display=payload.owner_display,
        )
    except ValueError as error:
        _conflict(error)
    return {"work_item": item}


@router.get("/my")
def my_work(conn=Depends(get_db)):
    self_person_id = work_items.ensure_self_person(conn, settings.watson_user_name)
    profile = settings_service.profile(conn)
    return work_items.my_work(
        conn,
        self_person_id,
        separate_by_status=profile["separate_work_by_status"],
        timezone_name=profile["timezone"],
    )


@router.get("/team")
def team_work(me_mode: bool = False, conn=Depends(get_db)):
    self_person_id = work_items.ensure_self_person(conn, settings.watson_user_name)
    profile = settings_service.profile(conn)
    return work_items.team_work(
        conn,
        separate_by_status=profile["separate_work_by_status"],
        timezone_name=profile["timezone"],
        me_mode=me_mode,
        self_person_id=self_person_id,
    )


@router.get("/{work_item_id}")
def get_work_item(work_item_id: int, conn=Depends(get_db)):
    return {"work_item": _item_or_404(conn, work_item_id)}


@router.patch("/{work_item_id}")
def patch_work_item(work_item_id: int, payload: WorkItemPatch, conn=Depends(get_db)):
    _item_or_404(conn, work_item_id)
    changes = {"title": payload.title, "description": payload.description}
    if "owner_person_id" in payload.model_fields_set:
        changes["owner_person_id"] = payload.owner_person_id
    if "owner_display" in payload.model_fields_set:
        changes["owner_display"] = payload.owner_display
    try:
        item = work_items.update_work_item(conn, work_item_id, **changes)
    except ValueError as error:
        _conflict(error)
    return {"work_item": item}


@router.patch("/{work_item_id}/position")
def move_work_item(work_item_id: int, payload: WorkItemMove, conn=Depends(get_db)):
    # The target itself is a missing resource (404); placement failures are
    # ordering conflicts (409), including a missing before/after reference.
    _item_or_404(conn, work_item_id)
    try:
        item = work_items.move_work_item(
            conn,
            work_item_id,
            state=payload.state,
            before_id=payload.before_id,
            after_id=payload.after_id,
        )
    except ValueError as error:
        _conflict(error)
    return {"work_item": item}


@router.post("/{work_item_id}/activity", status_code=201)
def create_activity(work_item_id: int, payload: WorkActivityCreate, conn=Depends(get_db)):
    _item_or_404(conn, work_item_id)
    try:
        activity = work_items.add_activity(
            conn, work_item_id, activity_type=payload.activity_type, body=payload.body
        )
    except ValueError as error:
        _conflict(error)
    return {"activity": activity}


@router.post("/{work_item_id}/links", status_code=201)
def create_link(work_item_id: int, payload: WorkLinkCreate, conn=Depends(get_db)):
    _item_or_404(conn, work_item_id)
    try:
        link = work_items.add_work_link(
            conn,
            work_item_id,
            source_type=payload.source_type,
            external_id=payload.external_id,
            url=payload.url,
            label=payload.label,
        )
    except ValueError as error:
        _conflict(error)
    return {"link": link}
