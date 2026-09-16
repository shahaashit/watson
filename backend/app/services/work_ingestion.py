"""Import cached, read-only connector data into Watson's local work board.

This service intentionally treats connector rows as discovery signals.  It
only creates local cards and links; it never changes an existing card's lane,
position, owner, or manually chosen title.
"""
from collections import OrderedDict
from contextlib import contextmanager

from ..models import now_iso
from . import clickup_client, gitlab_client, work_items, work_suppression


@contextmanager
def _atomic(conn):
    """Commit standalone scheduled imports without disrupting an outer caller."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT work_ingestion")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT work_ingestion")
            conn.execute("RELEASE SAVEPOINT work_ingestion")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT work_ingestion")
        return
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _mr_groups(rows):
    groups = OrderedDict()
    for row in rows:
        clickup_id = gitlab_client.clickup_id_from_mr(row)
        key = ("clickup", clickup_id) if clickup_id else ("mr", row["mr_id"])
        groups.setdefault(key, []).append(row)
    return groups


def _owner_for_author(conn, username: str | None) -> int | None:
    username = (username or "").strip()
    if not username:
        return None
    row = conn.execute(
        "SELECT person_id FROM person_identities "
        "WHERE source='gitlab' AND external_id=?",
        (username,),
    ).fetchone()
    if row is not None:
        return row["person_id"]

    timestamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO people (display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
        "VALUES (?, 0, 0, 0, ?, ?)",
        (username, timestamp, timestamp),
    )
    person_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO person_identities (person_id, source, external_id, display_value) "
        "VALUES (?, 'gitlab', ?, ?)",
        (person_id, username, username),
    )
    return person_id


def _linked_item(conn, source_type: str, external_id: str):
    return conn.execute(
        "SELECT wi.* FROM work_items wi JOIN work_links wl ON wl.work_item_id=wi.id "
        "WHERE wl.source_type=? AND wl.external_id=?",
        (source_type, external_id),
    ).fetchone()


def _group_item(conn, key, rows):
    source_type, external_id = key
    if source_type == "clickup":
        item = _linked_item(conn, "clickup", external_id)
        if item is not None:
            return item
    for row in rows:
        item = _linked_item(conn, "gitlab_mr", row["mr_id"])
        if item is not None:
            return item
    return None


def _add_link(conn, item_id: int, *, source_type: str, external_id: str, url="", label="") -> bool:
    try:
        before = conn.execute(
            "SELECT 1 FROM work_links WHERE source_type=? AND external_id=?",
            (source_type, external_id),
        ).fetchone()
        work_items.add_work_link(
            conn,
            item_id,
            source_type=source_type,
            external_id=external_id,
            url=url or "",
            label=label or "",
        )
        return before is None
    except ValueError as error:
        # A manually linked card wins over discovery; never move its link.
        if str(error) == "external link is already linked to another work item":
            return False
        raise


def _update_generated_title(conn, item, title: str) -> bool:
    title = (title or "").strip()
    if item["title_is_manual"] or not title or title == item["title"]:
        return False
    conn.execute(
        "UPDATE work_items SET title=?, updated_at=? WHERE id=?",
        (title, now_iso(), item["id"]),
    )
    return True


def _ingest_groups(conn, groups) -> dict[str, int]:
    created = updated = 0
    for key, rows in groups.items():
        rows = [row for row in rows if not work_suppression.mr_removed(conn, row['mr_id'])]
        if not rows or (key[0] == 'clickup' and work_suppression.source_removed(conn, 'clickup', key[1])):
            continue
        item = _group_item(conn, key, rows)
        if item is not None and work_suppression.removed_at(conn, item['id']):
            continue
        group_created = item is None
        cached_clickup = None
        if key[0] == "clickup":
            cached_clickup = conn.execute(
                "SELECT url, name FROM clickup_tasks_cache WHERE task_id=?", (key[1],)
            ).fetchone()
        changed = False
        if item is None:
            item = work_items.create_work_item(
                conn,
                title=(rows[0]["title"] or f"Merge request {rows[0]['mr_id']}").strip(),
                state="next",
                owner_person_id=_owner_for_author(conn, rows[0]["author_username"]),
                origin="discovery",
            )
            created += 1
        else:
            # ClickUp is the canonical generated title once its task is
            # cached. Reconciliation alone updates that title, so a later MR
            # cache refresh cannot briefly undo it. Without a cached task,
            # the generated MR title remains the useful fallback.
            if key[0] != "clickup" or cached_clickup is None:
                changed = _update_generated_title(conn, item, rows[0]["title"])
            item = conn.execute("SELECT * FROM work_items WHERE id=?", (item["id"],)).fetchone()

        if key[0] == "clickup":
            changed = _add_link(
                conn,
                item["id"],
                source_type="clickup",
                external_id=key[1],
                url=(cached_clickup["url"] if cached_clickup else ""),
                label=(cached_clickup["name"] if cached_clickup else ""),
            ) or changed
        for row in rows:
            changed = _add_link(
                conn,
                item["id"],
                source_type="gitlab_mr",
                external_id=row["mr_id"],
                url=row["url"],
                label=row["title"],
            ) or changed
        if changed and not group_created:
            updated += 1
    return {"created": created, "updated": updated}


def _group_is_team_relevant(conn, key, rows) -> bool:
    if any(work_items.gitlab_mr_is_team_relevant(conn, row) for row in rows):
        return True
    if key[0] != "clickup":
        return False
    cached_clickup = conn.execute(
        "SELECT * FROM clickup_tasks_cache WHERE task_id=?", (key[1],)
    ).fetchone()
    return (
        cached_clickup is not None
        and work_items.clickup_task_is_team_relevant(conn, cached_clickup)
    )


def ingest_cached_mrs(conn) -> dict[str, int]:
    """Append only cached opened MRs involving the user or tracked people."""
    rows = conn.execute(
        "SELECT * FROM gitlab_mrs_cache WHERE state='opened' ORDER BY mr_id"
    ).fetchall()
    groups = _mr_groups([row for row in rows if not work_suppression.mr_removed(conn, row['mr_id'])])
    relevant_groups = OrderedDict(
        (key, group_rows)
        for key, group_rows in groups.items()
        if _group_is_team_relevant(conn, key, group_rows)
    )
    with _atomic(conn):
        return _ingest_groups(conn, relevant_groups)


def retire_terminal_mr_work(conn) -> int:
    """Hide discovered cards only when every linked MR is known to be terminal."""
    with _atomic(conn):
        retired = 0
        items = conn.execute(
            "SELECT DISTINCT wi.id FROM work_items wi "
            "JOIN work_links wl ON wl.work_item_id=wi.id "
            "WHERE wi.origin='discovery' AND wl.source_type='gitlab_mr' "
            "ORDER BY wi.id"
        ).fetchall()
        for item in items:
            if work_suppression.removed_at(conn, item['id']):
                continue
            linked_states = conn.execute(
                "SELECT gm.state FROM active_work_links wl "
                "LEFT JOIN gitlab_mrs_cache gm ON gm.mr_id=wl.external_id "
                "WHERE wl.work_item_id=? AND wl.source_type='gitlab_mr' "
                "ORDER BY wl.id",
                (item["id"],),
            ).fetchall()
            states = [row["state"] for row in linked_states]
            if not states or any(state not in {"closed", "merged"} for state in states):
                continue
            conn.execute(
                "UPDATE work_items SET origin='ignored', updated_at=? WHERE id=?",
                (now_iso(), item["id"]),
            )
            retired += 1
        return retired


def retire_unselected_gitlab_work(conn) -> int:
    """Hide automatic MR-only cards whose projects left the allowlist.

    Manual imports are explicit exceptions. Cards with a ClickUp link also
    remain useful independently of the GitLab repository selection.
    Intentionally removed links are not retirement evidence; cards with no
    active MR links retain their lifecycle so those links can be restored.
    """
    selected = gitlab_client.selected_project_ids(conn)
    if selected is None:
        return 0
    with _atomic(conn):
        retired = 0
        items = conn.execute(
            "SELECT DISTINCT wi.id FROM work_items wi "
            "JOIN active_work_links mr ON mr.work_item_id=wi.id "
            " AND mr.source_type='gitlab_mr' "
            "WHERE wi.origin='discovery' "
            "AND NOT EXISTS (SELECT 1 FROM active_work_links cu "
            " WHERE cu.work_item_id=wi.id AND cu.source_type='clickup') "
            "ORDER BY wi.id"
        ).fetchall()
        for item in items:
            if work_suppression.removed_at(conn, item['id']):
                continue
            mr_ids = [
                row["external_id"]
                for row in conn.execute(
                    "SELECT external_id FROM active_work_links "
                    "WHERE work_item_id=? AND source_type='gitlab_mr'",
                    (item["id"],),
                )
            ]
            if any(
                mr_id.split("!", 1)[0] in selected for mr_id in mr_ids
            ):
                continue
            conn.execute(
                "UPDATE work_items SET origin='ignored', updated_at=? WHERE id=?",
                (now_iso(), item["id"]),
            )
            retired += 1
        return retired


def retire_missing_gitlab_work(conn) -> int:
    """Hide automatic MR-only cards absent from a completed GitLab refresh.

    The refreshed cache is the authoritative set of actionable MRs. Manual
    imports and ClickUp-backed work remain explicit/local exceptions.
    Only active links count: suppressed sources are deliberately absent from
    refreshes, not evidence that their parent work should be retired.
    """
    with _atomic(conn):
        rows = conn.execute(
            "SELECT DISTINCT wi.id FROM work_items wi "
            "JOIN active_work_links mr ON mr.work_item_id=wi.id "
            " AND mr.source_type='gitlab_mr' "
            "WHERE wi.origin='discovery' "
            "AND NOT EXISTS (SELECT 1 FROM active_work_links cu "
            " WHERE cu.work_item_id=wi.id AND cu.source_type='clickup') "
            "AND NOT EXISTS (SELECT 1 FROM active_work_links current "
            " JOIN gitlab_mrs_cache gm ON gm.mr_id=current.external_id "
            " WHERE current.work_item_id=wi.id "
            " AND current.source_type='gitlab_mr') "
            "ORDER BY wi.id"
        ).fetchall()
        timestamp = now_iso()
        for row in rows:
            if work_suppression.removed_at(conn, row['id']):
                continue
            conn.execute(
                "UPDATE work_items SET origin='ignored', updated_at=? WHERE id=?",
                (timestamp, row["id"]),
            )
        return len(rows)


def linked_clickup_ids(conn) -> list[str]:
    """Refresh active, in-scope work without discarding historical links.

    Manual imports remain explicit opt-ins. Discovered work must still involve
    the user or a tracked teammate in the connector caches; legacy migrated
    work is scoped by its local owner.
    """
    rows = conn.execute(
        "SELECT wi.*, wl.external_id, p.is_self, p.is_tracked FROM work_items wi "
        "JOIN work_links wl ON wl.work_item_id=wi.id AND wl.source_type='clickup' "
        "LEFT JOIN people p ON p.id=wi.owner_person_id "
        "WHERE wi.state != 'done' AND wi.completed_at IS NULL "
        "AND wi.origin != 'ignored' "
        "ORDER BY wl.external_id COLLATE NOCASE, wl.external_id"
    ).fetchall()
    ids = []
    for row in rows:
        if work_suppression.removed_at(conn, row['id']):
            continue
        if row["origin"] == "manual":
            relevant = True
        elif row["origin"] == "discovery":
            relevant = work_items._discovery_is_team_relevant(conn, row)
        else:
            relevant = bool(row["is_self"] or row["is_tracked"])
        if relevant and row["external_id"] not in ids:
            ids.append(row["external_id"])
    return ids


def reconcile_clickup_titles(conn) -> int:
    """Use refreshed ClickUp titles only for cards Watson generated."""
    with _atomic(conn):
        changed = 0
        rows = conn.execute(
            "SELECT wi.*, ct.name AS clickup_name FROM work_items wi "
            "JOIN work_links wl ON wl.work_item_id=wi.id AND wl.source_type='clickup' "
            "JOIN clickup_tasks_cache ct ON ct.task_id=wl.external_id "
            "WHERE wi.title_is_manual=0 ORDER BY wi.id, wl.id"
        ).fetchall()
        seen = set()
        for row in rows:
            if work_suppression.removed_at(conn, row['id']):
                continue
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            new_title = (row["clickup_name"] or "").strip()
            old_title = row["title"]
            if not new_title or new_title == old_title:
                continue
            conn.execute(
                "UPDATE work_items SET title=?, updated_at=? WHERE id=?",
                (new_title, now_iso(), row["id"]),
            )
            work_items.add_activity(
                conn,
                row["id"],
                activity_type="system",
                body=f"Updated generated title from {old_title!r} to {new_title!r}",
                metadata={"old_title": old_title, "new_title": new_title},
            )
            changed += 1
        return changed


def _upsert_clickup_task(conn, task: dict) -> None:
    row = clickup_client._task_to_cache_row(task)
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type, list_name, url, "
        "assignees, due_date, date_closed, synced_at) VALUES "
        "(:task_id, :name, :status, :status_type, :list_name, :url, :assignees, :due_date, "
        ":date_closed, :synced_at) ON CONFLICT(task_id) DO UPDATE SET "
        "name=excluded.name, status=excluded.status, status_type=excluded.status_type, "
        "list_name=excluded.list_name, url=excluded.url, assignees=excluded.assignees, "
        "due_date=excluded.due_date, date_closed=excluded.date_closed, synced_at=excluded.synced_at",
        {**row, "synced_at": now_iso()},
    )


def _import_clickup(conn, url: str, task_id: str) -> dict:
    work_suppression.require_sources(conn, clickup_id=task_id)
    existing = _linked_item(conn, "clickup", task_id)
    task = clickup_client.get_task_resilient(task_id)
    if not isinstance(task, dict) or str(task.get("id") or "") != task_id:
        raise RuntimeError("ClickUp returned an invalid task")
    _upsert_clickup_task(conn, task)
    if existing is not None:
        _update_generated_title(conn, existing, task.get("name") or "")
        conn.execute(
            "UPDATE work_items SET origin='manual', updated_at=? WHERE id=?",
            (now_iso(), existing["id"]),
        )
        return {"work_item": work_items.get_work_detail(conn, existing["id"]), "created": False}
    item = work_items.create_work_item(
        conn,
        title=(task.get("name") or f"ClickUp {task_id}").strip(),
        state="next",
        origin="discovery",
    )
    conn.execute(
        "UPDATE work_items SET origin='manual', updated_at=? WHERE id=?",
        (now_iso(), item["id"]),
    )
    _add_link(
        conn, item["id"], source_type="clickup", external_id=task_id,
        url=task.get("url") or "", label=task.get("name") or "",
    )
    return {"work_item": work_items.get_work_detail(conn, item["id"]), "created": True}


def _import_gitlab(conn, url: str) -> dict:
    returned_ids = gitlab_client.ensure_mrs_cached(conn, url)
    rows = []
    for mr_id in returned_ids:
        row = conn.execute("SELECT * FROM gitlab_mrs_cache WHERE mr_id=?", (mr_id,)).fetchone()
        if row is not None:
            rows.append(row)
    if not rows:
        # `ensure_mrs_cached` deliberately returns no IDs for an already cached
        # URL.  It is still a valid idempotent import, but only this exact URL
        # is eligible — never every row in the cache.
        row = conn.execute("SELECT * FROM gitlab_mrs_cache WHERE url=?", (url,)).fetchone()
        if row is None:
            raise RuntimeError("GitLab merge request could not be cached")
        rows = [row]
    groups = _mr_groups(rows)
    work_suppression.require_sources(conn, [row['mr_id'] for row in rows])
    existing_items = [
        _group_item(conn, key, group_rows)
        for key, group_rows in groups.items()
    ]
    with _atomic(conn):
        _ingest_groups(conn, groups)
        item = next((item for item in existing_items if item is not None), None)
        if item is None:
            item = _linked_item(conn, "gitlab_mr", rows[0]["mr_id"])
        conn.execute(
            "UPDATE work_items SET origin='manual', updated_at=? WHERE id=?",
            (now_iso(), item["id"]),
        )
    return {
        "work_item": work_items.get_work_detail(conn, item["id"]),
        "created": not any(item is not None for item in existing_items),
    }


def import_external_url(conn, url: str) -> dict:
    """Safely import one exact ClickUp task or GitLab merge-request URL."""
    candidate = (url or "").strip()
    clickup_match = gitlab_client.CLICKUP_URL_RE.fullmatch(candidate)
    if clickup_match:
        with _atomic(conn):
            return _import_clickup(conn, candidate, clickup_match.group(1))
    if gitlab_client._MR_URL_RE.fullmatch(candidate):
        return _import_gitlab(conn, candidate)
    raise ValueError("unsupported work import URL")
