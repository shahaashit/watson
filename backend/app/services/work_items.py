"""Watson-local work item mutations, ordering, and read models.

This service deliberately has no integration-client imports: changing a card is
local state only.  Every mutation owns its SQLite transaction (or savepoint
when a caller already has one) so a lane cannot be observed half-reordered.
"""
import json
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import (
    action_dict,
    now_iso,
    person_dict,
    reminder_dict,
    work_activity_dict,
    work_item_dict,
    work_link_dict,
)


VALID_STATES = {"today", "next", "waiting", "done"}
UNSET = object()
STATE_ORDER_SQL = "CASE state WHEN 'today' THEN 1 WHEN 'next' THEN 2 WHEN 'waiting' THEN 3 WHEN 'done' THEN 4 END"


@contextmanager
def _mutation(conn):
    """Create an atomic mutation without committing a caller's outer work."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT work_items_mutation")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT work_items_mutation")
            conn.execute("RELEASE SAVEPOINT work_items_mutation")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT work_items_mutation")
        return

    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _item_row(conn, work_item_id: int):
    return conn.execute("SELECT * FROM work_items WHERE id=?", (work_item_id,)).fetchone()


def _require_item(conn, work_item_id: int):
    row = _item_row(conn, work_item_id)
    if row is None:
        raise ValueError("work item not found")
    return row


def _validate_state(state: str) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"invalid state: {state}")


def _validate_owner(conn, owner_person_id: int | None) -> None:
    if owner_person_id is None:
        return
    exists = conn.execute("SELECT 1 FROM people WHERE id=?", (owner_person_id,)).fetchone()
    if exists is None:
        raise ValueError("owner person does not exist")


def _canonical_owner_display(owner_person_id: int | None, owner_display: str | None) -> str:
    """Keep person-backed and fallback owner lanes mutually exclusive."""
    if owner_person_id is not None or owner_display is None:
        return ""
    return owner_display


def _lane_rows(conn, owner_person_id: int | None, owner_display: str, state: str):
    return conn.execute(
        "SELECT * FROM work_items WHERE owner_person_id IS ? AND owner_display = ? "
        "AND state = ? ORDER BY priority_position, id",
        (owner_person_id, owner_display, state),
    ).fetchall()


def _normalize_lane(conn, owner_person_id: int | None, owner_display: str, state: str) -> None:
    _write_lane_order(conn, _lane_rows(conn, owner_person_id, owner_display, state))


def _write_lane_order(conn, rows) -> None:
    for index, row in enumerate(rows, start=1):
        conn.execute("UPDATE work_items SET position=? WHERE id=?", (index * 1000, row["id"]))


def _append_position(conn, owner_person_id: int | None, owner_display: str, state: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) AS position FROM work_items "
        "WHERE owner_person_id IS ? AND owner_display = ? AND state = ?",
        (owner_person_id, owner_display, state),
    ).fetchone()
    return row["position"] + 1000


def _owner_rows(conn, owner_person_id: int | None, owner_display: str):
    return conn.execute(
        "SELECT * FROM work_items WHERE owner_person_id IS ? AND owner_display=? "
        "ORDER BY priority_position, id",
        (owner_person_id, owner_display),
    ).fetchall()


def _append_priority_position(
    conn, owner_person_id: int | None, owner_display: str
) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(priority_position), 0) AS priority_position "
        "FROM work_items WHERE owner_person_id IS ? AND owner_display=?",
        (owner_person_id, owner_display),
    ).fetchone()
    return row["priority_position"] + 1000


def _write_owner_priority(conn, rows) -> None:
    for index, row in enumerate(rows, start=1):
        conn.execute(
            "UPDATE work_items SET priority_position=? WHERE id=?",
            (index * 1000, row["id"]),
        )


def ensure_self_person(conn, display_name: str) -> int:
    """Return the one local user, creating it if the database is new."""
    with _mutation(conn):
        existing = conn.execute("SELECT id FROM people WHERE is_self=1").fetchone()
        if existing:
            return existing["id"]
        timestamp = now_iso()
        cursor = conn.execute(
            "INSERT INTO people (display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
            "VALUES (?, 1, 1, 0, ?, ?)",
            (display_name, timestamp, timestamp),
        )
        return cursor.lastrowid


def create_work_item(
    conn,
    *,
    title: str,
    description: str = "",
    state: str = "next",
    owner_person_id: int | None = None,
    owner_display: str = "",
    origin: str = "manual",
) -> dict:
    _validate_state(state)
    with _mutation(conn):
        _validate_owner(conn, owner_person_id)
        owner_display = _canonical_owner_display(owner_person_id, owner_display)
        timestamp = now_iso()
        cursor = conn.execute(
            "INSERT INTO work_items (title, title_is_manual, description, state, owner_person_id, "
            "owner_display, position, priority_position, origin, created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                int(origin == "manual"),
                description,
                state,
                owner_person_id,
                owner_display,
                _append_position(conn, owner_person_id, owner_display, state),
                _append_priority_position(conn, owner_person_id, owner_display),
                origin,
                timestamp,
                timestamp,
                timestamp if state == "done" else None,
            ),
        )
        _normalize_lane(conn, owner_person_id, owner_display, state)
        return work_item_dict(_require_item(conn, cursor.lastrowid))


def update_work_item(
    conn,
    work_item_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    owner_person_id=UNSET,
    owner_display=UNSET,
) -> dict:
    """Patch an item; ``UNSET`` preserves nullable ownership fields.

    A router can pass ``None`` explicitly to clear the owner, while a title or
    description-only patch leaves it alone.
    """
    with _mutation(conn):
        item = _require_item(conn, work_item_id)
        new_owner_id = item["owner_person_id"] if owner_person_id is UNSET else owner_person_id
        new_owner_display = _canonical_owner_display(
            new_owner_id,
            item["owner_display"] if owner_display is UNSET else owner_display,
        )
        _validate_owner(conn, new_owner_id)
        ownership_changed = (
            new_owner_id != item["owner_person_id"]
            or new_owner_display != item["owner_display"]
        )
        timestamp = now_iso()
        new_title = item["title"] if title is None else title
        new_description = item["description"] if description is None else description
        new_position = (
            _append_position(conn, new_owner_id, new_owner_display, item["state"])
            if ownership_changed
            else item["position"]
        )
        new_priority_position = (
            _append_priority_position(conn, new_owner_id, new_owner_display)
            if ownership_changed
            else item["priority_position"]
        )
        conn.execute(
            "UPDATE work_items SET title=?, title_is_manual=?, description=?, owner_person_id=?, "
            "owner_display=?, position=?, priority_position=?, updated_at=? WHERE id=?",
            (
                new_title,
                1 if title is not None else item["title_is_manual"],
                new_description,
                new_owner_id,
                new_owner_display,
                new_position,
                new_priority_position,
                timestamp,
                work_item_id,
            ),
        )
        if ownership_changed:
            _normalize_lane(conn, item["owner_person_id"], item["owner_display"], item["state"])
            _normalize_lane(conn, new_owner_id, new_owner_display, item["state"])
            _write_owner_priority(
                conn,
                _owner_rows(conn, item["owner_person_id"], item["owner_display"]),
            )
            _write_owner_priority(conn, _owner_rows(conn, new_owner_id, new_owner_display))
        return work_item_dict(_require_item(conn, work_item_id))


def merge_items(conn, survivor_id: int, duplicate_ids: list[int]) -> dict:
    """Collapse local cards atomically while preserving every association."""
    duplicate_ids = list(dict.fromkeys(duplicate_ids))
    if survivor_id in duplicate_ids:
        raise ValueError("survivor cannot also be a duplicate")
    if not duplicate_ids:
        return work_item_dict(_require_item(conn, survivor_id))

    with _mutation(conn):
        item_ids = [survivor_id, *duplicate_ids]
        rows = [dict(_require_item(conn, item_id)) for item_id in item_ids]
        owner_keys = {
            ("person", row["owner_person_id"])
            if row["owner_person_id"] is not None
            else ("display", row["owner_display"].strip().casefold())
            for row in rows
            if row["owner_person_id"] is not None or row["owner_display"].strip()
        }
        if len(owner_keys) > 1:
            raise ValueError("work item owners are incompatible")

        placeholders = ",".join("?" for _ in duplicate_ids)
        conn.execute(
            "DELETE FROM work_links WHERE work_item_id IN (" + placeholders + ") "
            "AND EXISTS (SELECT 1 FROM work_links survivor_link "
            "WHERE survivor_link.work_item_id=? "
            "AND survivor_link.source_type=work_links.source_type "
            "AND survivor_link.external_id=work_links.external_id)",
            (*duplicate_ids, survivor_id),
        )
        conn.execute(
            "UPDATE work_links SET work_item_id=? WHERE work_item_id IN (" + placeholders + ")",
            (survivor_id, *duplicate_ids),
        )
        for table, column in (
            ("captures", "work_item_id"),
            ("entries", "work_item_id"),
            ("reminders", "work_item_id"),
            ("work_activity", "work_item_id"),
            ("work_inbox", "suggested_work_item_id"),
            ("review_groups", "work_item_id"),
        ):
            conn.execute(
                f"UPDATE {table} SET {column}=? WHERE {column} IN ({placeholders})",
                (survivor_id, *duplicate_ids),
            )

        survivor = rows[0]
        title = survivor["title"]
        title_is_manual = survivor["title_is_manual"]
        if not title_is_manual:
            canonical = conn.execute(
                "SELECT title FROM review_groups WHERE work_item_id=? ORDER BY id LIMIT 1",
                (survivor_id,),
            ).fetchone()
            if canonical is not None:
                title = canonical["title"]
        conn.execute(
            "UPDATE work_items SET title=?, title_is_manual=?, priority_position=?, "
            "updated_at=? WHERE id=?",
            (
                title,
                title_is_manual,
                min(row["priority_position"] for row in rows),
                now_iso(),
                survivor_id,
            ),
        )
        conn.execute(
            "UPDATE work_items SET origin='ignored', updated_at=? "
            "WHERE id IN (" + placeholders + ")",
            (now_iso(), *duplicate_ids),
        )
        return work_item_dict(_require_item(conn, survivor_id))


def move_work_item(
    conn,
    work_item_id: int,
    *,
    state: str,
    before_id: int | None = None,
    after_id: int | None = None,
) -> dict:
    _validate_state(state)
    if before_id is not None and after_id is not None:
        raise ValueError("before_id and after_id cannot both be set")
    with _mutation(conn):
        item = _require_item(conn, work_item_id)
        if before_id == work_item_id or after_id == work_item_id:
            raise ValueError("placement reference cannot be the moved item")
        placement_id = before_id if before_id is not None else after_id
        if placement_id is not None:
            placement = _require_item(conn, placement_id)
            if (
                placement["owner_person_id"] != item["owner_person_id"]
                or placement["owner_display"] != item["owner_display"]
            ):
                raise ValueError("placement reference is outside the owner lane")

        old_lane = (item["owner_person_id"], item["owner_display"], item["state"])
        owner_rows = [
            row
            for row in _owner_rows(
                conn, item["owner_person_id"], item["owner_display"]
            )
            if row["id"] != work_item_id
        ]
        if before_id is not None:
            index = next(
                row_index
                for row_index, row in enumerate(owner_rows)
                if row["id"] == before_id
            )
        elif after_id is not None:
            index = next(
                row_index
                for row_index, row in enumerate(owner_rows)
                if row["id"] == after_id
            ) + 1
        else:
            state_indexes = [
                row_index
                for row_index, row in enumerate(owner_rows)
                if row["state"] == state
            ]
            index = state_indexes[-1] + 1 if state_indexes else len(owner_rows)
        owner_rows.insert(index, item)

        timestamp = now_iso()
        completed_at = item["completed_at"]
        if item["state"] != "done" and state == "done":
            completed_at = timestamp
        elif item["state"] == "done" and state != "done":
            completed_at = None
        conn.execute(
            "UPDATE work_items SET state=?, position=?, updated_at=?, completed_at=? WHERE id=?",
            (
                state,
                _append_position(
                    conn, item["owner_person_id"], item["owner_display"], state
                ),
                timestamp,
                completed_at,
                work_item_id,
            ),
        )
        destination_lane = (item["owner_person_id"], item["owner_display"], state)
        _write_owner_priority(conn, owner_rows)
        if old_lane != destination_lane:
            _normalize_lane(conn, *old_lane)
        _normalize_lane(conn, *destination_lane)
        return work_item_dict(_require_item(conn, work_item_id))


def add_work_link(
    conn,
    work_item_id: int,
    *,
    source_type: str,
    external_id: str,
    url: str = "",
    label: str = "",
) -> dict:
    with _mutation(conn):
        _require_item(conn, work_item_id)
        existing = conn.execute(
            "SELECT * FROM work_links WHERE source_type=? AND external_id=?",
            (source_type, external_id),
        ).fetchone()
        if existing is not None:
            if existing["work_item_id"] == work_item_id:
                return work_link_dict(existing)
            raise ValueError("external link is already linked to another work item")
        cursor = conn.execute(
            "INSERT INTO work_links (work_item_id, source_type, external_id, url, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (work_item_id, source_type, external_id, url, label, now_iso()),
        )
        return work_link_dict(conn.execute("SELECT * FROM work_links WHERE id=?", (cursor.lastrowid,)).fetchone())


def add_activity(
    conn,
    work_item_id: int,
    *,
    activity_type: str,
    body: str,
    metadata: dict | None = None,
    capture_id: int | None = None,
    reminder_id: int | None = None,
    pending_action_id: int | None = None,
) -> dict:
    with _mutation(conn):
        _require_item(conn, work_item_id)
        cursor = conn.execute(
            "INSERT INTO work_activity (work_item_id, activity_type, body, metadata_json, capture_id, "
            "reminder_id, pending_action_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                work_item_id,
                activity_type,
                body,
                json.dumps(metadata or {}, sort_keys=True),
                capture_id,
                reminder_id,
                pending_action_id,
                now_iso(),
            ),
        )
        return work_activity_dict(conn.execute("SELECT * FROM work_activity WHERE id=?", (cursor.lastrowid,)).fetchone())


def get_work_detail(conn, work_item_id: int) -> dict | None:
    item = _item_row(conn, work_item_id)
    if item is None:
        return None
    detail = work_item_dict(item)
    detail["links"] = [work_link_dict(row) for row in conn.execute(
        "SELECT * FROM work_links WHERE work_item_id=? ORDER BY created_at, id", (work_item_id,)
    ).fetchall()]
    detail["activity"] = [work_activity_dict(row) for row in conn.execute(
        "SELECT * FROM work_activity WHERE work_item_id=? ORDER BY created_at DESC, id DESC", (work_item_id,)
    ).fetchall()]
    detail["reminders"] = [reminder_dict(row) for row in conn.execute(
        "SELECT * FROM reminders WHERE work_item_id=? OR id IN "
        "(SELECT reminder_id FROM work_activity WHERE work_item_id=? AND reminder_id IS NOT NULL) "
        "ORDER BY due_at, id",
        (work_item_id, work_item_id),
    ).fetchall()]
    detail["pending_actions"] = [action_dict(row) for row in conn.execute(
        "SELECT * FROM pending_actions WHERE id IN ("
        "SELECT pending_actions.id FROM pending_actions JOIN captures "
        "ON captures.id=pending_actions.capture_id WHERE captures.work_item_id=? "
        "UNION SELECT pending_action_id FROM work_activity "
        "WHERE work_item_id=? AND pending_action_id IS NOT NULL) "
        "ORDER BY created_at DESC, id DESC",
        (work_item_id, work_item_id),
    ).fetchall()]
    return detail


def _completed_today(value, timezone_name: str) -> bool:
    if not value:
        return False
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = ZoneInfo("UTC")
    try:
        completed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone)
    return completed.astimezone(timezone).date() == datetime.now(timezone).date()


def my_work(
    conn,
    self_person_id: int,
    *,
    separate_by_status: bool = True,
    timezone_name: str = "UTC",
) -> dict:
    _validate_owner(conn, self_person_id)
    result = {state: [] for state in ("today", "next", "waiting", "done")}
    rows = conn.execute(
        "SELECT wi.* FROM work_items wi WHERE wi.owner_person_id=? "
        "AND NOT EXISTS(SELECT 1 FROM review_groups rg WHERE rg.work_item_id=wi.id) "
        "AND NOT EXISTS(SELECT 1 FROM work_links wl JOIN managed_tasks mt "
        "ON mt.clickup_task_id=wl.external_id WHERE wl.work_item_id=wi.id "
        "AND wl.source_type='clickup' AND lower(mt.category)='review') "
        "ORDER BY wi.priority_position, wi.id",
        (self_person_id,),
    ).fetchall()
    clickup_urls = {}
    for link in conn.execute(
        "SELECT wl.work_item_id, wl.external_id FROM work_links wl "
        "JOIN work_items wi ON wi.id=wl.work_item_id "
        "WHERE wi.owner_person_id=? AND wl.source_type='clickup' "
        "ORDER BY EXISTS(SELECT 1 FROM managed_tasks mt WHERE mt.clickup_task_id=wl.external_id) DESC, wl.id",
        (self_person_id,),
    ):
        task_id = link['external_id']
        if task_id and task_id.isascii() and task_id.isalnum():
            clickup_urls.setdefault(link['work_item_id'], f'https://app.clickup.com/t/{task_id}')
    def personal_item(row):
        return {**work_item_dict(row), 'clickup_url': clickup_urls.get(row['id'], '')}
    for row in rows:
        if row["state"] != "done" or _completed_today(row["completed_at"], timezone_name):
            result[row["state"]].append(personal_item(row))
    active = [personal_item(row) for row in rows if row["state"] != "done"]
    result["mode"] = "segregated" if separate_by_status else "flat"
    result["items"] = active
    return result


def _row_value(row, key: str, default=""):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _normalized_json_values(value) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {
        str(item).strip().casefold()
        for item in parsed
        if item is not None and str(item).strip()
    }


def _tracked_identity_values(conn, source: str) -> set[str]:
    rows = conn.execute(
        "SELECT p.display_name, pi.external_id, pi.display_value FROM people p "
        "LEFT JOIN person_identities pi ON pi.person_id=p.id AND pi.source=? "
        "WHERE p.is_tracked=1",
        (source,),
    ).fetchall()
    return {
        str(value).strip().casefold()
        for row in rows
        for value in (row["display_name"], row["external_id"], row["display_value"])
        if value is not None and str(value).strip()
    }


def _self_identity_values(conn, source: str) -> set[str]:
    rows = conn.execute(
        "SELECT p.display_name, pi.external_id, pi.display_value FROM people p "
        "LEFT JOIN person_identities pi ON pi.person_id=p.id AND pi.source=? "
        "WHERE p.is_self=1",
        (source,),
    ).fetchall()
    return {
        str(value).strip().casefold()
        for row in rows
        for value in (row["display_name"], row["external_id"], row["display_value"])
        if value is not None and str(value).strip()
    }


def gitlab_mr_is_team_relevant(conn, row) -> bool:
    """A cached MR matters when it involves Watson's user or a tracked person."""
    if _normalized_json_values(_row_value(row, "roles")):
        return True
    participants = {
        str(_row_value(row, "author_username")).strip().casefold()
    }
    participants |= _normalized_json_values(_row_value(row, "assignee_usernames"))
    participants |= _normalized_json_values(_row_value(row, "reviewer_usernames"))
    participants.discard("")
    return bool(participants & _tracked_identity_values(conn, "gitlab"))


