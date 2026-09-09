from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import now_iso, reminder_dict
from ..schemas import SnoozeIn
from ..services.classifier import resolve_due_at

router = APIRouter(prefix="/api", tags=["reminders"])


def _get(conn, reminder_id: int):
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if not row:
        raise HTTPException(404, "reminder not found")
    return row


@router.get("/reminders")
def list_reminders(status: Optional[str] = None, conn=Depends(get_db)):
    if status:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = ? ORDER BY due_at", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM reminders ORDER BY due_at").fetchall()
    return {"reminders": [reminder_dict(r) for r in rows]}


@router.post("/reminders/{reminder_id}/done")
def mark_done(reminder_id: int, conn=Depends(get_db)):
    _get(conn, reminder_id)
    conn.execute(
        "UPDATE reminders SET status = 'done', snoozed_until = NULL WHERE id = ?",
        (reminder_id,),
    )
    return {"reminder": reminder_dict(_get(conn, reminder_id))}


@router.post("/reminders/{reminder_id}/snooze")
def snooze(reminder_id: int, payload: SnoozeIn, conn=Depends(get_db)):
    _get(conn, reminder_id)
    until = resolve_due_at(payload.until)
    if not until:
        raise HTTPException(422, f"cannot parse snooze time: {payload.until!r}")
    if until <= now_iso():
        raise HTTPException(422, "snooze time must be in the future")
    conn.execute(
        "UPDATE reminders SET status = 'snoozed', snoozed_until = ? WHERE id = ?",
        (until, reminder_id),
    )
    return {"reminder": reminder_dict(_get(conn, reminder_id))}
