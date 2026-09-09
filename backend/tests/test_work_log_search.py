"""Work context must be discoverable without replacing the existing journal."""


def test_search_returns_work_item_and_activity_with_stable_internal_urls(client):
    item = client.post(
        "/api/work-items",
        json={"title": "Pixel investigation", "description": "Investigate the threshold"},
    ).json()["work_item"]
    client.post(
        f"/api/work-items/{item['id']}/activity",
        json={"activity_type": "decision", "body": "Use the 20 percent threshold"},
    )

    results = client.get("/api/search", params={"q": "threshold"}).json()["results"]
    by_kind = {row["kind"]: row for row in results}

    assert by_kind["work_item"] == {
        "kind": "work_item",
        "id": item["id"],
        "work_item_id": item["id"],
        "url": f"/work/{item['id']}",
        "title": "Pixel investigation",
        "snippet": "Investigate the threshold",
        "subtitle": "Work context",
        "created_at": item["created_at"],
        "icon": "📋",
    }
    activity = by_kind["work_activity"]
    assert activity["work_item_id"] == item["id"]
    assert activity["url"] == f"/work/{item['id']}"
    assert activity["title"] == "Pixel investigation"
    assert activity["snippet"] == "Use the 20 percent threshold"
    assert activity["activity_type"] == "decision"
    assert activity["created_at"]


def test_log_merges_work_activity_and_completed_work_with_work_filter(client):
    item = client.post(
        "/api/work-items", json={"title": "Pixel investigation", "description": "Threshold work"}
    ).json()["work_item"]
    client.post(
        f"/api/work-items/{item['id']}/activity",
        json={"activity_type": "blocker", "body": "Waiting for threshold data"},
    )
    client.patch(f"/api/work-items/{item['id']}/position", json={"state": "done"})

    rows = client.get("/api/log", params={"source": "work", "q": "threshold"}).json()["items"]
    assert {row["kind"] for row in rows} == {"work_activity", "work_completed"}
    assert all(row["source"] == "work" for row in rows)
    assert all(row["work_item_id"] == item["id"] for row in rows)
    assert all(row["url"] == f"/work/{item['id']}" for row in rows)
    activity = next(row for row in rows if row["kind"] == "work_activity")
    completed = next(row for row in rows if row["kind"] == "work_completed")
    assert activity["body"] == "Waiting for threshold data"
    assert activity["activity_type"] == "blocker"
    assert completed["body"] == "Threshold work"
    assert completed["completed_at"]


def test_work_source_excludes_entries_and_events_and_global_limit_is_newest_first(client, conn):
    item = client.post("/api/work-items", json={"title": "Work card"}).json()["work_item"]
    conn.execute(
        "UPDATE work_items SET created_at='2026-06-20T08:00:00', updated_at='2026-06-20T08:00:00' WHERE id=?",
        (item["id"],),
    )
    conn.execute(
        "INSERT INTO work_activity (work_item_id, activity_type, body, created_at) "
        "VALUES (?, 'note', 'old work note', '2026-06-20T09:00:00')",
        (item["id"],),
    )
    conn.execute(
        "INSERT INTO entries (capture_id, type, title, body, tags, created_at) "
        "VALUES (NULL, 'note', 'new entry', '', '[]', '2026-06-20T11:00:00')"
    )
    conn.execute(
        "INSERT INTO system_events (kind, subject, created_at) "
        "VALUES ('sync', 'new event', '2026-06-20T12:00:00')"
    )
    conn.commit()

    work_rows = client.get("/api/log", params={"source": "work"}).json()["items"]
    assert [row["source"] for row in work_rows] == ["work"]
    merged = client.get("/api/log", params={"limit": 2}).json()["items"]
    assert [row["title"] for row in merged] == ["new event", "new entry"]


def test_log_hides_legacy_state_change_events(client, conn):
    conn.execute(
        "INSERT INTO system_events (kind, subject, created_at) "
        "VALUES ('state_change', 'Moved work to Next', '2026-06-20T12:00:00')"
    )
    conn.execute(
        "INSERT INTO system_events (kind, subject, created_at) "
        "VALUES ('sync', 'Sync complete', '2026-06-20T11:00:00')"
    )
    conn.commit()

    result = client.get("/api/log").json()
    assert [row["kind"] for row in result["items"]] == ["sync"]
    assert result["kinds"] == ["sync"]
    assert client.get("/api/log", params={"kind": "state_change"}).json()["items"] == []
