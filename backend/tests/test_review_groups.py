import json
import sqlite3

import pytest

from app import db
from app.services import review_automation, review_groups, review_semantic_grouper, work_items


def _upsert(conn, *, fingerprint="exact:clickup:86abc", mr_ids=None, **overrides):
    values = {
        "author_username": "lee.dev",
        "title": "Review one",
        "description": "Review the shared change",
        "provenance": "exact",
        "confidence": 1.0,
        "related_clickup_task_id": "86abc",
    }
    values.update(overrides)
    return review_groups.upsert_group(
        conn,
        fingerprint=fingerprint,
        mr_ids=mr_ids or ["1!10"],
        **values,
    )


def test_review_group_schema_is_idempotent(conn):
    db.init_db()
    db.init_db()
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"review_groups", "review_group_mrs", "review_group_decisions"} <= tables

    indexes = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {
        "idx_review_groups_creation_state",
        "idx_review_groups_author_username",
        "idx_review_group_mrs_group_id",
    } <= indexes


def test_review_group_migration_creates_tables_before_indexes(tmp_path):
    legacy = sqlite3.connect(tmp_path / "legacy.db")
    legacy.row_factory = sqlite3.Row
    legacy.executescript(db.SCHEMA)
    legacy.execute("DROP TABLE IF EXISTS review_group_mrs")
    legacy.execute("DROP TABLE IF EXISTS review_group_decisions")
    legacy.execute("DROP TABLE IF EXISTS review_groups")

    db._migrate(legacy)
    db._migrate(legacy)

    tables = {
        row["name"]
        for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        row["name"]
        for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"review_groups", "review_group_mrs", "review_group_decisions"} <= tables
    assert {
        "idx_review_groups_creation_state",
        "idx_review_groups_author_username",
        "idx_review_group_mrs_group_id",
    } <= indexes
    legacy.close()


def test_upsert_returns_group_and_replaces_members_without_resetting_creation(conn):
    group = _upsert(conn, mr_ids=["1!10", "2!20"])
    assert group == {
        "id": group["id"],
        "author_username": "lee.dev",
        "title": "Review one",
        "description": "Review the shared change",
        "provenance": "exact",
        "confidence": 1.0,
        "fingerprint": "exact:clickup:86abc",
        "work_item_id": None,
        "clickup_task_id": None,
        "related_clickup_task_id": "86abc",
        "creation_state": "pending",
        "last_error": None,
        "mr_ids": ["1!10", "2!20"],
        "created_at": group["created_at"],
        "updated_at": group["updated_at"],
    }

    cursor = conn.execute(
        "INSERT INTO work_items "
        "(title, origin, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("Review one", "discovery", "2026-09-08", "2026-09-08"),
    )
    work_item_id = cursor.lastrowid
    conn.commit()
    review_groups.mark_created(
        conn, group["id"], "CU-1", work_item_id=work_item_id
    )
    updated = _upsert(
        conn,
        mr_ids=["2!20", "3!30"],
        title="Updated review",
        confidence=0.97,
    )

    assert updated["id"] == group["id"]
    assert updated["title"] == "Updated review"
    assert updated["confidence"] == 0.97
    assert updated["mr_ids"] == ["2!20", "3!30"]
    assert updated["clickup_task_id"] == "CU-1"
    assert updated["work_item_id"] == work_item_id
    assert updated["creation_state"] == "created"


def test_group_membership_is_unique_across_groups_and_rolls_back_insert(conn):
    first = _upsert(conn)

    with pytest.raises(sqlite3.IntegrityError):
        _upsert(
            conn,
            fingerprint="ai:second",
            provenance="ai",
            confidence=0.91,
            mr_ids=["1!10"],
        )

    assert first["creation_state"] == "pending"
    assert conn.execute(
        "SELECT COUNT(*) FROM review_groups WHERE fingerprint='ai:second'"
    ).fetchone()[0] == 0


