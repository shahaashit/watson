import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.models import work_item_dict
from app.services import work_items


def test_work_schema_is_idempotent(conn):
    db.init_db()
    db.init_db()
    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"people", "person_identities", "work_items", "work_links",
            "work_activity", "work_inbox", "review_groups",
            "review_group_mrs", "review_group_decisions"} <= tables
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(work_items)")
    }
    assert "priority_position" in columns


def test_reminder_capture_migration_is_guarded_and_idempotent(tmp_path):
    """A pre-capture_id reminders table must gain the column and index once."""
    legacy = sqlite3.connect(tmp_path / "legacy.db")
    legacy.row_factory = sqlite3.Row
    legacy.executescript(db.SCHEMA)
    legacy.execute("DROP TABLE reminders")
    legacy.execute(
        "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "entry_id INTEGER NULL, text TEXT NOT NULL, due_at TIMESTAMP NOT NULL, "
        "status TEXT DEFAULT 'pending', snoozed_until TIMESTAMP NULL, created_at TIMESTAMP)"
    )
    db._migrate(legacy)
    db._migrate(legacy)

    columns = {row["name"] for row in legacy.execute("PRAGMA table_info(reminders)")}
    indexes = {row["name"] for row in legacy.execute("PRAGMA index_list(reminders)")}
    assert "capture_id" in columns
    assert "idx_reminders_capture" in indexes
    legacy.close()


def test_init_db_migrates_legacy_reminders_before_creating_capture_index(tmp_path, monkeypatch):
    """Putting this index in SCHEMA would fail before the guarded column add runs."""
    from app.config import settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(settings, "watson_data_dir", str(data_dir))
    monkeypatch.setattr(settings, "watson_backup_dir", str(tmp_path / "backups"))
    legacy = sqlite3.connect(data_dir / "watson.db")
    legacy.execute(
        "CREATE TABLE captures (id INTEGER PRIMARY KEY, raw_text TEXT, created_at TEXT, "
        "classified_at TEXT, classification_json TEXT)"
    )
    legacy.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, capture_id INTEGER, type TEXT, title TEXT, "
        "body TEXT, people TEXT, tags TEXT, created_at TEXT)"
    )
    legacy.execute(
        "CREATE TABLE reminders (id INTEGER PRIMARY KEY, entry_id INTEGER, text TEXT, due_at TEXT, "
        "status TEXT, snoozed_until TEXT, created_at TEXT)"
    )
    legacy.commit()
    legacy.close()

    db.init_db()
    migrated = sqlite3.connect(data_dir / "watson.db")
    try:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(reminders)")}
        indexes = {row[1] for row in migrated.execute("PRAGMA index_list(reminders)")}
        assert "capture_id" in columns
        assert "idx_reminders_capture" in indexes
    finally:
        migrated.close()


def test_work_item_serializer_exposes_local_fields(conn):
    conn.execute(
        "INSERT INTO work_items (title, state, position, origin, created_at, updated_at)"
        " VALUES ('Investigate pixels', 'next', 1000, 'manual', '2026-08-27', '2026-08-27')"
    )
    row = conn.execute("SELECT * FROM work_items").fetchone()
    assert work_item_dict(row) == {
        "id": row["id"], "title": "Investigate pixels", "description": "",
        "title_is_manual": True,
        "state": "next", "owner_person_id": None, "owner_display": "",
        "position": 1000, "priority_position": 1000,
        "origin": "manual", "created_at": "2026-08-27",
        "updated_at": "2026-08-27", "completed_at": None,
    }


def test_first_work_migration_creates_backup_without_changing_rows(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "watson_backup_dir", str(tmp_path))
    old = sqlite3.connect(tmp_path / "old.db")
    old.row_factory = sqlite3.Row
    old.execute("CREATE TABLE captures (id INTEGER PRIMARY KEY, raw_text TEXT, created_at TEXT)")
    old.execute("INSERT INTO captures (raw_text, created_at) VALUES ('keep me', '2026-08-27')")
    old.commit()
    backup = db.backup_before_work_migration(old)
    assert backup and backup.exists()
    assert old.execute("SELECT raw_text FROM captures").fetchone()["raw_text"] == "keep me"
    old.close()