def clickup_task_is_team_relevant(conn, row) -> bool:
    """A cached ClickUp task matters when one of its assignees is configured."""
    assignees = _normalized_json_values(_row_value(row, "assignees"))
    return bool(assignees & _tracked_identity_values(conn, "clickup"))


def _discovery_is_team_relevant(conn, item) -> bool:
    if item["origin"] != "discovery":
        return True
    links = conn.execute(
        "SELECT source_type, external_id FROM work_links WHERE work_item_id=?",
        (item["id"],),
    ).fetchall()
    for link in links:
        if link["source_type"] == "gitlab_mr":
            row = conn.execute(
                "SELECT * FROM gitlab_mrs_cache WHERE mr_id=?", (link["external_id"],)
            ).fetchone()
            if row is not None and gitlab_mr_is_team_relevant(conn, row):
                return True
        elif link["source_type"] == "clickup":
            row = conn.execute(
                "SELECT * FROM clickup_tasks_cache WHERE task_id=?", (link["external_id"],)
            ).fetchone()
            if row is not None and clickup_task_is_team_relevant(conn, row):
                return True
    return False


def _work_item_involves_self(conn, item) -> bool:
    if item["origin"] == "manual":
        return True
    links = conn.execute(
        "SELECT source_type, external_id FROM work_links WHERE work_item_id=?",
        (item["id"],),
    ).fetchall()
    self_clickup_values = None
    for link in links:
        if link["source_type"] == "gitlab_mr":
            row = conn.execute(
                "SELECT role FROM gitlab_mrs_cache WHERE mr_id=?",
                (link["external_id"],),
            ).fetchone()
            if row is not None and row["role"] in {"reviewer", "assignee", "engaged"}:
                return True
        elif link["source_type"] == "clickup":
            row = conn.execute(
                "SELECT assignees FROM clickup_tasks_cache WHERE task_id=?",
                (link["external_id"],),
            ).fetchone()
            if row is None:
                continue
            if self_clickup_values is None:
                self_clickup_values = _self_identity_values(conn, "clickup")
            if _normalized_json_values(row["assignees"]) & self_clickup_values:
                return True
    return False


