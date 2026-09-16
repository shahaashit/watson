"""Durable storage for canonical groups of related GitLab review MRs."""

import json
from contextlib import contextmanager

from ..models import now_iso
from . import events


@contextmanager
def _atomic(conn):
    """Make repository writes atomic without committing an outer transaction."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT review_groups")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT review_groups")
            conn.execute("RELEASE SAVEPOINT review_groups")
            raise
        else:
            conn.execute("RELEASE SAVEPOINT review_groups")
        return

    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _group_dict(conn, row) -> dict:
    mr_ids = [
        member["mr_id"]
        for member in conn.execute(
            "SELECT mr_id FROM review_group_mrs WHERE group_id=? ORDER BY mr_id",
            (row["id"],),
        )
    ]
    return {
        "id": row["id"],
        "author_username": row["author_username"],
        "title": row["title"],
        "description": row["description"],
        "provenance": row["provenance"],
        "confidence": row["confidence"],
        "fingerprint": row["fingerprint"],
        "work_item_id": row["work_item_id"],
        "clickup_task_id": row["clickup_task_id"],
        "related_clickup_task_id": row["related_clickup_task_id"],
        "creation_state": row["creation_state"],
        "last_error": row["last_error"],
        "mr_ids": mr_ids,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_group(conn, group_id: int) -> dict:
    row = conn.execute("SELECT * FROM review_groups WHERE id=?", (group_id,)).fetchone()
    if row is None:
        raise ValueError("review group not found")
    return _group_dict(conn, row)


def upsert_group(
    conn,
    *,
    fingerprint: str,
    author_username: str,
    title: str,
    description: str,
    provenance: str,
    confidence: float,
    mr_ids,
    related_clickup_task_id: str | None = None,
) -> dict:
    """Insert or refresh a canonical group and its complete MR membership."""
    members = sorted(set(mr_ids))
    if not members:
        raise ValueError("review group must contain at least one MR")

    timestamp = now_iso()
    with _atomic(conn):
        from . import work_suppression
        work_suppression.require_sources(conn, members, clickup_id=related_clickup_task_id)
        existing = conn.execute('SELECT id FROM review_groups WHERE fingerprint=?', (fingerprint,)).fetchone()
        if existing and work_suppression.group_removed(conn, _get_group(conn, existing['id'])):
            raise ValueError('Review group contains removed work; restore it first.')
        conn.execute(
            "INSERT INTO review_groups "
            "(author_username, title, description, provenance, confidence, fingerprint, "
            "related_clickup_task_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET "
            "author_username=excluded.author_username, title=excluded.title, "
            "description=excluded.description, provenance=excluded.provenance, "
            "confidence=excluded.confidence, "
            "related_clickup_task_id=excluded.related_clickup_task_id, "
            "updated_at=excluded.updated_at",
            (
                author_username,
                title,
                description,
                provenance,
                confidence,
                fingerprint,
                related_clickup_task_id,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM review_groups WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        group_id = row["id"]
        conn.execute("DELETE FROM review_group_mrs WHERE group_id=?", (group_id,))
        conn.executemany(
            "INSERT INTO review_group_mrs (group_id, mr_id, created_at) VALUES (?, ?, ?)",
            ((group_id, mr_id, timestamp) for mr_id in members),
        )
    return _get_group(conn, group_id)


def open_groups(conn, author_username: str | None = None) -> list[dict]:
    """Return persisted canonical groups, optionally scoped to one author."""
    sql = "SELECT * FROM review_groups"
    params = ()
    if author_username is not None:
        sql += " WHERE author_username=?"
        params = (author_username,)
    sql += " ORDER BY id"
    from . import work_suppression
    groups = [_group_dict(conn, row) for row in conn.execute(sql, params)]
    return [group for group in groups if not work_suppression.group_removed(conn, group)]


def claim_creation(conn, group_id: int, *, allow_failed: bool = False) -> bool:
    """Atomically claim one group before crossing the ClickUp write boundary."""
    timestamp = now_iso()
    with _atomic(conn):
        from . import work_suppression
        if work_suppression.group_removed(conn, _get_group(conn, group_id)):
            return False
        if allow_failed:
            cursor = conn.execute(
                "UPDATE review_groups SET creation_state='creating', updated_at=? "
                "WHERE id=? AND creation_state IN ('pending','deferred','disabled','failed')",
                (timestamp, group_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE review_groups SET creation_state='creating', updated_at=? "
                "WHERE id=? AND creation_state IN ('pending','deferred','disabled')",
                (timestamp, group_id),
            )
        claimed = cursor.rowcount == 1
    return claimed


def mark_created(
    conn,
    group_id: int,
    clickup_task_id: str,
    *,
    work_item_id: int | None = None,
) -> dict:
    timestamp = now_iso()
    with _atomic(conn):
        conn.execute(
            "UPDATE review_groups SET clickup_task_id=?, "
            "work_item_id=COALESCE(?, work_item_id), creation_state='created', "
            "last_error=NULL, updated_at=? WHERE id=?",
            (clickup_task_id, work_item_id, timestamp, group_id),
        )
    return _get_group(conn, group_id)


def mark_deferred(conn, group_id: int, error: str | None = None) -> dict:
    timestamp = now_iso()
    with _atomic(conn):
        conn.execute(
            "UPDATE review_groups SET creation_state='deferred', last_error=?, "
            "updated_at=? WHERE id=? "
            "AND creation_state IN ('pending','creating','deferred','disabled')",
            (error, timestamp, group_id),
        )
    return _get_group(conn, group_id)


def mark_uncertain(conn, group_id: int, error: str | None = None) -> dict:
    timestamp = now_iso()
    with _atomic(conn):
        conn.execute(
            "UPDATE review_groups SET creation_state='uncertain', last_error=?, "
            "updated_at=? WHERE id=?",
            (error, timestamp, group_id),
        )
    return _get_group(conn, group_id)


def mark_failed(conn, group_id: int, error: str | None = None) -> dict:
    timestamp = now_iso()
    with _atomic(conn):
        conn.execute(
            "UPDATE review_groups SET creation_state='failed', last_error=?, "
            "updated_at=? WHERE id=? AND creation_state='creating'",
            (error, timestamp, group_id),
        )
    return _get_group(conn, group_id)


def mark_disabled(conn, group_id: int) -> dict:
    timestamp = now_iso()
    with _atomic(conn):
        conn.execute(
            "UPDATE review_groups SET creation_state='disabled', last_error=NULL, "
            "updated_at=? WHERE id=? "
            "AND creation_state IN ('pending','deferred','disabled')",
            (timestamp, group_id),
        )
    return _get_group(conn, group_id)


def recover_creating(conn) -> int:
    """A prior process may have crossed the write boundary; never retry blindly."""
    timestamp = now_iso()
    with _atomic(conn):
        cursor = conn.execute(
            "UPDATE review_groups SET creation_state='uncertain', "
            "last_error='ClickUp creation outcome is uncertain', updated_at=? "
            "WHERE creation_state='creating'",
            (timestamp,),
        )
    return cursor.rowcount


def record_separation(conn, fingerprint: str) -> None:
    with _atomic(conn):
        conn.execute(
            "INSERT OR IGNORE INTO review_group_decisions "
            "(fingerprint, decision, created_at) VALUES (?, 'keep_separate', ?)",
            (fingerprint, now_iso()),
        )


def is_separated(conn, fingerprint: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM review_group_decisions "
        "WHERE fingerprint=? AND decision='keep_separate'",
        (fingerprint,),
    ).fetchone()
    return row is not None


def _work_item_ids_for_mrs(conn, mr_ids) -> list[int]:
    members = list(mr_ids)
    if not members:
        return []
    placeholders = ",".join("?" for _ in members)
    return [
        row["work_item_id"]
        for row in conn.execute(
            "SELECT DISTINCT work_item_id FROM work_links "
            "WHERE source_type='gitlab_mr' AND external_id IN (" + placeholders + ") "
            "ORDER BY work_item_id",
            members,
        )
    ]


def _managed_review_tasks_for_mrs(conn, mr_ids) -> list[dict]:
    members = set(mr_ids)
    matches = []
    for row in conn.execute(
        "SELECT * FROM managed_tasks WHERE category='Review' ORDER BY id"
    ):
        managed_mrs = {row["related_mr_id"]} if row["related_mr_id"] else set()
        try:
            managed_mrs.update(json.loads(row["additional_mr_ids"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        if members.intersection(managed_mrs):
            matches.append(dict(row))
    return matches


def _work_item_for_clickup(conn, clickup_task_id: str) -> int | None:
    row = conn.execute(
        "SELECT work_item_id FROM work_links "
        "WHERE source_type='clickup' AND external_id=?",
        (clickup_task_id,),
    ).fetchone()
    return row["work_item_id"] if row else None


def _draft_duplicate_cleanup(conn, canonical_task_id: str, duplicate_task_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM pending_actions "
        "WHERE kind='clickup_close_task' AND target_id=?",
        (duplicate_task_id,),
    ).fetchone():
        return
    payload = {
        "draft": (
            "Close this task — it duplicates Watson task "
            f"{canonical_task_id}. The local review card has already been merged."
        ),
        "reason": "Duplicate after merge approval",
        "duplicate_of": canonical_task_id,
        "auto_proposed": True,
    }
    action_id = conn.execute(
        "INSERT INTO pending_actions "
        "(capture_id, kind, target_id, payload_json, status, created_at) "
        "VALUES (NULL, 'clickup_close_task', ?, ?, 'pending', ?)",
        (duplicate_task_id, json.dumps(payload), now_iso()),
    ).lastrowid
    events.record(
        conn,
        "review_cleanup",
        f"Duplicate ClickUp review task needs approval to close: {duplicate_task_id}",
        details={"duplicate_of": canonical_task_id},
        action_id=action_id,
        task_id=duplicate_task_id,
    )


def _reconcile_managed_review_tasks(conn, group: dict) -> dict:
    """Adopt one canonical Watson task and gate duplicate closure behind approval."""
    from . import work_suppression
    if work_suppression.group_removed(conn, group):
        return group
    from . import work_items

    managed = _managed_review_tasks_for_mrs(conn, group["mr_ids"])
    if not managed:
        return group
    canonical = managed[0]
    duplicate_tasks = managed[1:]

    work_item_ids = _work_item_ids_for_mrs(conn, group["mr_ids"])
    for task in managed:
        work_item_id = _work_item_for_clickup(conn, task["clickup_task_id"])
        if work_item_id is not None:
            work_item_ids.append(work_item_id)
    if group["work_item_id"] is not None:
        work_item_ids.append(group["work_item_id"])
    work_item_ids = sorted(set(work_item_ids))
    survivor_id = work_item_ids[0] if work_item_ids else None
    if survivor_id is not None:
        duplicates = [item_id for item_id in work_item_ids if item_id != survivor_id]
        if duplicates:
            work_items.merge_items(conn, survivor_id, duplicates)
            events.record(
                conn,
                "review_merge",
                f"Merged local review cards: {group['title']}",
                details={
                    "group_id": group["id"],
                    "merged_card_count": len(duplicates),
                },
            )

    members = sorted(set(group["mr_ids"]))
    primary = canonical["related_mr_id"] if canonical["related_mr_id"] in members else members[0]
    try:
        additional = list(json.loads(canonical["additional_mr_ids"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        additional = []
    additional = list(dict.fromkeys(
        mr_id for mr_id in [*additional, *members] if mr_id and mr_id != primary
    ))
    conn.execute(
        "UPDATE managed_tasks SET related_mr_id=?, additional_mr_ids=? "
        "WHERE id=?",
        (primary, json.dumps(additional), canonical["id"]),
    )
    for duplicate in duplicate_tasks:
        conn.execute(
            "UPDATE managed_tasks SET status='closed' WHERE id=?", (duplicate["id"],)
        )
        _draft_duplicate_cleanup(
            conn, canonical["clickup_task_id"], duplicate["clickup_task_id"]
        )
    conn.execute(
        "UPDATE review_groups SET clickup_task_id=?, work_item_id=?, "
        "creation_state='created', last_error=NULL, updated_at=? WHERE id=?",
        (canonical["clickup_task_id"], survivor_id, now_iso(), group["id"]),
    )
    return _get_group(conn, group["id"])


def _reconcile_all_managed_review_tasks(conn) -> None:
    for group in open_groups(conn):
        _reconcile_managed_review_tasks(conn, group)


def _reconcile_group(conn, planned, *, provenance: str | None = None) -> dict:
    from . import work_items

    existing = None
    if planned.existing_group_id is not None:
        existing = _get_group(conn, planned.existing_group_id)
    else:
        row = conn.execute(
            "SELECT id FROM review_groups WHERE fingerprint=?", (planned.fingerprint,)
        ).fetchone()
        if row:
            existing = _get_group(conn, row["id"])
    members = set(planned.mr_ids)
    if existing:
        members.update(existing["mr_ids"])
    from . import work_suppression
    work_suppression.require_sources(conn, members, item_id=existing['work_item_id'] if existing else None, clickup_id=planned.related_clickup_task_id)
    group = upsert_group(
        conn,
        fingerprint=existing["fingerprint"] if existing else planned.fingerprint,
        author_username=planned.author_username,
        title=planned.title,
        description=planned.description,
        provenance=provenance or planned.provenance,
        confidence=planned.confidence,
        mr_ids=members,
        related_clickup_task_id=(
            planned.related_clickup_task_id
            if planned.related_clickup_task_id is not None
            else existing["related_clickup_task_id"] if existing else None
        ),
    )
    work_item_ids = _work_item_ids_for_mrs(conn, members)
    if existing and existing["work_item_id"] is not None:
        work_item_ids.append(existing["work_item_id"])
    work_item_ids = sorted(set(work_item_ids))
    if work_item_ids:
        survivor_id = (
            existing["work_item_id"]
            if existing and existing["work_item_id"] in work_item_ids
            else work_item_ids[0]
        )
        conn.execute(
            "UPDATE review_groups SET work_item_id=?, updated_at=? WHERE id=?",
            (survivor_id, now_iso(), group["id"]),
        )
        duplicates = [item_id for item_id in work_item_ids if item_id != survivor_id]
        if duplicates:
            work_items.merge_items(conn, survivor_id, duplicates)
            events.record(
                conn,
                "review_merge",
                f"Merged local review cards: {group['title']}",
                details={
                    "group_id": group["id"],
                    "merged_card_count": len(duplicates),
                },
            )
        group = _get_group(conn, group["id"])
    return _reconcile_managed_review_tasks(conn, group)


def _ambiguous_fields(group):
    members = tuple(getattr(group, "member_mr_ids", getattr(group, "mr_ids", ())))
    fingerprint = getattr(group, "fingerprint", None) or "ai:" + "+".join(sorted(members))
    return members, fingerprint


def reconcile_plan(conn, groups, ambiguous) -> dict[str, int]:
    """Atomically persist automatic groups and draft uncertain local decisions."""
    from . import work_suppression
    counts = {"exact_grouped": 0, "ai_grouped": 0, "attached": 0, "ambiguous": 0}
    with _atomic(conn):
        for planned in groups:
            existing = (_get_group(conn, planned.existing_group_id) if planned.existing_group_id else None)
            if existing is None:
                row = conn.execute('SELECT id FROM review_groups WHERE fingerprint=?', (planned.fingerprint,)).fetchone()
                existing = _get_group(conn, row['id']) if row else None
            if work_suppression.group_removed(conn, {
                'mr_ids': planned.mr_ids, 'related_clickup_task_id': planned.related_clickup_task_id,
            }) or (existing and work_suppression.group_removed(conn, existing)):
                continue
            if planned.provenance == "exact":
                grouping_subject = f"Grouped review MRs using exact signals: {planned.title}"
            elif planned.provenance == "ai":
                grouping_subject = f"Grouped review MRs using AI: {planned.title}"
            else:
                grouping_subject = f"Kept review MR as its own group: {planned.title}"
            events.record(
                conn,
                "review_grouping",
                grouping_subject,
                details={
                    "provenance": planned.provenance,
                    "mr_count": len(planned.mr_ids),
                },
            )
            if planned.existing_group_id is not None:
                events.record(
                    conn,
                    "review_attachment",
                    f"Attached review MRs to existing group: {planned.title}",
                    details={
                        "group_id": planned.existing_group_id,
                        "mr_count": len(planned.mr_ids),
                    },
                )
            _reconcile_group(conn, planned)
            key = "exact_grouped" if planned.provenance == "exact" else "ai_grouped"
            counts[key] += 1
            counts["attached"] += int(planned.existing_group_id is not None)

        for suggestion in ambiguous:
            members, fingerprint = _ambiguous_fields(suggestion)
            if any(work_suppression.mr_removed(conn, mr_id) for mr_id in members):
                continue
            if is_separated(conn, fingerprint):
                continue
            exists = conn.execute(
                "SELECT 1 FROM pending_actions WHERE kind='group_review_mrs' AND target_id=?",
                (fingerprint,),
            ).fetchone()
            if exists:
                continue
            work_item_ids = _work_item_ids_for_mrs(conn, members)
            payload = {
                "kind": "group_review_mrs",
                "fingerprint": fingerprint,
                "member_mr_ids": list(members),
                "candidate_work_item_ids": work_item_ids,
                "suggested_title": suggestion.title,
                "reasoning": getattr(suggestion, "reasoning", ""),
                "confidence": suggestion.confidence,
                "auto_proposed": True,
            }
            action_id = conn.execute(
                "INSERT INTO pending_actions "
                "(capture_id, kind, target_id, payload_json, status, created_at) "
                "VALUES (NULL, 'group_review_mrs', ?, ?, 'pending', ?)",
                (fingerprint, json.dumps(payload), now_iso()),
            ).lastrowid
            events.record(
                conn,
                "review_ambiguity",
                f"Review grouping needs a decision: {suggestion.title}",
                details={"mr_count": len(members)},
                action_id=action_id,
            )
            counts["ambiguous"] += 1
    return counts


def apply_grouping_action(conn, payload: dict) -> dict:
    """Apply an approved ambiguous merge as a user-provenance local group."""
    from .review_automation import PlannedReviewGroup

    members = tuple(payload["member_mr_ids"])
    placeholders = ",".join("?" for _ in members)
    author_rows = conn.execute(
        "SELECT DISTINCT COALESCE(NULLIF(author_username, ''), author) AS author_username "
        "FROM gitlab_mrs_cache WHERE mr_id IN (" + placeholders + ")",
        members,
    ).fetchall()
    authors = {row["author_username"] or "" for row in author_rows}
    if len(authors) > 1:
        raise ValueError("ambiguous AI group crosses authors")
    planned = PlannedReviewGroup(
        fingerprint=payload["fingerprint"],
        author_username=next(iter(authors), ""),
        title=payload["suggested_title"],
        description=payload.get("reasoning", ""),
        provenance="user",
        confidence=float(payload["confidence"]),
        mr_ids=members,
    )
    with _atomic(conn):
        return _reconcile_group(conn, planned, provenance="user")
