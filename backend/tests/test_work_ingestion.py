import pytest

from app import db
from app.services import work_items


def _insert_mr(
    conn, mr_id, title, *, author_username="", branch="", description="", url="",
    roles='["reviewer"]', assignee_usernames="[]", reviewer_usernames="[]",
    state="opened",
):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache "
        "(mr_id, title, state, author_username, source_branch, description, url, roles, "
        "assignee_usernames, reviewer_usernames, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'now')",
        (
            mr_id, title, state, author_username, branch, description, url, roles,
            assignee_usernames, reviewer_usernames,
        ),
    )
    conn.commit()


def test_terminal_mrs_retire_discovered_work_without_deleting_it(conn):
    """Leaving terminal discovery visible would keep completed team work on the board."""
    from app.services import work_ingestion

    item = work_items.create_work_item(conn, title="Reusable Media", origin="discovery")
    work_items.add_work_link(
        conn, item["id"], source_type="gitlab_mr", external_id="1!closed"
    )
    work_items.add_work_link(
        conn, item["id"], source_type="gitlab_mr", external_id="2!merged"
    )
    _insert_mr(conn, "1!closed", "Closed half", state="closed")
    _insert_mr(conn, "2!merged", "Merged half", state="merged")

    assert work_ingestion.retire_terminal_mr_work(conn) == 1
    assert conn.execute(
        "SELECT origin FROM work_items WHERE id=?", (item["id"],)
    ).fetchone()["origin"] == "ignored"
    assert conn.execute(
        "SELECT COUNT(*) FROM work_links WHERE work_item_id=?", (item["id"],)
    ).fetchone()[0] == 2


def test_one_open_mr_keeps_multi_repo_discovery_visible(conn):
    """Retiring on any terminal MR would hide a multi-repo card that still has active work."""
    from app.services import work_ingestion

    item = work_items.create_work_item(conn, title="Multi repo", origin="discovery")
    for mr_id in ("1!closed", "2!open"):
        work_items.add_work_link(
            conn, item["id"], source_type="gitlab_mr", external_id=mr_id
        )
    _insert_mr(conn, "1!closed", "Closed half", state="closed")
    _insert_mr(conn, "2!open", "Open half", state="opened")

    assert work_ingestion.retire_terminal_mr_work(conn) == 0
    assert conn.execute(
        "SELECT origin FROM work_items WHERE id=?", (item["id"],)
    ).fetchone()["origin"] == "discovery"


def test_missing_mr_cache_row_keeps_discovery_visible(conn):
    """A partial GitLab refresh must not hide work whose current state is unknown."""
    from app.services import work_ingestion

    item = work_items.create_work_item(conn, title="Uncertain work", origin="discovery")
    work_items.add_work_link(
        conn, item["id"], source_type="gitlab_mr", external_id="1!closed"
    )
    work_items.add_work_link(
        conn, item["id"], source_type="gitlab_mr", external_id="2!missing"
    )
    _insert_mr(conn, "1!closed", "Closed half", state="closed")

    assert work_ingestion.retire_terminal_mr_work(conn) == 0
    assert conn.execute(
        "SELECT origin FROM work_items WHERE id=?", (item["id"],)
    ).fetchone()["origin"] == "discovery"


def test_terminal_mr_never_retires_manual_work(conn):
    """Broad retirement would remove work that the user explicitly added."""
    from app.services import work_ingestion

    item = work_items.create_work_item(conn, title="Keep manually", origin="manual")
    work_items.add_work_link(
        conn, item["id"], source_type="gitlab_mr", external_id="1!closed"
    )
    _insert_mr(conn, "1!closed", "Closed MR", state="closed")

    assert work_ingestion.retire_terminal_mr_work(conn) == 0
    assert conn.execute(
        "SELECT origin FROM work_items WHERE id=?", (item["id"],)
    ).fetchone()["origin"] == "manual"