def _gitlab_repositories(conn, work_item_id: int) -> list[str]:
    return [
        row["project"]
        for row in conn.execute(
            "SELECT DISTINCT gm.project FROM work_links wl "
            "JOIN gitlab_mrs_cache gm ON gm.mr_id=wl.external_id "
            "WHERE wl.work_item_id=? AND wl.source_type='gitlab_mr' "
            "AND gm.project IS NOT NULL AND trim(gm.project) <> '' "
            "ORDER BY gm.project COLLATE NOCASE, gm.project",
            (work_item_id,),
        )
    ]


def _team_items(
    conn,
    where_sql: str,
    params=(),
    *,
    relevant_only=False,
    me_mode=False,
    separate_by_status=False,
    timezone_name="UTC",
):
    rows = conn.execute(
        f"SELECT * FROM work_items WHERE origin <> 'ignored' AND ({where_sql}) "
        "ORDER BY priority_position, id",
        params,
    ).fetchall()
    if relevant_only:
        rows = [row for row in rows if _discovery_is_team_relevant(conn, row)]
    if me_mode:
        rows = [row for row in rows if _work_item_involves_self(conn, row)]
    rows = [
        row
        for row in rows
        if row["state"] != "done"
        or (
            separate_by_status
            and _completed_today(row["completed_at"], timezone_name)
        )
    ]
    items = []
    for row in rows:
        item = work_item_dict(row)
        item["repositories"] = _gitlab_repositories(conn, row["id"])
        items.append(item)
    return items


