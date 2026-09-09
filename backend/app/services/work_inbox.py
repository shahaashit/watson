"""Local Inbox routing for captures that were not explicitly assigned to work."""
from contextlib import contextmanager

from ..models import now_iso, work_inbox_dict
from . import matcher, work_items


@contextmanager
def _atomic(conn):
    """Commit a small mutation, or isolate it when called inside a transaction."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT work_inbox_mutation")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT work_inbox_mutation")
            conn.execute("RELEASE SAVEPOINT work_inbox_mutation")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT work_inbox_mutation")
        return

    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def suggest_capture_link(conn, capture_id: int, text: str) -> dict:
    """Create one pending Inbox row, with at most one high-confidence local suggestion."""
    capture = conn.execute("SELECT id FROM captures WHERE id=?", (capture_id,)).fetchone()
    if capture is None:
        raise ValueError("capture not found")

    rows = conn.execute(
        "SELECT id, title FROM work_items WHERE state != 'done' ORDER BY position, id"
    ).fetchall()
    candidates = [{"task_id": row["id"], "name": row["title"]} for row in rows]
    match, confidence = matcher.best_match(text, candidates)
    suggested_work_item_id = match["task_id"] if match and confidence >= 0.70 else None
    stored_confidence = confidence if suggested_work_item_id is not None else None

    with _atomic(conn):
        existing = conn.execute(
            "SELECT * FROM work_inbox WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if existing is not None:
            return work_inbox_dict(existing)
        cursor = conn.execute(
            "INSERT INTO work_inbox (capture_id, suggested_work_item_id, confidence, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (capture_id, suggested_work_item_id, stored_confidence, now_iso()),
        )
        row = conn.execute("SELECT * FROM work_inbox WHERE id=?", (cursor.lastrowid,)).fetchone()
        return work_inbox_dict(row)


def list_inbox(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT wi.*, c.raw_text AS capture_text, suggested.id AS suggested_item_id, "
        "suggested.title AS suggested_item_title "
        "FROM work_inbox wi "
        "JOIN captures c ON c.id=wi.capture_id "
        "LEFT JOIN work_items suggested ON suggested.id=wi.suggested_work_item_id "
        "WHERE wi.status='pending' ORDER BY wi.created_at DESC, wi.id DESC"
    ).fetchall()
    inbox = []
    for row in rows:
        item = work_inbox_dict(row)
        item["capture_text"] = row["capture_text"]
        item["suggested_work_item"] = (
            {"id": row["suggested_item_id"], "title": row["suggested_item_title"]}
            if row["suggested_item_id"] is not None
            else None
        )
        inbox.append(item)
    return inbox


def resolve_inbox_item(conn, inbox_id: int, work_item_id: int) -> dict:
    """Atomically attach a capture and every classified child to a work item."""
    with _atomic(conn):
        inbox = conn.execute("SELECT * FROM work_inbox WHERE id=?", (inbox_id,)).fetchone()
        if inbox is None:
            raise ValueError("inbox item not found")
        if inbox["status"] != "pending":
            raise ValueError("inbox item is already resolved")
        capture = conn.execute(
            "SELECT id, raw_text FROM captures WHERE id=?", (inbox["capture_id"],)
        ).fetchone()
        if capture is None:
            raise ValueError("capture not found")
        if conn.execute("SELECT 1 FROM work_items WHERE id=?", (work_item_id,)).fetchone() is None:
            raise ValueError("work item not found")

        conn.execute("UPDATE captures SET work_item_id=? WHERE id=?", (work_item_id, capture["id"]))
        conn.execute("UPDATE entries SET work_item_id=? WHERE capture_id=?", (work_item_id, capture["id"]))
        conn.execute(
            "UPDATE reminders SET work_item_id=? WHERE capture_id=? OR entry_id IN "
            "(SELECT id FROM entries WHERE capture_id=?)",
            (work_item_id, capture["id"], capture["id"]),
        )
        work_items.add_activity(
            conn,
            work_item_id,
            activity_type="capture",
            body=capture["raw_text"],
            capture_id=capture["id"],
        )
        resolved_at = now_iso()
        conn.execute(
            "UPDATE work_inbox SET status='linked', resolved_at=? WHERE id=?", (resolved_at, inbox_id)
        )
        row = conn.execute("SELECT * FROM work_inbox WHERE id=?", (inbox_id,)).fetchone()
        return work_inbox_dict(row)


def dismiss_inbox_item(conn, inbox_id: int) -> dict:
    with _atomic(conn):
        inbox = conn.execute("SELECT * FROM work_inbox WHERE id=?", (inbox_id,)).fetchone()
        if inbox is None:
            raise ValueError("inbox item not found")
        if inbox["status"] != "pending":
            raise ValueError("inbox item is already resolved")
        conn.execute(
            "UPDATE work_inbox SET status='dismissed', resolved_at=? WHERE id=?",
            (now_iso(), inbox_id),
        )
        row = conn.execute("SELECT * FROM work_inbox WHERE id=?", (inbox_id,)).fetchone()
        return work_inbox_dict(row)