def test_conflicting_membership_update_rolls_back_metadata_and_members(conn):
    original = _upsert(
        conn,
        title="Original review",
        description="Original description",
        confidence=0.91,
        mr_ids=["1!10", "2!20"],
    )
    _upsert(
        conn,
        fingerprint="singleton:3!30",
        author_username="other.user",
        title="Other review",
        description="Other description",
        provenance="singleton",
        confidence=1.0,
        related_clickup_task_id=None,
        mr_ids=["3!30"],
    )

    with pytest.raises(sqlite3.IntegrityError):
        _upsert(
            conn,
            title="Replacement review",
            description="Replacement description",
            confidence=0.99,
            mr_ids=["2!20", "3!30"],
        )

    restored = review_groups._get_group(conn, original["id"])
    assert restored["title"] == "Original review"
    assert restored["description"] == "Original description"
    assert restored["confidence"] == 0.91
    assert restored["mr_ids"] == ["1!10", "2!20"]


def test_open_groups_can_filter_by_author_and_orders_members(conn):
    first = _upsert(conn, mr_ids=["2!20", "1!10"])
    second = _upsert(
        conn,
        fingerprint="singleton:3!30",
        author_username="other.user",
        provenance="singleton",
        related_clickup_task_id=None,
        mr_ids=["3!30"],
    )

    assert [group["id"] for group in review_groups.open_groups(conn)] == [
        first["id"],
        second["id"],
    ]
    filtered = review_groups.open_groups(conn, "lee.dev")
    assert [group["id"] for group in filtered] == [first["id"]]
    assert filtered[0]["mr_ids"] == ["1!10", "2!20"]


@pytest.mark.parametrize("state", ["pending", "deferred", "disabled"])
def test_normal_creation_claim_accepts_retryable_states(conn, state):
    group = _upsert(conn, fingerprint=f"singleton:{state}")
    conn.execute(
        "UPDATE review_groups SET creation_state=? WHERE id=?", (state, group["id"])
    )
    conn.commit()

    assert review_groups.claim_creation(conn, group["id"]) is True
    assert conn.execute(
        "SELECT creation_state FROM review_groups WHERE id=?", (group["id"],)
    ).fetchone()[0] == "creating"
    assert review_groups.claim_creation(conn, group["id"]) is False


def test_failed_creation_requires_explicit_manual_retry(conn):
    group = _upsert(conn)
    conn.execute(
        "UPDATE review_groups SET creation_state='failed' WHERE id=?", (group["id"],)
    )
    conn.commit()

    assert review_groups.claim_creation(conn, group["id"]) is False
    assert review_groups.claim_creation(conn, group["id"], allow_failed=True) is True


def test_creation_markers_preserve_terminal_uncertainty(conn):
    group = _upsert(conn)
    review_groups.mark_deferred(conn, group["id"], "ClickUp unavailable")
    deferred = review_groups.open_groups(conn)[0]
    assert deferred["creation_state"] == "deferred"
    assert deferred["last_error"] == "ClickUp unavailable"

    assert review_groups.claim_creation(conn, group["id"])
    review_groups.mark_uncertain(conn, group["id"], "response lost")
    uncertain = review_groups.open_groups(conn)[0]
    assert uncertain["creation_state"] == "uncertain"
    assert uncertain["last_error"] == "response lost"

    review_groups.mark_deferred(conn, group["id"], "late preflight failure")
    still_uncertain = review_groups.open_groups(conn)[0]
    assert still_uncertain["creation_state"] == "uncertain"
    assert still_uncertain["last_error"] == "response lost"
    assert review_groups.claim_creation(conn, group["id"], allow_failed=True) is False


def test_separation_decision_is_sticky_and_idempotent(conn):
    assert review_groups.is_separated(conn, "ai:one+two") is False

    review_groups.record_separation(conn, "ai:one+two")
    review_groups.record_separation(conn, "ai:one+two")

    assert review_groups.is_separated(conn, "ai:one+two") is True
    assert conn.execute(
        "SELECT COUNT(*) FROM review_group_decisions WHERE fingerprint='ai:one+two'"
    ).fetchone()[0] == 1