def test_create_defaults_to_bottom_of_next(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    first = work_items.create_work_item(conn, title="First", owner_person_id=owner)
    second = work_items.create_work_item(conn, title="Second", owner_person_id=owner)

    assert first["state"] == "next"
    assert second["position"] > first["position"]


def test_priority_is_global_across_an_owner_states(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    first = work_items.create_work_item(
        conn, title="Today", state="today", owner_person_id=owner
    )
    second = work_items.create_work_item(
        conn, title="Next", state="next", owner_person_id=owner
    )

    assert second["priority_position"] > first["priority_position"]


def test_board_views_hide_historical_done_and_flat_mode_has_one_active_order(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    first = work_items.create_work_item(
        conn, title="First", state="today", owner_person_id=owner
    )
    second = work_items.create_work_item(
        conn, title="Second", state="waiting", owner_person_id=owner
    )
    done_today = work_items.create_work_item(
        conn, title="Done today", state="done", owner_person_id=owner
    )
    old_done = work_items.create_work_item(
        conn, title="Old done", state="done", owner_person_id=owner
    )
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    conn.execute(
        "UPDATE work_items SET completed_at=? WHERE id=?",
        ((now - timedelta(days=2)).isoformat(), old_done["id"]),
    )

    flat = work_items.my_work(
        conn, owner, separate_by_status=False, timezone_name="Asia/Kolkata"
    )
    assert flat["mode"] == "flat"
    assert [item["id"] for item in flat["items"]] == [first["id"], second["id"]]

    segregated = work_items.my_work(
        conn, owner, separate_by_status=True, timezone_name="Asia/Kolkata"
    )
    assert segregated["mode"] == "segregated"
    assert [item["id"] for item in segregated["done"]] == [done_today["id"]]


def test_discovered_titles_are_not_marked_manual(conn):
    discovered = work_items.create_work_item(conn, title="MR title", origin="discovery")
    manual = work_items.create_work_item(conn, title="My title")
    assert discovered["title_is_manual"] is False
    assert manual["title_is_manual"] is True


def test_move_reorders_without_duplicate_positions(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    work_items.create_work_item(conn, title="A", owner_person_id=owner)
    b = work_items.create_work_item(conn, title="B", owner_person_id=owner)
    c = work_items.create_work_item(conn, title="C", owner_person_id=owner)

    work_items.move_work_item(conn, c["id"], state="today")
    work_items.move_work_item(conn, b["id"], state="today", before_id=c["id"])

    rows = conn.execute(
        "SELECT title, position FROM work_items WHERE state='today' ORDER BY position"
    ).fetchall()
    assert [row["title"] for row in rows] == ["B", "C"]
    assert [row["position"] for row in rows] == [1000, 2000]

    work_items.move_work_item(conn, c["id"], state="today", before_id=b["id"])
    rows = conn.execute(
        "SELECT title, position FROM work_items WHERE state='today' ORDER BY position"
    ).fetchall()
    assert [row["title"] for row in rows] == ["C", "B"]
    assert [row["position"] for row in rows] == [1000, 2000]


def test_move_uses_global_owner_priority_across_states(conn):
    owner = work_items.ensure_self_person(conn, "Taylor")
    today = work_items.create_work_item(
        conn, title="Today", state="today", owner_person_id=owner
    )
    next_item = work_items.create_work_item(
        conn, title="Next", state="next", owner_person_id=owner
    )
    waiting = work_items.create_work_item(
        conn, title="Waiting", state="waiting", owner_person_id=owner
    )

    reordered = work_items.move_work_item(
        conn, waiting["id"], state="waiting", before_id=today["id"]
    )
    assert reordered["state"] == "waiting"
    rows = conn.execute(
        "SELECT id FROM work_items WHERE owner_person_id=? ORDER BY priority_position",
        (owner,),
    ).fetchall()
    assert [row["id"] for row in rows] == [waiting["id"], today["id"], next_item["id"]]

    moved_state = work_items.move_work_item(
        conn, next_item["id"], state="today", before_id=today["id"]
    )
    assert moved_state["state"] == "today"
    current_today = work_items.get_work_detail(conn, today["id"])
    assert moved_state["priority_position"] < current_today["priority_position"]

def test_link_is_globally_idempotent_and_never_relinks(conn):
    first = work_items.create_work_item(conn, title="MR work")
    second = work_items.create_work_item(conn, title="Different work")

    one = work_items.add_work_link(
        conn, first["id"], source_type="gitlab_mr", external_id="7!42"
    )
    two = work_items.add_work_link(
        conn, first["id"], source_type="gitlab_mr", external_id="7!42"
    )

    assert one["id"] == two["id"]
    with pytest.raises(ValueError, match="already linked"):
        work_items.add_work_link(
            conn, second["id"], source_type="gitlab_mr", external_id="7!42"
        )


def test_create_and_update_validate_owners_and_change_lanes_at_bottom(conn):
    first_owner = work_items.ensure_self_person(conn, "Taylor")
    second_owner = conn.execute(
        "INSERT INTO people (display_name, is_tracked, created_at, updated_at)"
        " VALUES ('Morgan', 1, 'now', 'now')"
    ).lastrowid
    existing = work_items.create_work_item(conn, title="Existing", owner_person_id=second_owner)
    item = work_items.create_work_item(conn, title="Move me", owner_person_id=first_owner)

    with pytest.raises(ValueError, match="owner"):
        work_items.create_work_item(conn, title="Invalid", owner_person_id=99999)

    updated = work_items.update_work_item(conn, item["id"], title="Manually named", owner_person_id=second_owner)
    assert updated["title_is_manual"] is True
    assert updated["owner_person_id"] == second_owner
    assert updated["position"] > existing["position"]


def test_owner_display_is_canonical_for_tracked_and_explicitly_cleared_owners(conn):
    tracked = conn.execute(
        "INSERT INTO people (display_name, is_tracked, created_at, updated_at)"
        " VALUES ('Morgan', 1, 'now', 'now')"
    ).lastrowid
    existing = work_items.create_work_item(
        conn, title="Tracked first", owner_person_id=tracked, owner_display="stale display"
    )
    fallback = work_items.create_work_item(conn, title="Fallback", owner_display="Morgan")

    assigned = work_items.update_work_item(conn, fallback["id"], owner_person_id=tracked)
    assert existing["owner_display"] == ""
    assert assigned["owner_display"] == ""
    rows = conn.execute(
        "SELECT id, position FROM work_items WHERE owner_person_id=? AND state='next' ORDER BY position",
        (tracked,),
    ).fetchall()
    assert [row["id"] for row in rows] == [existing["id"], assigned["id"]]
    assert [row["position"] for row in rows] == [1000, 2000]

    cleared = work_items.update_work_item(conn, fallback["id"], owner_person_id=None, owner_display=None)
    assert cleared["owner_person_id"] is None
    assert cleared["owner_display"] == ""


def test_move_sets_and_clears_completed_at_and_rejects_bad_placement(conn):
    item = work_items.create_work_item(conn, title="Finish me")
    done = work_items.move_work_item(conn, item["id"], state="done")
    assert done["completed_at"]
    reopened = work_items.move_work_item(conn, item["id"], state="next")
    assert reopened["completed_at"] is None

    with pytest.raises(ValueError, match="state"):
        work_items.move_work_item(conn, item["id"], state="later")
    with pytest.raises(ValueError, match="before_id and after_id"):
        work_items.move_work_item(conn, item["id"], state="next", before_id=item["id"], after_id=item["id"])


def test_detail_and_work_views_return_local_ordered_data(conn):
    self_person = work_items.ensure_self_person(conn, "Taylor")
    tracked = conn.execute(
        "INSERT INTO people (display_name, is_tracked, lane_position, created_at, updated_at)"
        " VALUES ('Morgan', 1, 20, 'now', 'now')"
    ).lastrowid
    mine = work_items.create_work_item(conn, title="Mine", owner_person_id=self_person)
    work_items.add_work_link(conn, mine["id"], source_type="url", external_id="https://example.test", label="Source")
    work_items.add_activity(conn, mine["id"], activity_type="note", body="First")
    work_items.add_activity(conn, mine["id"], activity_type="note", body="Second")
    work_items.create_work_item(conn, title="Tracked", owner_person_id=tracked)
    work_items.create_work_item(conn, title="Other", owner_display="New person")
    work_items.create_work_item(conn, title="Unassigned")

    detail = work_items.get_work_detail(conn, mine["id"])
    assert detail and detail["links"][0]["label"] == "Source"
    assert [entry["body"] for entry in detail["activity"]] == ["Second", "First"]
    assert detail["reminders"] == []
    assert detail["pending_actions"] == []
    assert [item["id"] for item in work_items.my_work(conn, self_person)["next"]] == [mine["id"]]

    team = work_items.team_work(conn)
    assert [lane["name"] for lane in team["lanes"]] == ["Morgan", "Others", "Unassigned"]
    assert team["lanes"][0]["items"][0]["title"] == "Tracked"


def test_team_special_lanes_hide_unrelated_discovery_but_keep_relevant_and_manual_work(conn):
    """Dropping source relevance would surface stale and unrelated work under Others."""
    work_items.ensure_self_person(conn, "Taylor")
    tracked = conn.execute(
        "INSERT INTO people (display_name, is_tracked, lane_position, created_at, updated_at) "
        "VALUES ('Morgan', 1, 1, 'now', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO person_identities (person_id, source, external_id, display_value) "
        "VALUES (?, 'gitlab', 'morgan.dev', 'Morgan')",
        (tracked,),
    )
    outsider = conn.execute(
        "INSERT INTO people (display_name, is_tracked, created_at, updated_at) "
        "VALUES ('Outside', 0, 'now', 'now')"
    ).lastrowid

    tracked_item = work_items.create_work_item(
        conn, title="Tracked teammate", owner_person_id=tracked, origin="discovery"
    )
    stale = work_items.create_work_item(
        conn, title="Stale unrelated MR", owner_person_id=outsider, origin="discovery"
    )
    work_items.add_work_link(
        conn, stale["id"], source_type="gitlab_mr", external_id="1!stale"
    )
    relevant_mr = work_items.create_work_item(
        conn, title="External MR assigned to me", owner_person_id=outsider, origin="discovery"
    )
    work_items.add_work_link(
        conn, relevant_mr["id"], source_type="gitlab_mr", external_id="1!mine"
    )
    unrelated_clickup = work_items.create_work_item(
        conn, title="Someone else's ClickUp task", owner_person_id=outsider, origin="discovery"
    )
    work_items.add_work_link(
        conn, unrelated_clickup["id"], source_type="clickup", external_id="outside-task"
    )
    manual = work_items.create_work_item(
        conn, title="Manually retained", owner_display="External collaborator"
    )
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id, title, state, author_username, roles, "
        "assignee_usernames, reviewer_usernames, synced_at) "
        "VALUES ('1!mine', 'External MR assigned to me', 'opened', 'outside.person', "
        "'[\"assignee\"]', '[\"taylor.dev\"]', '[]', 'now')"
    )
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, list_name, url, assignees, synced_at) "
        "VALUES ('outside-task', 'Someone else''s ClickUp task', 'open', '', '', "
        "'[\"Drashti Darji\"]', 'now')"
    )
    conn.commit()

    lanes = {lane["name"]: lane for lane in work_items.team_work(conn)["lanes"]}

    assert [item["id"] for item in lanes["Morgan"]["items"]] == [tracked_item["id"]]
    assert {item["id"] for item in lanes["Others"]["items"]} == {relevant_mr["id"], manual["id"]}
    assert lanes["Unassigned"]["items"] == []
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 5


def test_inbox_resolution_links_capture_entries_reminders_and_activity(conn):
    """Removing any relationship update from resolution must fail this test."""
    from app.services import work_inbox

    item = work_items.create_work_item(conn, title="Pixel investigation")
    capture_id = conn.execute(
        "INSERT INTO captures (raw_text, created_at) VALUES ('Pixel investigation blocker', 'now')"
    ).lastrowid
    entry_id = conn.execute(
        "INSERT INTO entries (capture_id, type, title, body, people, tags, created_at) "
        "VALUES (?, 'note', 'Blocker', '', '[]', '[]', 'now')",
        (capture_id,),
    ).lastrowid
    reminder_id = conn.execute(
        "INSERT INTO reminders (entry_id, text, due_at, created_at) VALUES (?, 'Resolve it', 'tomorrow', 'now')",
        (entry_id,),
    ).lastrowid
    conn.commit()

    inbox = work_inbox.suggest_capture_link(conn, capture_id, "Pixel investigation blocker")
    assert inbox["status"] == "pending"
    assert inbox["suggested_work_item_id"] == item["id"]

    resolved = work_inbox.resolve_inbox_item(conn, inbox["id"], item["id"])
    assert resolved["status"] == "linked"
    assert conn.execute("SELECT work_item_id FROM captures WHERE id=?", (capture_id,)).fetchone()[0] == item["id"]
    assert conn.execute("SELECT work_item_id FROM entries WHERE id=?", (entry_id,)).fetchone()[0] == item["id"]
    assert conn.execute("SELECT work_item_id FROM reminders WHERE id=?", (reminder_id,)).fetchone()[0] == item["id"]
    activity = conn.execute(
        "SELECT activity_type, body, capture_id FROM work_activity WHERE work_item_id=?", (item["id"],)
    ).fetchone()
    assert tuple(activity) == ("capture", "Pixel investigation blocker", capture_id)


def test_managed_task_backfill_is_idempotent_and_preserves_legacy_rows(conn):
    """Creating a second migration item or mutating legacy rows is a bug."""
    from tests.conftest import seed_task
    from app.services import work_backfill

    seed_task(conn)
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, related_clickup_task_id, status, created_at)"
        " VALUES ('mine1', 'abc123', 'open', '2026-08-27')"
    )
    conn.commit()

    first = work_backfill.backfill_managed_work(conn)
    second = work_backfill.backfill_managed_work(conn)

    assert first["created"] == 1
    assert second["created"] == 0
    assert conn.execute("SELECT count(*) FROM managed_tasks").fetchone()[0] == 1
    item = conn.execute("SELECT title, origin, state FROM work_items").fetchone()
    assert tuple(item) == ("Fix iframe z-index on Playback player", "migration", "next")
    assert conn.execute(
        "SELECT external_id FROM work_links WHERE source_type='clickup'"
    ).fetchone()[0] == "abc123"