def team_work(
    conn,
    *,
    separate_by_status: bool = True,
    timezone_name: str = "UTC",
    me_mode: bool = False,
    self_person_id: int | None = None,
) -> dict:
    """Return your own lane, then tracked lanes, then the fixed Others/Unassigned."""
    lanes = []
    self_person = None
    if self_person_id is not None:
        self_person = conn.execute(
            "SELECT * FROM people WHERE id=?", (self_person_id,)
        ).fetchone()
    if self_person is not None:
        lanes.append({
            "name": self_person["display_name"],
            "person": person_dict(self_person),
            # Me mode narrows teammate lanes to work that touches you. Your own
            # lane is already all yours, so it stays whole.
            "items": _team_items(
                conn,
                "owner_person_id=?",
                (self_person["id"],),
                separate_by_status=separate_by_status,
                timezone_name=timezone_name,
            ),
        })
    tracked_people = conn.execute(
        "SELECT * FROM people WHERE is_tracked=1 AND is_self=0 ORDER BY lane_position, id"
    ).fetchall()
    for person in tracked_people:
        lanes.append({
            "name": person["display_name"],
            "person": person_dict(person),
            "items": _team_items(
                conn,
                "owner_person_id=?",
                (person["id"],),
                me_mode=me_mode,
                separate_by_status=separate_by_status,
                timezone_name=timezone_name,
            ),
        })
    lanes.append({
        "name": "Others",
        "person": None,
        "items": _team_items(
            conn,
            "(owner_person_id IN (SELECT id FROM people WHERE is_self=0 AND is_tracked=0) "
            "OR (owner_person_id IS NULL AND owner_display <> ''))",
            relevant_only=True,
            me_mode=me_mode,
            separate_by_status=separate_by_status,
            timezone_name=timezone_name,
        ),
    })
    lanes.append({
        "name": "Unassigned",
        "person": None,
        "items": _team_items(
            conn,
            "owner_person_id IS NULL AND owner_display = ''",
            relevant_only=True,
            me_mode=me_mode,
            separate_by_status=separate_by_status,
            timezone_name=timezone_name,
        ),
    })
    return {
        "mode": "segregated" if separate_by_status else "flat",
        "lanes": lanes,
    }