def _seed_merge_graph(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    survivor = work_items.create_work_item(conn, title="My manual title", owner_person_id=owner)
    duplicate = work_items.create_work_item(
        conn, title="Generated review", owner_person_id=owner, origin="discovery"
    )
    for item_id, source_type, external_id in (
        (survivor["id"], "gitlab_mr", "1!10"),
        (survivor["id"], "clickup", "cu-one"),
        (duplicate["id"], "gitlab_mr", "2!20"),
        (duplicate["id"], "clickup", "cu-two"),
    ):
        work_items.add_work_link(conn, item_id, source_type=source_type, external_id=external_id)
    capture_id = conn.execute(
        "INSERT INTO captures (raw_text, created_at, work_item_id) VALUES ('capture', '2026-09-08', ?)",
        (duplicate["id"],),
    ).lastrowid
    entry_id = conn.execute(
        "INSERT INTO entries (work_item_id, type, title, people, tags, created_at) "
        "VALUES (?, 'note', 'entry', '[]', '[]', '2026-09-08')", (duplicate["id"],)
    ).lastrowid
    reminder_id = conn.execute(
        "INSERT INTO reminders (work_item_id, text, due_at, created_at) "
        "VALUES (?, 'reminder', '2026-09-09', '2026-09-08')", (duplicate["id"],)
    ).lastrowid
    activity_id = conn.execute(
        "INSERT INTO work_activity (work_item_id, activity_type, created_at) "
        "VALUES (?, 'note', '2026-09-08')", (duplicate["id"],)
    ).lastrowid
    inbox_id = conn.execute(
        "INSERT INTO work_inbox (capture_id, suggested_work_item_id, created_at) "
        "VALUES (?, ?, '2026-09-08')", (capture_id, duplicate["id"])
    ).lastrowid
    group = _upsert(
        conn, fingerprint="ai:duplicate", provenance="ai",
        related_clickup_task_id=None, mr_ids=["2!20"]
    )
    conn.execute(
        "UPDATE review_groups SET work_item_id=? WHERE id=?", (duplicate["id"], group["id"])
    )
    conn.commit()
    return survivor, duplicate, {
        "capture": capture_id, "entry": entry_id, "reminder": reminder_id,
        "activity": activity_id, "inbox": inbox_id, "group": group["id"],
    }


def test_merge_items_preserves_all_associations_and_manual_title(conn):
    survivor, duplicate, ids = _seed_merge_graph(conn)

    merged = work_items.merge_items(conn, survivor["id"], [duplicate["id"]])

    assert merged["id"] == survivor["id"]
    assert merged["title"] == "My manual title"
    assert merged["priority_position"] == 1000
    assert {row["external_id"] for row in conn.execute(
        "SELECT external_id FROM work_links WHERE work_item_id=?", (survivor["id"],)
    )} == {"1!10", "2!20", "cu-one", "cu-two"}
    for table, column, row_id in (
        ("captures", "work_item_id", ids["capture"]),
        ("entries", "work_item_id", ids["entry"]),
        ("reminders", "work_item_id", ids["reminder"]),
        ("work_activity", "work_item_id", ids["activity"]),
        ("work_inbox", "suggested_work_item_id", ids["inbox"]),
        ("review_groups", "work_item_id", ids["group"]),
    ):
        assert conn.execute(f"SELECT {column} FROM {table} WHERE id=?", (row_id,)).fetchone()[0] == survivor["id"]
    assert conn.execute(
        "SELECT origin FROM work_items WHERE id=?", (duplicate["id"],)
    ).fetchone()[0] == "ignored"


def test_merge_items_rolls_back_every_association_on_midway_integrity_error(conn):
    survivor, duplicate, ids = _seed_merge_graph(conn)
    conn.execute(
        "CREATE TRIGGER fail_entry_merge BEFORE UPDATE OF work_item_id ON entries "
        "WHEN OLD.work_item_id != NEW.work_item_id BEGIN "
        "SELECT RAISE(ABORT, 'forced merge failure'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced merge failure"):
        work_items.merge_items(conn, survivor["id"], [duplicate["id"]])

    assert conn.execute("SELECT work_item_id FROM captures WHERE id=?", (ids["capture"],)).fetchone()[0] == duplicate["id"]
    assert conn.execute("SELECT work_item_id FROM entries WHERE id=?", (ids["entry"],)).fetchone()[0] == duplicate["id"]
    assert conn.execute("SELECT work_item_id FROM work_links WHERE external_id='2!20'").fetchone()[0] == duplicate["id"]
    assert conn.execute("SELECT origin FROM work_items WHERE id=?", (duplicate["id"],)).fetchone()[0] == "discovery"


def _planned(*, confidence=0.93):
    return review_automation.PlannedReviewGroup(
        fingerprint="ai:1!10+2!20", author_username="lee.dev",
        title="Keyword TTL review", description="One rollout", provenance="ai",
        confidence=confidence, mr_ids=("1!10", "2!20"),
    )


def test_reconcile_plan_automatically_merges_cards_for_confident_group(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    first = work_items.create_work_item(conn, title="First", owner_person_id=owner, origin="discovery")
    second = work_items.create_work_item(conn, title="Second", owner_person_id=owner, origin="discovery")
    work_items.add_work_link(conn, first["id"], source_type="gitlab_mr", external_id="1!10")
    work_items.add_work_link(conn, second["id"], source_type="gitlab_mr", external_id="2!20")

    counts = review_groups.reconcile_plan(conn, [_planned()], [])

    group = review_groups.open_groups(conn)[0]
    assert counts == {"exact_grouped": 0, "ai_grouped": 1, "attached": 0, "ambiguous": 0}
    assert group["work_item_id"] == first["id"]
    assert conn.execute("SELECT origin FROM work_items WHERE id=?", (second["id"],)).fetchone()[0] == "ignored"


def test_reconcile_plan_logs_exact_ai_attachment_merge_and_ambiguity_outcomes(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    first = work_items.create_work_item(
        conn, title="Existing card", owner_person_id=owner, origin="discovery"
    )
    second = work_items.create_work_item(
        conn, title="New card", owner_person_id=owner, origin="discovery"
    )
    work_items.add_work_link(
        conn, first["id"], source_type="gitlab_mr", external_id="1!10"
    )
    work_items.add_work_link(
        conn, second["id"], source_type="gitlab_mr", external_id="2!20"
    )
    existing = review_groups.upsert_group(
        conn,
        fingerprint="ai:existing",
        author_username="lee.dev",
        title="Existing review",
        description="Existing context",
        provenance="ai",
        confidence=0.9,
        mr_ids=["1!10"],
    )
    conn.execute(
        "UPDATE review_groups SET work_item_id=? WHERE id=?",
        (first["id"], existing["id"]),
    )
    exact = review_automation.PlannedReviewGroup(
        fingerprint="exact:clickup:86exact",
        author_username="exact.user",
        title="Exact review",
        description="Safe",
        provenance="exact",
        confidence=1.0,
        mr_ids=("3!30",),
        related_clickup_task_id="86exact",
    )
    attached = review_automation.PlannedReviewGroup(
        fingerprint="ignored-for-existing",
        author_username="lee.dev",
        title="Existing review",
        description="Authorization: Bearer secret-provider-body",
        provenance="ai",
        confidence=0.94,
        mr_ids=("2!20",),
        existing_group_id=existing["id"],
        reasoning="raw prompt secret-provider-body",
    )
    ambiguous = review_semantic_grouper.SemanticGroup(
        member_mr_ids=("4!40", "5!50"),
        existing_group_id=None,
        title="Ambiguous review",
        reasoning="secret-provider-body",
        confidence=0.7,
    )

    review_groups.reconcile_plan(conn, [exact, attached], [ambiguous])

    rows = conn.execute(
        "SELECT kind, subject, details_json FROM system_events "
        "WHERE kind LIKE 'review_%' ORDER BY id"
    ).fetchall()
    kinds = [row["kind"] for row in rows]
    assert kinds == [
        "review_grouping",
        "review_grouping",
        "review_attachment",
        "review_merge",
        "review_ambiguity",
    ]
    assert "Exact review" in rows[0]["subject"]
    assert "Existing review" in rows[2]["subject"]
    assert "Ambiguous review" in rows[-1]["subject"]
    serialized = json.dumps([dict(row) for row in rows])
    assert "secret-provider-body" not in serialized
    assert "Authorization" not in serialized


def test_deterministic_singleton_log_does_not_claim_ai_grouping(conn):
    singleton = review_automation.PlannedReviewGroup(
        fingerprint="singleton:1!10",
        author_username="lee.dev",
        title="Standalone review",
        description="",
        provenance="singleton",
        confidence=1.0,
        mr_ids=("1!10",),
    )

    review_groups.reconcile_plan(conn, [singleton], [])

    row = conn.execute(
        "SELECT subject FROM system_events WHERE kind='review_grouping'"
    ).fetchone()
    assert row["subject"] == "Kept review MR as its own group: Standalone review"


def test_reconcile_plan_canonicalizes_duplicate_review_tasks_without_closing_clickup(
    conn,
):
    owner = work_items.ensure_self_person(conn, "Taylor")
    first = work_items.create_work_item(
        conn, title="First review", owner_person_id=owner, origin="discovery"
    )
    second = work_items.create_work_item(
        conn, title="Second review", owner_person_id=owner, origin="discovery"
    )
    for work_item_id, source_type, external_id in (
        (first["id"], "gitlab_mr", "1!10"),
        (first["id"], "clickup", "CU-FIRST"),
        (second["id"], "gitlab_mr", "2!20"),
        (second["id"], "clickup", "CU-SECOND"),
    ):
        work_items.add_work_link(
            conn,
            work_item_id,
            source_type=source_type,
            external_id=external_id,
        )
    first_managed = conn.execute(
        "INSERT INTO managed_tasks "
        "(clickup_task_id, related_mr_id, additional_mr_ids, category, status, created_at) "
        "VALUES ('CU-FIRST', '1!10', '[]', 'Review', 'open', '2026-09-01')"
    ).lastrowid
    second_managed = conn.execute(
        "INSERT INTO managed_tasks "
        "(clickup_task_id, related_mr_id, additional_mr_ids, category, status, created_at) "
        "VALUES ('CU-SECOND', '2!20', '[]', 'Review', 'open', '2026-09-02')"
    ).lastrowid
    conn.commit()

    first_counts = review_groups.reconcile_plan(conn, [_planned()], [])
    second_counts = review_groups.reconcile_plan(conn, [_planned()], [])

    assert first_counts["ai_grouped"] == 1
    assert second_counts["ai_grouped"] == 1
    group = review_groups.open_groups(conn)[0]
    assert group["clickup_task_id"] == "CU-FIRST"
    assert group["creation_state"] == "created"
    assert group["work_item_id"] == first["id"]
    assert conn.execute(
        "SELECT additional_mr_ids FROM managed_tasks WHERE id=?", (first_managed,)
    ).fetchone()[0] == '["2!20"]'
    assert conn.execute(
        "SELECT status FROM managed_tasks WHERE id=?", (second_managed,)
    ).fetchone()[0] == "closed"
    assert conn.execute(
        "SELECT origin FROM work_items WHERE id=?", (second["id"],)
    ).fetchone()[0] == "ignored"
    links = conn.execute(
        "SELECT source_type, external_id FROM work_links WHERE work_item_id=? "
        "ORDER BY source_type, external_id",
        (first["id"],),
    ).fetchall()
    assert [tuple(row) for row in links] == [
        ("clickup", "CU-FIRST"),
        ("clickup", "CU-SECOND"),
        ("gitlab_mr", "1!10"),
        ("gitlab_mr", "2!20"),
    ]
    cleanup = conn.execute(
        "SELECT kind, target_id, payload_json, status FROM pending_actions "
        "WHERE kind='clickup_close_task'"
    ).fetchall()
    assert len(cleanup) == 1
    assert cleanup[0]["target_id"] == "CU-SECOND"
    assert cleanup[0]["status"] == "pending"
    assert json.loads(cleanup[0]["payload_json"])["duplicate_of"] == "CU-FIRST"


def test_reconcile_plan_deduplicates_ambiguous_pending_action(conn):
    ambiguous = review_semantic_grouper.SemanticGroup(
        member_mr_ids=("1!10", "2!20"), existing_group_id=None,
        title="Keyword TTL review", reasoning="Possibly one rollout", confidence=0.84,
    )

    first = review_groups.reconcile_plan(conn, [], [ambiguous])
    second = review_groups.reconcile_plan(conn, [], [ambiguous])

    assert first["ambiguous"] == 1
    assert second["ambiguous"] == 0
    row = conn.execute("SELECT kind, payload_json FROM pending_actions").fetchone()
    assert row["kind"] == "group_review_mrs"
    assert json.loads(row["payload_json"]) == {
        "kind": "group_review_mrs", "fingerprint": "ai:1!10+2!20",
        "member_mr_ids": ["1!10", "2!20"], "candidate_work_item_ids": [],
        "suggested_title": "Keyword TTL review", "reasoning": "Possibly one rollout",
        "confidence": 0.84, "auto_proposed": True,
    }