def test_backfill_assigns_migrated_items_to_self_and_uses_a_name_fallback(conn):
    """Legacy work belongs in My Work even if its cached ClickUp name is NULL."""
    from app.services import work_backfill

    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, list_name, url, assignees, due_date, synced_at) "
        "VALUES ('legacy-null-name', NULL, 'open', '', '', '[]', NULL, 'now')"
    )
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, related_clickup_task_id, status, created_at) "
        "VALUES ('mine1', 'legacy-null-name', 'open', 'now')"
    )
    conn.commit()

    work_backfill.backfill_managed_work(conn)

    self_person = conn.execute("SELECT id FROM people WHERE is_self=1").fetchone()
    item = conn.execute("SELECT id, title, owner_person_id FROM work_items").fetchone()
    assert (item["title"], item["owner_person_id"]) == ("ClickUp legacy-null-name", self_person["id"])
    assert [row["id"] for row in work_items.my_work(conn, self_person["id"])["next"]] == [
        item["id"]
    ]
    assert "ClickUp legacy-null-name" not in [
        row["title"] for lane in work_items.team_work(conn)["lanes"]
        if lane["name"] == "Unassigned" for row in lane["items"]
    ]


def test_ensure_self_person_preserves_an_existing_display_name(conn):
    """Reading or creating My Work must not silently overwrite profile naming."""
    original_id = work_items.ensure_self_person(conn, "Original profile name")

    reused_id = work_items.ensure_self_person(conn, "Configured name")

    assert reused_id == original_id
    assert conn.execute(
        "SELECT display_name FROM people WHERE id=?", (original_id,)
    ).fetchone()["display_name"] == "Original profile name"


