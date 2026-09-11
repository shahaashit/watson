"""Scratch notes for the work boards.

Notes never reach the classifier or any connector: they are local jottings the
user keeps beside My Work and Team.
"""
from ..models import now_iso


def _note_dict(row) -> dict:
    return {
        "id": row["id"],
        "body": row["body"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _note_or_error(conn, note_id: int):
    row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if row is None:
        raise ValueError("note not found")
    return row


def list_notes(conn) -> list[dict]:
    # Newest first, and stable: editing a note must not make it jump position.
    rows = conn.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
    return [_note_dict(row) for row in rows]


def create_note(conn, body: str) -> dict:
    timestamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO notes (body, created_at, updated_at) VALUES (?, ?, ?)",
        (body, timestamp, timestamp),
    )
    return _note_dict(_note_or_error(conn, cursor.lastrowid))


def update_note(conn, note_id: int, body: str) -> dict:
    _note_or_error(conn, note_id)
    conn.execute(
        "UPDATE notes SET body=?, updated_at=? WHERE id=?", (body, now_iso(), note_id)
    )
    return _note_dict(_note_or_error(conn, note_id))


def delete_note(conn, note_id: int) -> None:
    _note_or_error(conn, note_id)
    conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
