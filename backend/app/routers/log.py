"""Unified activity log — merges classified entries (captured notes) with
system_events (sync ticks, drafts, approvals, rejections) into one timeline.

Rows always carry a `source` discriminator so the UI can style them:
  source='entry' → your captured note (with follow-ups nested)
  source='event' → something Watson did (sync, propose, approve, reject)

The two datasources are fetched independently, transformed to a common shape,
merged in Python, and sorted by created_at descending. This is fine at Watson's
single-user scale — a few hundred rows per source per week — and keeps the SQL
readable. If the volume ever explodes, this is where to add a proper UNION.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends

from ..db import get_db
from ..models import entry_dict

router = APIRouter(prefix="/api", tags=["log"])


def _fetch_entry_rows(conn, q, entry_type, tag, limit):
    """Top-level entries when unfiltered, ALL matching entries when filtered
    (so a follow-up can be found by its text). Mirrors /api/entries."""
    filtered = bool(q or entry_type or tag)
    sql = (
        "SELECT e.*, c.raw_text AS capture_raw FROM entries e"
        " LEFT JOIN captures c ON e.capture_id = c.id WHERE 1=1"
    )
    params = []
    if not filtered:
        sql += " AND e.parent_entry_id IS NULL"
    if q:
        sql += " AND (e.title LIKE ? OR e.body LIKE ? OR c.raw_text LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    if entry_type:
        sql += " AND e.type = ?"
        params.append(entry_type)
    if tag:
        sql += " AND e.tags LIKE ?"
        params.append(f'%"{tag}"%')
    sql += " ORDER BY e.created_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _fetch_followups(conn, parent_ids):
    if not parent_ids:
        return {}
    marks = ",".join(["?"] * len(parent_ids))
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


def _fetch_event_rows(conn, q, event_kind, limit):
    # State transitions are retained as legacy audit data, but local state is no
    # longer a user-facing Watson concept and should not leak into the timeline.
    sql = "SELECT * FROM system_events WHERE kind != 'state_change'"
    params = []
    if q:
        sql += " AND subject LIKE ?"
        params.append(f"%{q}%")
    if event_kind:
        sql += " AND kind = ?"
        params.append(event_kind)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _fetch_work_activity_rows(conn, q, kind, limit):
    """Activity joined to its item in one parameterized query (no N+1 detail lookups)."""
    if kind and kind != "work_activity":
        return []
    sql = (
        "SELECT wa.id, wa.work_item_id, wa.activity_type, wa.body, wa.created_at, "
        "wi.title, wi.description FROM work_activity wa "
        "JOIN work_items wi ON wi.id=wa.work_item_id WHERE 1=1"
    )
    params = []
    if q:
        like = f"%{q}%"
        sql += " AND (wi.title LIKE ? OR wi.description LIKE ? OR wa.body LIKE ?)"
        params += [like, like, like]
    sql += " ORDER BY wa.created_at DESC, wa.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _fetch_completed_work_rows(conn, q, kind, limit):
    if kind and kind != "work_completed":
        return []
    sql = (
        "SELECT id, title, description, completed_at, updated_at, created_at, "
        "COALESCE(completed_at, updated_at, created_at) AS timeline_at "
        "FROM work_items WHERE state='done'"
    )
    params = []
    if q:
        like = f"%{q}%"
        sql += " AND (title LIKE ? OR description LIKE ?)"
        params += [like, like]
    sql += " ORDER BY timeline_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _event_dict(row) -> dict:
    return {
        "id": f"s-{row['id']}",
        "source": "event",
        "kind": row["kind"],
        "title": row["subject"],
        "body": "",
        "details": json.loads(row["details_json"]) if row["details_json"] else None,
        "related_action_id": row["related_action_id"],
        "related_mr_id": row["related_mr_id"],
        "related_task_id": row["related_task_id"],
        "created_at": row["created_at"],
    }


def _entry_row_to_log(row, follow_ups):
    d = entry_dict(row)
    d["id"] = f"e-{row['id']}"           # unified id space
    d["entry_id"] = row["id"]            # raw id for delete/thread endpoints
    d["source"] = "entry"
    d["kind"] = row["type"]              # unify field name with events
    d["follow_ups"] = follow_ups.get(row["id"], [])
    return d


def _work_activity_row_to_log(row) -> dict:
    return {
        "id": f"wa-{row['id']}",
        "source": "work",
        "kind": "work_activity",
        "work_activity_id": row["id"],
        "work_item_id": row["work_item_id"],
        "url": f"/work/{row['work_item_id']}",
        "title": row["title"],
        "body": row["body"] or "",
        "activity_type": row["activity_type"],
        "created_at": row["created_at"],
    }


def _completed_work_row_to_log(row) -> dict:
    return {
        "id": f"wc-{row['id']}",
        "source": "work",
        "kind": "work_completed",
        "work_item_id": row["id"],
        "url": f"/work/{row['id']}",
        "title": row["title"],
        "body": row["description"] or "",
        "activity_type": "completed",
        "completed_at": row["completed_at"] or row["timeline_at"],
        "created_at": row["timeline_at"],
    }


@router.get("/log")
def unified_log(
    q: Optional[str] = None,
    kind: Optional[str] = None,       # entry type OR event kind — we filter both
    tag: Optional[str] = None,        # entries-only tag filter
    source: Optional[str] = None,     # 'entry' | 'event' | 'work' | None
    limit: int = 100,
    conn=Depends(get_db),
):
    limit = min(max(limit, 1), 500)
    include_entries = source in (None, "entry")
    include_events = source in (None, "event")
    include_work = source in (None, "work")

    entry_rows = _fetch_entry_rows(conn, q, kind if include_entries else "__none__",
                                   tag, limit) if include_entries else []
    # if `kind` filter was set but is meant for events, entry_rows will be
    # empty naturally (no entry has that type). Same the other way around.
    followups = _fetch_followups(conn, [r["id"] for r in entry_rows])
    entries = [_entry_row_to_log(r, followups) for r in entry_rows]
    if kind and include_entries:
        entries = [e for e in entries if e["kind"] == kind]

    events_ = [_event_dict(r) for r in _fetch_event_rows(
        conn, q, kind if include_events else "__none__", limit)] if include_events else []
    if kind and include_events:
        events_ = [e for e in events_ if e["kind"] == kind]

    work_activity = [_work_activity_row_to_log(row) for row in _fetch_work_activity_rows(
        conn, q, kind, limit
    )] if include_work else []
    completed_work = [_completed_work_row_to_log(row) for row in _fetch_completed_work_rows(
        conn, q, kind, limit
    )] if include_work else []

    items = entries + events_ + work_activity + completed_work
    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    items = items[:limit]

    # kinds seen (used for the client's filter chips)
    kinds = sorted({i["kind"] for i in items if i.get("kind")})
    return {"items": items, "kinds": kinds}