def test_backfill_skips_conflicting_mrs_and_malformed_ids_without_repeating(conn):
    """A duplicated MR must not abort other groups or create duplicate links."""
    from app.services import work_backfill

    conn.executemany(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, list_name, url, assignees, due_date, synced_at) "
        "VALUES (?, ?, 'open', '', ?, '[]', NULL, 'now')",
        [("source-a", "First source", None), ("source-b", "Second source", "")],
    )
    conn.executemany(
        "INSERT INTO managed_tasks (clickup_task_id, related_clickup_task_id, related_mr_id, "
        "additional_mr_ids, status, created_at) VALUES (?, ?, '1!shared', ?, 'open', 'now')",
        [
            ("mine-a", "source-a", '["2!valid", {"mr": "ignored"}, "", null]'),
            ("mine-b", "source-b", "[]"),
        ],
    )
    conn.commit()

    first = work_backfill.backfill_managed_work(conn)
    second = work_backfill.backfill_managed_work(conn)

    assert first["created"] == 2
    assert second["created"] == 0
    assert conn.execute("SELECT count(*) FROM work_items").fetchone()[0] == 2
    assert conn.execute(
        "SELECT count(*) FROM work_links WHERE source_type='gitlab_mr'"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT url FROM work_links WHERE source_type='clickup' AND external_id='source-a'"
    ).fetchone()[0] == ""
    assert conn.execute(
        "SELECT count(*) FROM system_events WHERE kind='backfill_conflict'"
    ).fetchone()[0] == 1


