from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import entry_dict

router = APIRouter(prefix="/api", tags=["entries"])


def _fetch_followups(conn, parent_ids: list) -> dict:
    """Return {parent_id: [child entry_dict, ...]} for the given parents."""
    if not parent_ids:
        return {}
    marks = ",".join("?" * len(parent_ids))
    rows = conn.execute(
        f"SELECT e.*, c.raw_text AS capture_raw FROM entries e"
        f" LEFT JOIN captures c ON e.capture_id = c.id"
        f" WHERE e.parent_entry_id IN ({marks}) ORDER BY e.id",
        parent_ids,
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["parent_entry_id"], []).append(entry_dict(r))
    return grouped


@router.get("/entries")
def list_entries(
    q: Optional[str] = None,
    type: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 100,
    conn=Depends(get_db),
):
    filtered = bool(q or type or tag)
    sql = (
        "SELECT e.*, c.raw_text AS capture_raw FROM entries e"
        " LEFT JOIN captures c ON e.capture_id = c.id WHERE 1=1"
    )
    params = []
    # With no filters, show only top-level entries and nest their follow-ups.
    # When searching/filtering, search across ALL entries (incl. follow-ups) flat,
    # so a follow-up can be found by its own text.
    if not filtered:
        sql += " AND e.parent_entry_id IS NULL"
    if q:
        sql += " AND (e.title LIKE ? OR e.body LIKE ? OR c.raw_text LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    if type:
        sql += " AND e.type = ?"
        params.append(type)
    if tag:
        sql += " AND e.tags LIKE ?"
        params.append(f'%"{tag}"%')
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(min(limit, 500))
    rows = conn.execute(sql, params).fetchall()
    entries = [entry_dict(r) for r in rows]

    followups = _fetch_followups(conn, [e["id"] for e in entries])
    for e in entries:
        e["follow_ups"] = followups.get(e["id"], [])
    return {"entries": entries}


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: int, conn=Depends(get_db)):
    """Delete an entry, any follow-ups threaded under it, and their reminders.
    Captures are left intact (a capture may have produced other entries)."""
    if not conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone():
        raise HTTPException(404, "entry not found")

    # gather the entry plus all descendant follow-ups (threads can nest)
    to_delete = [entry_id]
    frontier = [entry_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        kids = [r["id"] for r in conn.execute(
            f"SELECT id FROM entries WHERE parent_entry_id IN ({marks})", frontier
        )]
        new = [k for k in kids if k not in to_delete]
        to_delete.extend(new)
        frontier = new

    marks = ",".join("?" * len(to_delete))
    conn.execute(f"DELETE FROM reminders WHERE entry_id IN ({marks})", to_delete)
    conn.execute(f"DELETE FROM entries WHERE id IN ({marks})", to_delete)
    return {"deleted": len(to_delete), "ids": to_delete}


@router.get("/entries/{entry_id}/thread")
def entry_thread(entry_id: int, conn=Depends(get_db)):
    """The full thread for an entry: the root plus all follow-ups in order."""
    row = conn.execute(
        "SELECT e.*, c.raw_text AS capture_raw FROM entries e"
        " LEFT JOIN captures c ON e.capture_id = c.id WHERE e.id = ?",
        (entry_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "entry not found")
    root = entry_dict(row)
    # if this entry is itself a follow-up, climb to the root
    if root["parent_entry_id"]:
        return entry_thread(root["parent_entry_id"], conn)
    root["follow_ups"] = _fetch_followups(conn, [entry_id]).get(entry_id, [])
    return {"thread": root}
