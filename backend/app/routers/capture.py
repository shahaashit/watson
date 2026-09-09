"""Capture endpoint — capture-first invariant lives here.

CORE PRINCIPLE: NEVER LOSE THE USER'S INPUT.
The raw text is INSERTed into captures BEFORE the LLM is ever called. If the
LLM fails (network, parse error, rate limit, anything), the capture row is
still there with classification_json=NULL and the UI shows a 'needs manual
review' badge. The user re-runs classification later by re-capturing or by
editing the prompt and waiting for the next attempt.

If you refactor: do NOT move classifier.classify_capture before the INSERT.
See CLAUDE.md (Core principles)."""
from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import action_dict, capture_dict, entry_dict, now_iso, reminder_dict
from ..schemas import CaptureIn
from ..services import classifier, work_inbox, work_items

router = APIRouter(prefix="/api", tags=["capture"])


@router.post("/capture")
def create_capture(payload: CaptureIn, conn=Depends(get_db)):
    text = payload.text.strip()
    cur = conn.execute(
        "INSERT INTO captures (raw_text, created_at) VALUES (?, ?)",
        (text, now_iso()),
    )
    capture_id = cur.lastrowid
    conn.commit()  # capture-first: persisted before the LLM is even called

    inbox = None
    if payload.work_item_id is not None:
        if conn.execute("SELECT 1 FROM work_items WHERE id=?", (payload.work_item_id,)).fetchone() is None:
            # The raw capture has already been committed, even when the chosen
            # local card no longer exists.
            raise HTTPException(404, "work item not found")
        try:
            conn.execute(
                "UPDATE captures SET work_item_id=? WHERE id=?", (payload.work_item_id, capture_id)
            )
            work_items.add_activity(
                conn,
                payload.work_item_id,
                activity_type="capture",
                body=text,
                capture_id=capture_id,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result = classifier.classify_capture(conn, capture_id, text, payload.work_item_id)
    if payload.work_item_id is None:
        # Classification is allowed to create entries/reminders first. Publish
        # the pending Inbox row only after that fan-out is complete so a user
        # cannot resolve the capture while its children are still unlinked.
        inbox = work_inbox.suggest_capture_link(conn, capture_id, text)
    row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    response = {"capture": capture_dict(row), "result": result}
    if inbox is not None:
        response["inbox"] = inbox
    return response


@router.get("/captures")
def recent_captures(limit: int = 10, conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT * FROM captures ORDER BY id DESC LIMIT ?", (min(limit, 50),)
    ).fetchall()
    captures = []
    for row in rows:
        c = capture_dict(row)
        c["entries"] = [
            entry_dict(r) for r in conn.execute(
                "SELECT * FROM entries WHERE capture_id = ? ORDER BY id", (row["id"],)
            )
        ]
        entry_ids = [e["id"] for e in c["entries"]]
        if entry_ids:
            marks = ",".join("?" * len(entry_ids))
            c["reminders"] = [
                reminder_dict(r) for r in conn.execute(
                    f"SELECT * FROM reminders WHERE entry_id IN ({marks}) ORDER BY due_at",
                    entry_ids,
                )
            ]
        else:
            c["reminders"] = []
        c["actions"] = [
            action_dict(r) for r in conn.execute(
                "SELECT * FROM pending_actions WHERE capture_id = ? ORDER BY id", (row["id"],)
            )
        ]
        captures.append(c)
    return {"captures": captures}