def test_backfill_rolls_back_an_incomplete_group(conn, monkeypatch):
    """A link failure after item creation must leave no orphan migration item."""
    from tests.conftest import seed_task
    from app.services import work_backfill

    seed_task(conn)
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, related_clickup_task_id, status, created_at) "
        "VALUES ('mine1', 'abc123', 'open', 'now')"
    )
    conn.commit()

    real_add_link = work_backfill.work_items.add_work_link

    def fail_clickup_link(*args, **kwargs):
        if kwargs["source_type"] == "clickup":
            raise RuntimeError("simulated link failure")
        return real_add_link(*args, **kwargs)

    monkeypatch.setattr(work_backfill.work_items, "add_work_link", fail_clickup_link)
    with pytest.raises(RuntimeError, match="simulated link failure"):
        work_backfill.backfill_managed_work(conn)

    assert conn.execute("SELECT count(*) FROM work_items").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM work_links").fetchone()[0] == 0


def test_inbox_resolution_links_reminder_without_an_entry(conn):
    """Reminder-only classifications need the same capture-based Inbox attachment."""
    from app.services import work_inbox

    item = work_items.create_work_item(conn, title="Pixel investigation")
    capture_id = conn.execute(
        "INSERT INTO captures (raw_text, created_at) VALUES ('Pixel reminder', 'now')"
    ).lastrowid
    reminder_id = conn.execute(
        "INSERT INTO reminders (entry_id, capture_id, text, due_at, created_at) "
        "VALUES (NULL, ?, 'Remember this', 'tomorrow', 'now')",
        (capture_id,),
    ).lastrowid
    conn.commit()

    inbox = work_inbox.suggest_capture_link(conn, capture_id, "Pixel reminder")
    work_inbox.resolve_inbox_item(conn, inbox["id"], item["id"])

    assert conn.execute(
        "SELECT work_item_id FROM reminders WHERE id=?", (reminder_id,)
    ).fetchone()[0] == item["id"]