def test_repository_allowlist_retires_only_unselected_mr_only_discovery(conn):
    """Unselected automatic MR cards must leave Team without hiding explicit or ClickUp work."""
    from app.services import app_settings, work_ingestion

    app_settings.set_value(
        conn,
        "integration.gitlab.projects",
        [{"id": 1, "name": "kept", "path": "cm/kept"}],
    )
    unselected = work_items.create_work_item(
        conn, title="Unselected automatic MR", origin="discovery"
    )
    manual = work_items.create_work_item(
        conn, title="Explicit outside MR", origin="manual"
    )
    clickup_backed = work_items.create_work_item(
        conn, title="ClickUp work", origin="discovery"
    )
    selected = work_items.create_work_item(
        conn, title="Selected MR", origin="discovery"
    )
    for item, mr_id in (
        (unselected, "2!20"),
        (manual, "2!21"),
        (clickup_backed, "2!22"),
        (selected, "1!10"),
    ):
        work_items.add_work_link(
            conn, item["id"], source_type="gitlab_mr", external_id=mr_id
        )
    work_items.add_work_link(
        conn,
        clickup_backed["id"],
        source_type="clickup",
        external_id="86keep",
    )

    assert work_ingestion.retire_unselected_gitlab_work(conn) == 1
    assert {
        row["title"]: row["origin"]
        for row in conn.execute("SELECT title, origin FROM work_items")
    } == {
        "Unselected automatic MR": "ignored",
        "Explicit outside MR": "manual",
        "ClickUp work": "discovery",
        "Selected MR": "discovery",
    }


def test_missing_gitlab_work_is_retired_after_a_complete_cache_refresh(conn):
    """Leaving cache-missing discovery active would keep stale cards in Others."""
    from app.services import work_ingestion

    stale = work_items.create_work_item(
        conn, title="Stale automatic MR", origin="discovery"
    )
    current = work_items.create_work_item(
        conn, title="Current automatic MR", origin="discovery"
    )
    manual = work_items.create_work_item(
        conn, title="Explicit old MR", origin="manual"
    )
    clickup_backed = work_items.create_work_item(
        conn, title="ClickUp-backed old MR", origin="discovery"
    )
    for item, mr_id in (
        (stale, "1!9"),
        (current, "1!10"),
        (manual, "1!11"),
        (clickup_backed, "1!12"),
    ):
        work_items.add_work_link(
            conn, item["id"], source_type="gitlab_mr", external_id=mr_id
        )
    work_items.add_work_link(
        conn,
        clickup_backed["id"],
        source_type="clickup",
        external_id="86keep",
    )
    _insert_mr(conn, "1!10", "Current automatic MR", author_username="morgan.dev")

    assert work_ingestion.retire_missing_gitlab_work(conn) == 1
    assert {
        row["title"]: row["origin"]
        for row in conn.execute("SELECT title, origin FROM work_items")
    } == {
        "Stale automatic MR": "ignored",
        "Current automatic MR": "discovery",
        "Explicit old MR": "manual",
        "ClickUp-backed old MR": "discovery",
    }


def test_ingestion_skips_mr_groups_unrelated_to_self_or_tracked_people(conn):
    """Removing the relevance gate would recreate unrelated Team cards on every sync."""
    from app.services import work_ingestion

    tracked_id = conn.execute(
        "INSERT INTO people (display_name, is_tracked, created_at, updated_at) "
        "VALUES ('Morgan', 1, 'now', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO person_identities (person_id, source, external_id, display_value) "
        "VALUES (?, 'gitlab', 'morgan.dev', 'Morgan')",
        (tracked_id,),
    )
    conn.commit()
    _insert_mr(
        conn, "1!unrelated", "Unrelated MR", author_username="outside.person", roles="[]",
    )
    _insert_mr(
        conn, "1!mine", "Assigned to me", author_username="outside.person",
        roles='["assignee"]', assignee_usernames='["taylor.dev"]',
    )
    _insert_mr(
        conn, "1!team", "Authored by my team", author_username="morgan.dev", roles="[]",
    )

    result = work_ingestion.ingest_cached_mrs(conn)

    assert result == {"created": 2, "updated": 0}
    assert [
        row["title"] for row in conn.execute("SELECT title FROM work_items ORDER BY title")
    ] == ["Assigned to me", "Authored by my team"]
    assert conn.execute(
        "SELECT COUNT(*) FROM people WHERE display_name='outside.person'"
    ).fetchone()[0] == 1


