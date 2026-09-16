"""Global search powering the ⌘K command bar.

Every result is local/cache-backed. Work URLs intentionally point at Watson's
own stable workspace route instead of an external system.
"""
from fastapi import APIRouter, Depends

from ..db import get_db

router = APIRouter(prefix="/api", tags=["search"])

PER_KIND_LIMIT = 6


def _snip(text: str, n: int = 90) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


@router.get("/search")
def search(q: str = "", conn=Depends(get_db)):
    q = (q or "").strip()
    if not q:
        return {"results": []}
    like = f"%{q}%"
    out = []

    # entries — match title or body
    for r in conn.execute(
        "SELECT id, title, body, type, created_at FROM entries"
        " WHERE title LIKE ? OR body LIKE ?"
        " ORDER BY id DESC LIMIT ?",
        (like, like, PER_KIND_LIMIT),
    ):
        out.append({
            "kind": "entry",
            "id": r["id"],
            "title": r["title"],
            "subtitle": f"{r['type']} · {r['created_at'][:10]}",
            "icon": "📝",
        })

    # captures — match raw text
    for r in conn.execute(
        "SELECT id, raw_text, created_at FROM captures WHERE raw_text LIKE ?"
        " ORDER BY id DESC LIMIT ?",
        (like, PER_KIND_LIMIT),
    ):
        out.append({
            "kind": "capture",
            "id": r["id"],
            "title": _snip(r["raw_text"]),
            "subtitle": (r["created_at"] or "")[:16].replace("T", " "),
            "icon": "💬",
        })

    # Local work — title and description are the durable board context.
    for r in conn.execute(
        "SELECT id, title, description, created_at FROM work_items "
        "WHERE (title LIKE ? OR description LIKE ?) "
        "AND NOT EXISTS(SELECT 1 FROM work_removals r WHERE r.work_item_id=work_items.id AND r.restored_at IS NULL) "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        (like, like, PER_KIND_LIMIT),
    ):
        out.append({
            "kind": "work_item",
            "id": r["id"],
            "work_item_id": r["id"],
            "title": r["title"],
            "snippet": _snip(r["description"]),
            "subtitle": "Work context",
            "url": f"/work/{r['id']}",
            "created_at": r["created_at"],
            "icon": "📋",
        })

    # Activity is searched by what was actually written in the activity body;
    # joining here avoids one lookup per row and keeps a stable work destination.
    for r in conn.execute(
        "SELECT wa.id, wa.work_item_id, wa.activity_type, wa.body, wa.created_at, wi.title "
        "FROM work_activity wa JOIN work_items wi ON wi.id=wa.work_item_id "
        "WHERE wa.body LIKE ? AND NOT EXISTS(SELECT 1 FROM work_removals r WHERE r.work_item_id=wi.id AND r.restored_at IS NULL) ORDER BY wa.created_at DESC, wa.id DESC LIMIT ?",
        (like, PER_KIND_LIMIT),
    ):
        out.append({
            "kind": "work_activity",
            "id": r["id"],
            "work_item_id": r["work_item_id"],
            "title": r["title"],
            "snippet": _snip(r["body"]),
            "subtitle": f"{(r['activity_type'] or 'activity').replace('_', ' ').title()} · "
                        f"{(r['created_at'] or '')[:16].replace('T', ' ')}",
            "activity_type": r["activity_type"],
            "url": f"/work/{r['work_item_id']}",
            "created_at": r["created_at"],
            "icon": "✦",
        })

    # cached ClickUp tasks — match by name
    for r in conn.execute(
        "SELECT task_id, name, status, list_name, url FROM clickup_tasks_cache"
        " WHERE name LIKE ?"
        " ORDER BY synced_at DESC LIMIT ?",
        (like, PER_KIND_LIMIT),
    ):
        out.append({
            "kind": "clickup_task",
            "id": r["task_id"],
            "title": r["name"] or r["task_id"],
            "subtitle": " · ".join(x for x in (r["status"], r["list_name"]) if x),
            "url": r["url"] or "",
            "icon": "✅",
        })

    # cached MRs — match by title, project, or branch substring
    for r in conn.execute(
        "SELECT mr_id, title, project, state, url, author, source_branch"
        " FROM gitlab_mrs_cache WHERE title LIKE ? OR project LIKE ?"
        " OR source_branch LIKE ?"
        " ORDER BY updated_at DESC LIMIT ?",
        (like, like, like, PER_KIND_LIMIT),
    ):
        bits = [r["project"], r["state"]]
        if r["author"]:
            bits.append(f"by {r['author']}")
        out.append({
            "kind": "mr",
            "id": r["mr_id"],
            "title": r["title"] or r["mr_id"],
            "subtitle": " · ".join(b for b in bits if b),
            "url": r["url"] or "",
            "icon": "🔀",
        })

    return {"results": out}