def test_mrs_with_same_clickup_id_form_one_next_item(conn):
    """Removing the ClickUp grouping would create one card per MR."""
    from app.services import work_ingestion

    _insert_mr(conn, "1!10", "API half", author_username="morgan.dev", branch="feature_clickup86abc1234")
    _insert_mr(conn, "2!20", "UI half", author_username="morgan.dev", branch="ui_clickup-86abc1234")

    result = work_ingestion.ingest_cached_mrs(conn)

    assert result == {"created": 1, "updated": 0}
    item = conn.execute("SELECT * FROM work_items").fetchone()
    assert item["title"] == "API half"
    assert item["title_is_manual"] == 0
    assert item["state"] == "next"
    links = conn.execute(
        "SELECT source_type, external_id FROM work_links WHERE work_item_id=? ORDER BY source_type, external_id",
        (item["id"],),
    ).fetchall()
    assert [(row["source_type"], row["external_id"]) for row in links] == [
        ("clickup", "86abc1234"), ("gitlab_mr", "1!10"), ("gitlab_mr", "2!20")
    ]


def test_explicit_tracked_identity_owns_discovered_item(conn):
    """Dropping identity lookup would route an explicitly tracked teammate to Others."""
    from app.services import work_ingestion

    person_id = conn.execute(
        "INSERT INTO people (display_name, is_tracked, created_at, updated_at) VALUES ('Morgan', 1, 'now', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO person_identities (person_id, source, external_id, display_value) "
        "VALUES (?, 'gitlab', 'morgan.dev', 'Morgan G')",
        (person_id,),
    )
    conn.commit()
    _insert_mr(conn, "3!30", "Tracked MR", author_username="morgan.dev")

    work_ingestion.ingest_cached_mrs(conn)

    assert conn.execute("SELECT owner_person_id FROM work_items").fetchone()["owner_person_id"] == person_id
    assert conn.execute("SELECT COUNT(*) FROM people WHERE is_tracked=1").fetchone()[0] == 1


def test_untracked_author_routes_to_others_without_named_lane(conn):
    """Promoting unknown GitLab authors would create a named team lane."""
    from app.services import work_ingestion

    _insert_mr(conn, "3!30", "Other MR", author_username="new.person")

    work_ingestion.ingest_cached_mrs(conn)

    item = conn.execute("SELECT * FROM work_items").fetchone()
    person = conn.execute("SELECT * FROM people WHERE id=?", (item["owner_person_id"],)).fetchone()
    identity = conn.execute("SELECT * FROM person_identities WHERE person_id=?", (person["id"],)).fetchone()
    assert person["display_name"] == "new.person"
    assert person["is_tracked"] == 0
    assert (identity["source"], identity["external_id"]) == ("gitlab", "new.person")


def test_missing_author_stays_unassigned(conn):
    """Inventing an owner for a missing author would make ownership misleading."""
    from app.services import work_ingestion

    _insert_mr(conn, "3!31", "No author")
    work_ingestion.ingest_cached_mrs(conn)

    item = conn.execute("SELECT * FROM work_items").fetchone()
    assert (item["owner_person_id"], item["owner_display"]) == (None, "")


def test_ingestion_commits_unknown_author_discovery(conn):
    """A scheduler connection closing after discovery must not roll back the new card."""
    from app.services import work_ingestion

    _insert_mr(conn, "3!32", "Durable discovery", author_username="durable.author")
    scheduled_conn = db.connect()
    try:
        work_ingestion.ingest_cached_mrs(scheduled_conn)
    finally:
        scheduled_conn.close()

    assert conn.execute("SELECT COUNT(*) FROM work_items WHERE title='Durable discovery'").fetchone()[0] == 1


def test_repeat_ingestion_preserves_local_choices_and_adds_new_link(conn):
    """Refreshing discovery must not undo a user's state, position, title, or owner choice."""
    from app.services import work_ingestion

    _insert_mr(conn, "4!40", "Generated title", author_username="owner", branch="feature_clickup86repeat")
    work_ingestion.ingest_cached_mrs(conn)
    item = conn.execute("SELECT * FROM work_items").fetchone()
    work_items.update_work_item(conn, item["id"], title="My chosen title", owner_display="Custom owner")
    work_items.move_work_item(conn, item["id"], state="waiting")
    before = conn.execute("SELECT state, position, title, owner_person_id, owner_display FROM work_items").fetchone()
    _insert_mr(conn, "4!41", "Second repo", author_username="different", branch="ui_clickup86repeat")

    result = work_ingestion.ingest_cached_mrs(conn)

    after = conn.execute("SELECT state, position, title, owner_person_id, owner_display FROM work_items").fetchone()
    assert result == {"created": 0, "updated": 1}
    assert tuple(after) == tuple(before)
    assert conn.execute("SELECT COUNT(*) FROM work_links WHERE work_item_id=?", (item["id"],)).fetchone()[0] == 3


def test_reconcile_clickup_titles_only_changes_generated_titles_and_records_activity(conn):
    """Reconciling a cache title must neither overwrite manual work nor omit its audit row."""
    from app.services import work_ingestion

    generated = work_items.create_work_item(conn, title="Old generated", origin="discovery")
    manual = work_items.create_work_item(conn, title="Keep my title")
    for item in (generated, manual):
        work_items.add_work_link(conn, item["id"], source_type="clickup", external_id=f"task-{item['id']}")
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, list_name, url, assignees, synced_at) "
            "VALUES (?, ?, 'open', '', '', '[]', 'now')",
            (f"task-{item['id']}", f"Cache title {item['id']}"),
        )
    conn.commit()

    assert work_ingestion.reconcile_clickup_titles(conn) == 1

    assert conn.execute("SELECT title FROM work_items WHERE id=?", (generated["id"],)).fetchone()[0] == f"Cache title {generated['id']}"
    assert conn.execute("SELECT title FROM work_items WHERE id=?", (manual["id"],)).fetchone()[0] == "Keep my title"
    activity = conn.execute("SELECT activity_type, body, metadata_json FROM work_activity WHERE work_item_id=?", (generated["id"],)).fetchone()
    assert activity["activity_type"] == "system"
    assert "Old generated" in activity["body"]
    assert f"Cache title {generated['id']}" in activity["body"]


def test_clickup_group_does_not_replace_reconciled_title_with_mr_title(conn):
    """MR refresh must not fight ClickUp's generated-title reconciliation."""
    from app.services import work_ingestion

    _insert_mr(conn, "7!70", "Original MR title", branch="feature_clickup86title")
    work_ingestion.ingest_cached_mrs(conn)
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, list_name, url, assignees, synced_at) "
        "VALUES ('86title', 'ClickUp canonical title', 'open', '', '', '[]', 'now')"
    )
    conn.commit()
    assert work_ingestion.reconcile_clickup_titles(conn) == 1
    item = conn.execute("SELECT * FROM work_items").fetchone()
    _insert_mr(conn, "7!71", "Later MR title", branch="ui_clickup86title")

    work_ingestion.ingest_cached_mrs(conn)

    assert conn.execute("SELECT title FROM work_items WHERE id=?", (item["id"],)).fetchone()[0] == "ClickUp canonical title"
    assert work_ingestion.reconcile_clickup_titles(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM work_activity WHERE work_item_id=?", (item["id"],)).fetchone()[0] == 1


def test_uncached_clickup_group_updates_its_generated_mr_fallback_title(conn):
    """Without a cached ClickUp task, the MR title remains the generated fallback."""
    from app.services import work_ingestion

    _insert_mr(conn, "7!72", "Fallback title v1", branch="feature_clickup86fallback")
    work_ingestion.ingest_cached_mrs(conn)
    conn.execute("UPDATE gitlab_mrs_cache SET title='Fallback title v2' WHERE mr_id='7!72'")
    conn.commit()

    work_ingestion.ingest_cached_mrs(conn)

    assert conn.execute("SELECT title FROM work_items").fetchone()[0] == "Fallback title v2"


def test_linked_clickup_ids_is_stable_and_deduplicated(conn):
    """A raw join would leak duplicate refresh IDs and unstable ordering."""
    from app.services import work_ingestion

    first = work_items.create_work_item(conn, title="First")
    second = work_items.create_work_item(conn, title="Second")
    work_items.add_work_link(conn, first["id"], source_type="clickup", external_id="z-task")
    work_items.add_work_link(conn, second["id"], source_type="clickup", external_id="a-task")

    assert work_ingestion.linked_clickup_ids(conn) == ["a-task", "z-task"]


def test_manual_clickup_url_imports_exact_task_to_next(client, monkeypatch):
    """Replacing exact import with arbitrary URL fetches would bypass the allowlist."""
    from app.services import clickup_client

    monkeypatch.setattr(clickup_client, "get_task_resilient", lambda task_id: {
        "id": task_id, "name": "Imported task", "url": f"https://app.clickup.com/t/{task_id}",
        "status": {"status": "open", "type": "open"}, "assignees": [],
    })

    response = client.post("/api/work-import", json={"url": "https://app.clickup.com/t/86abc1234"})
    again = client.post("/api/work-import", json={"url": "https://app.clickup.com/t/86abc1234"})

    assert response.status_code == 201
    assert response.json()["work_item"]["state"] == "next"
    assert response.json()["work_item"]["origin"] == "manual"
    assert response.json()["work_item"]["title_is_manual"] is False
    assert again.status_code == 200
    assert again.json()["work_item"]["id"] == response.json()["work_item"]["id"]


@pytest.mark.parametrize("url", [
    "https://app.clickup.com/t/86abc1234/extra",
    "https://not-clickup.test/t/86abc1234",
    "https://gitlab.example/group/project/-/merge_requests/not-a-number",
])
def test_manual_import_rejects_unsupported_or_malformed_url(client, url):
    """Weak URL matching would let user-controlled text become a fetch target."""
    assert client.post("/api/work-import", json={"url": url}).status_code == 422


def test_manual_gitlab_import_ingests_only_the_exact_cached_mr(client, monkeypatch):
    """Importing all cached MRs after one URL would create unrelated work cards."""
    from app.services import gitlab_client

    def cache_exact(conn, url):
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, title, state, url, author_username, synced_at) "
            "VALUES ('5!50', 'Exact MR', 'opened', ?, 'exact.author', 'now')",
            (url,),
        )
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, title, state, url, author_username, synced_at) "
            "VALUES ('5!51', 'Unrelated MR', 'opened', 'https://gitlab.example/other/project/-/merge_requests/51', 'other', 'now')"
        )
        conn.commit()
        return ["5!50"]

    monkeypatch.setattr(gitlab_client, "ensure_mrs_cached", cache_exact)
    response = client.post("/api/work-import", json={"url": "https://gitlab.example/group/project/-/merge_requests/50"})

    assert response.status_code == 201
    assert response.json()["work_item"]["title"] == "Exact MR"
    assert response.json()["work_item"]["origin"] == "manual"
    assert client.get("/api/work-items/team").json()["lanes"][-2]["items"][0]["title"] == "Exact MR"


def test_gitlab_import_returns_existing_clickup_group_item(client, monkeypatch):
    """An MR joining a manually imported ClickUp group must not claim a new card."""
    from app.services import clickup_client, gitlab_client

    monkeypatch.setattr(clickup_client, "get_task_resilient", lambda task_id: {
        "id": task_id, "name": "Shared task", "url": f"https://app.clickup.com/t/{task_id}",
        "status": {"status": "open", "type": "open"}, "assignees": [],
    })
    imported = client.post("/api/work-import", json={"url": "https://app.clickup.com/t/86shared"})

    def cache_exact(conn, url):
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, title, state, url, source_branch, synced_at) "
            "VALUES ('6!60', 'Shared MR', 'opened', ?, 'feature_clickup86shared', 'now')",
            (url,),
        )
        conn.commit()
        return ["6!60"]

    monkeypatch.setattr(gitlab_client, "ensure_mrs_cached", cache_exact)
    joined = client.post("/api/work-import", json={"url": "https://gitlab.example/group/project/-/merge_requests/60"})

    assert joined.status_code == 200
    assert joined.json()["work_item"]["id"] == imported.json()["work_item"]["id"]


def test_gitlab_import_joining_clickup_card_keeps_clickup_generated_title(client, monkeypatch):
    """Joining an MR must not temporarily replace a cached ClickUp title."""
    from app.services import clickup_client, gitlab_client

    monkeypatch.setattr(clickup_client, "get_task_resilient", lambda task_id: {
        "id": task_id, "name": "Canonical ClickUp title", "url": f"https://app.clickup.com/t/{task_id}",
        "status": {"status": "open", "type": "open"}, "assignees": [],
    })
    imported = client.post("/api/work-import", json={"url": "https://app.clickup.com/t/86stable"})

    def cache_exact(conn, url):
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, title, state, url, source_branch, synced_at) "
            "VALUES ('8!80', 'Different MR title', 'opened', ?, 'feature_clickup86stable', 'now')",
            (url,),
        )
        conn.commit()
        return ["8!80"]

    monkeypatch.setattr(gitlab_client, "ensure_mrs_cached", cache_exact)
    joined = client.post("/api/work-import", json={"url": "https://gitlab.example/group/project/-/merge_requests/80"})

    assert joined.status_code == 200
    assert joined.json()["work_item"]["id"] == imported.json()["work_item"]["id"]
    assert joined.json()["work_item"]["title"] == "Canonical ClickUp title"
