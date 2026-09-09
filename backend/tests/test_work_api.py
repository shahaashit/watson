def test_create_move_and_read_work_item(client):
    created = client.post("/api/work-items", json={"title": "Investigate pixels"})
    assert created.status_code == 201
    item = created.json()["work_item"]

    moved = client.patch(
        f"/api/work-items/{item['id']}/position",
        json={"state": "today", "before_id": None, "after_id": None},
    )

    assert moved.status_code == 200
    assert moved.json()["work_item"]["state"] == "today"
    assert client.get("/api/work-items/my").json()["today"][0]["id"] == item["id"]


def test_my_tasks_expose_clickup_links_without_live_provider_reads(client, conn):
    item = client.post('/api/work-items', json={'title': 'My task'}).json()['work_item']
    conn.execute("INSERT INTO work_links (work_item_id,source_type,external_id,created_at) VALUES (?,'clickup','abc123','now')", (item['id'],))
    conn.commit()
    task = client.get('/api/work-items/my').json()['items'][0]
    assert task['clickup_url'] == 'https://app.clickup.com/t/abc123'
    conn.execute("UPDATE work_links SET external_id='../../unsafe' WHERE work_item_id=?", (item['id'],))
    conn.commit()
    task = client.get('/api/work-items/my').json()['items'][0]
    assert task['clickup_url'] == ''


def test_my_task_link_prefers_own_managed_task_over_related_team_task(client, conn):
    item = client.post('/api/work-items', json={'title': 'My task'}).json()['work_item']
    for task_id in ('team123', 'mine456'):
        conn.execute("INSERT INTO work_links (work_item_id,source_type,external_id,created_at) VALUES (?,'clickup',?,'now')", (item['id'], task_id))
    conn.execute("INSERT INTO managed_tasks (clickup_task_id,related_clickup_task_id) VALUES ('mine456','team123')")
    conn.commit()
    assert client.get('/api/work-items/my').json()['items'][0]['clickup_url'] == 'https://app.clickup.com/t/mine456'


def test_my_work_excludes_tracked_reviews_without_deleting_them(client, conn):
    personal = client.post('/api/work-items', json={'title': 'Review my own project plan'}).json()['work_item']
    review = client.post('/api/work-items', json={'title': 'Review - Team merge request'}).json()['work_item']
    conn.execute("INSERT INTO work_links (work_item_id,source_type,external_id,created_at) VALUES (?,'clickup','review123','now')", (review['id'],))
    conn.execute("INSERT INTO managed_tasks (clickup_task_id,category) VALUES ('review123','Review')")
    conn.commit()
    result = client.get('/api/work-items/my').json()
    assert [item['id'] for item in result['items']] == [personal['id']]
    assert [item['id'] for item in result['next']] == [personal['id']]
    assert client.get(f"/api/work-items/{review['id']}").status_code == 200
    assert conn.execute('SELECT count(*) FROM managed_tasks').fetchone()[0] == 1


def test_board_routes_follow_profile_mode_and_hide_historical_done(client, conn):
    mine = client.post("/api/work-items", json={"title": "Active"}).json()["work_item"]
    historical = client.post(
        "/api/work-items", json={"title": "Historical", "state": "done"}
    ).json()["work_item"]
    conn.execute(
        "UPDATE work_items SET completed_at='2026-06-30T12:00:00' WHERE id=?",
        (historical["id"],),
    )
    conn.commit()

    flat = client.get("/api/work-items/my").json()
    assert flat["mode"] == "flat"
    assert [item["id"] for item in flat["items"]] == [mine["id"]]
    assert flat["done"] == []

    saved = client.patch(
        "/api/settings/profile", json={"separate_work_by_status": True}
    )
    assert saved.status_code == 200
    segregated = client.get("/api/work-items/my").json()
    assert segregated["mode"] == "segregated"
    assert segregated["done"] == []

    team = client.get("/api/work-items/team").json()
    assert team["mode"] == "segregated"


def test_startup_backfills_existing_managed_work_exactly_once(conn):
    """Lifespan startup must adopt legacy managed rows without modifying the source row."""
    from fastapi.testclient import TestClient

    from app.main import app

    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, list_name, url, assignees, due_date, synced_at) "
        "VALUES ('startup-linked', 'Startup migration', 'open', '', '', '[]', NULL, 'now')"
    )
    legacy_id = conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, related_clickup_task_id, status, created_at) "
        "VALUES ('mine-startup', 'startup-linked', 'open', 'now')"
    ).lastrowid
    conn.commit()

    with TestClient(app):
        pass
    with TestClient(app):
        pass

    migrated = conn.execute(
        "SELECT title, origin FROM work_items WHERE title='Startup migration'"
    ).fetchall()
    legacy = conn.execute(
        "SELECT clickup_task_id, related_clickup_task_id, status FROM managed_tasks WHERE id=?",
        (legacy_id,),
    ).fetchone()
    assert [tuple(row) for row in migrated] == [("Startup migration", "migration")]
    assert tuple(legacy) == ("mine-startup", "startup-linked", "open")


def test_my_work_requests_do_not_rename_the_existing_self_profile(client, conn):
    """GET/create My Work only create a self person; Settings owns later renames."""
    self_person_id = conn.execute("SELECT id FROM people WHERE is_self=1").fetchone()["id"]
    conn.execute(
        "UPDATE people SET display_name='Chosen profile name' WHERE id=?", (self_person_id,)
    )
    conn.commit()

    assert client.get("/api/work-items/my").status_code == 200
    assert client.post("/api/work-items", json={"title": "A local card"}).status_code == 201

    assert conn.execute(
        "SELECT display_name FROM people WHERE id=?", (self_person_id,)
    ).fetchone()["display_name"] == "Chosen profile name"


def test_external_write_is_not_triggered_by_move(client, monkeypatch):
    from app.services import clickup_client

    monkeypatch.setattr(
        clickup_client,
        "execute_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external write")),
    )
    item = client.post("/api/work-items", json={"title": "Local"}).json()["work_item"]

    assert client.patch(
        f"/api/work-items/{item['id']}/position", json={"state": "waiting"}
    ).status_code == 200


def test_work_item_routes_map_missing_items_and_bad_ordering(client):
    missing = 99999
    assert client.get(f"/api/work-items/{missing}").status_code == 404
    assert client.patch(f"/api/work-items/{missing}", json={"title": "Nope"}).status_code == 404
    assert client.patch(
        f"/api/work-items/{missing}/position", json={"state": "today"}
    ).status_code == 404
    assert client.post(
        f"/api/work-items/{missing}/activity", json={"activity_type": "note", "body": "Nope"}
    ).status_code == 404
    assert client.post(
        f"/api/work-items/{missing}/links", json={"source_type": "url", "external_id": "missing"}
    ).status_code == 404

    mine = client.post("/api/work-items", json={"title": "Mine"}).json()["work_item"]
    unassigned = client.post(
        "/api/work-items", json={"title": "Unassigned", "owner_person_id": None}
    ).json()["work_item"]

    invalid = client.patch(
        f"/api/work-items/{mine['id']}/position",
        json={"state": "today", "before_id": unassigned["id"]},
    )
    assert invalid.status_code == 409


def test_patch_preserves_omitted_owner_and_explicit_null_clears_it(client):
    item = client.post("/api/work-items", json={"title": "Owned"}).json()["work_item"]
    assert item["owner_person_id"] is not None

    renamed = client.patch(f"/api/work-items/{item['id']}", json={"title": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["work_item"]["owner_person_id"] == item["owner_person_id"]

    cleared = client.patch(f"/api/work-items/{item['id']}", json={"owner_person_id": None})
    assert cleared.status_code == 200
    assert cleared.json()["work_item"]["owner_person_id"] is None
    assert cleared.json()["work_item"]["owner_display"] == ""


def test_activity_links_and_team_work_are_available(client):
    item = client.post("/api/work-items", json={"title": "Investigate"}).json()["work_item"]

    activity = client.post(
        f"/api/work-items/{item['id']}/activity",
        json={"activity_type": "blocker", "body": "Awaiting test data"},
    )
    link = client.post(
        f"/api/work-items/{item['id']}/links",
        json={"source_type": "url", "external_id": "https://example.test", "label": "Context"},
    )
    detail = client.get(f"/api/work-items/{item['id']}")
    team = client.get("/api/work-items/team")

    assert activity.status_code == 201
    assert link.status_code == 201
    assert detail.json()["work_item"]["activity"][0]["body"] == "Awaiting test data"
    assert detail.json()["work_item"]["links"][0]["label"] == "Context"
    assert team.status_code == 200
    assert [lane["name"] for lane in team.json()["lanes"]][-2:] == ["Others", "Unassigned"]


def test_team_board_excludes_locally_ignored_discovered_work(client, conn):
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]
    visible = client.post(
        "/api/work-items",
        json={"title": "Recent work", "owner_person_id": person["id"]},
    ).json()["work_item"]
    ignored = client.post(
        "/api/work-items",
        json={"title": "Old discovered work", "owner_person_id": person["id"]},
    ).json()["work_item"]
    conn.execute(
        "UPDATE work_items SET origin='ignored' WHERE id=?", (ignored["id"],)
    )
    conn.commit()

    lane = next(
        lane
        for lane in client.get("/api/work-items/team").json()["lanes"]
        if lane["person"] and lane["person"]["id"] == person["id"]
    )

    assert [item["id"] for item in lane["items"]] == [visible["id"]]
    assert conn.execute(
        "SELECT title FROM work_items WHERE id=?", (ignored["id"],)
    ).fetchone()["title"] == "Old discovered work"


def test_team_cards_include_distinct_sorted_gitlab_repositories(client, conn):
    """Omitting linked MR projects would leave Team cards without repository context."""
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]
    item = client.post(
        "/api/work-items",
        json={"title": "Multi-repo change", "owner_person_id": person["id"]},
    ).json()["work_item"]
    conn.executemany(
        "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, synced_at) "
        "VALUES (?, ?, ?, 'opened', 'now')",
        [
            ("2!20", "cm/go/backend-service", "Backend",),
            ("1!10", "cm/frontend-service", "Frontend",),
            ("1!11", "cm/frontend-service", "Frontend follow-up",),
        ],
    )
    conn.commit()
    for mr_id in ("2!20", "1!10", "1!11"):
        assert client.post(
            f"/api/work-items/{item['id']}/links",
            json={"source_type": "gitlab_mr", "external_id": mr_id},
        ).status_code == 201

    lane = next(
        lane for lane in client.get("/api/work-items/team").json()["lanes"]
        if lane["person"] and lane["person"]["id"] == person["id"]
    )

    assert lane["items"][0]["repositories"] == ["cm/frontend-service", "cm/go/backend-service"]


def test_team_me_mode_keeps_only_work_directly_involving_the_local_user(client, conn):
    """Ignoring Me mode would leave unrelated teammate work in filtered lanes."""
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]

    def team_item(title):
        return client.post(
            "/api/work-items",
            json={"title": title, "owner_person_id": person["id"]},
        ).json()["work_item"]

    review = team_item("Review assigned to me")
    authored = team_item("Teammate work without me")
    assigned_clickup = team_item("ClickUp assigned to me")
    other_clickup = team_item("ClickUp assigned elsewhere")
    manual = team_item("Manually linked context")
    conn.executemany(
        "UPDATE work_items SET origin='discovery' WHERE id=?",
        [(review["id"],), (authored["id"],), (assigned_clickup["id"],), (other_clickup["id"],)],
    )
    conn.executemany(
        "INSERT INTO gitlab_mrs_cache "
        "(mr_id,title,state,role,roles,author_username,synced_at) "
        "VALUES (?,?,'opened',?,?,?,'now')",
        [
            ("1!10", "Review assigned to me", "reviewer", '["author","reviewer"]', "morgan.dev"),
            ("1!11", "Teammate work without me", "author", '["author"]', "morgan.dev"),
        ],
    )
    conn.executemany(
        "INSERT INTO clickup_tasks_cache "
        "(task_id,name,status,assignees,synced_at) VALUES (?,?,'open',?,'now')",
        [
                ("cu-me", "ClickUp assigned to me", '["User"]'),
            ("cu-other", "ClickUp assigned elsewhere", '["Someone Else"]'),
        ],
    )
    conn.commit()
    for item, source_type, external_id in (
        (review, "gitlab_mr", "1!10"),
        (authored, "gitlab_mr", "1!11"),
        (assigned_clickup, "clickup", "cu-me"),
        (other_clickup, "clickup", "cu-other"),
    ):
        assert client.post(
            f"/api/work-items/{item['id']}/links",
            json={"source_type": source_type, "external_id": external_id},
        ).status_code == 201

    full_lane = next(
        lane for lane in client.get("/api/work-items/team").json()["lanes"]
        if lane["person"] and lane["person"]["id"] == person["id"]
    )
    me_lane = next(
        lane for lane in client.get("/api/work-items/team?me_mode=true").json()["lanes"]
        if lane["person"] and lane["person"]["id"] == person["id"]
    )

    assert [item["title"] for item in full_lane["items"]] == [
        "Review assigned to me",
        "Teammate work without me",
        "ClickUp assigned to me",
        "ClickUp assigned elsewhere",
        "Manually linked context",
    ]
    assert [item["title"] for item in me_lane["items"]] == [
        "Review assigned to me",
        "ClickUp assigned to me",
        "Manually linked context",
    ]


def test_capture_inside_work_item_is_saved_and_attached(client, monkeypatch):
    """Dropping the capture-first attachment or classifier context must fail."""
    from app.services import classifier

    classified_work_item_ids = []

    def classify_capture(_conn, _capture_id, _text, work_item_id=None):
        classified_work_item_ids.append(work_item_id)
        return {"status": "classified", "entries": [], "reminders": [], "actions": []}

    monkeypatch.setattr(classifier, "classify_capture", classify_capture)
    item = client.post("/api/work-items", json={"title": "Pixel investigation"}).json()["work_item"]

    response = client.post(
        "/api/capture", json={"text": "blocker order differs", "work_item_id": item["id"]}
    )

    assert response.status_code == 200
    assert classified_work_item_ids == [item["id"]]
    detail = client.get(f"/api/work-items/{item['id']}").json()["work_item"]
    assert any(a["body"] == "blocker order differs" for a in detail["activity"])


def test_invalid_work_item_does_not_lose_the_raw_capture(client, conn, monkeypatch):
    """Validating an explicit attachment before insert would lose the user's text."""
    from app.services import classifier

    monkeypatch.setattr(
        classifier,
        "classify_capture",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not classify")),
    )
    response = client.post("/api/capture", json={"text": "keep this blocker", "work_item_id": 99999})

    assert response.status_code == 404
    assert conn.execute(
        "SELECT raw_text FROM captures WHERE raw_text='keep this blocker'"
    ).fetchone()["raw_text"] == "keep this blocker"


def test_explicit_capture_classification_inherits_its_work_item(client, conn, monkeypatch):
    """Omitting work_item_id from classifier fan-out would orphan its children."""
    import json

    from app.services import classifier
    from tests.test_classifier import CANNED

    monkeypatch.setattr(classifier, "call_llm", lambda _prompt: json.dumps(CANNED))
    item = client.post("/api/work-items", json={"title": "Pixel investigation"}).json()["work_item"]
    response = client.post("/api/capture", json={"text": "blocker order differs", "work_item_id": item["id"]})

    assert response.status_code == 200
    capture_id = response.json()["capture"]["id"]
    entry = conn.execute("SELECT work_item_id FROM entries WHERE capture_id=?", (capture_id,)).fetchone()
    reminder = conn.execute(
        "SELECT work_item_id FROM reminders WHERE entry_id IN (SELECT id FROM entries WHERE capture_id=?)",
        (capture_id,),
    ).fetchone()
    assert entry["work_item_id"] == item["id"]
    assert reminder["work_item_id"] == item["id"]


def test_reminder_only_classification_records_its_capture(client, conn, monkeypatch):
    """Dropping capture_id from reminder fan-out would make later Inbox resolve incomplete."""
    import json

    from app.services import classifier

    monkeypatch.setattr(
        classifier,
        "call_llm",
        lambda _prompt: json.dumps({
            "entries": [],
            "reminders": [{"text": "Remember this", "due_at": "2026-09-01T10:00:00"}],
            "clickup_actions": [],
            "gcal_events": [],
        }),
    )
    response = client.post("/api/capture", json={"text": "remind me"})

    assert response.status_code == 200
    capture_id = response.json()["capture"]["id"]
    assert conn.execute(
        "SELECT capture_id FROM reminders WHERE entry_id IS NULL"
    ).fetchone()["capture_id"] == capture_id


def test_global_capture_publishes_inbox_only_after_classification(client, monkeypatch):
    """Publishing before classification exposes an Inbox item that can resolve too early."""
    import threading

    from app import db
    from app.services import classifier

    classification_started = threading.Event()
    allow_classification_to_finish = threading.Event()
    response_holder = {}

    def classify_capture(*_args, **_kwargs):
        classification_started.set()
        assert allow_classification_to_finish.wait(timeout=2)
        return {"status": "classified", "entries": [], "reminders": [], "actions": []}

    monkeypatch.setattr(classifier, "classify_capture", classify_capture)

    def post_capture():
        response_holder["response"] = client.post("/api/capture", json={"text": "unassigned work"})

    thread = threading.Thread(target=post_capture)
    thread.start()
    assert classification_started.wait(timeout=2)
    observer = db.connect()
    try:
        assert observer.execute(
            "SELECT count(*) FROM captures WHERE raw_text='unassigned work'"
        ).fetchone()[0] == 1
        assert observer.execute("SELECT count(*) FROM work_inbox").fetchone()[0] == 0
    finally:
        observer.close()
        allow_classification_to_finish.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert response_holder["response"].status_code == 200
    observer = db.connect()
    try:
        assert observer.execute("SELECT count(*) FROM work_inbox").fetchone()[0] == 1
    finally:
        observer.close()


def test_capture_inbox_can_be_listed_resolved_and_dismissed(client, monkeypatch):
    """Inbox rows must remain pending until an explicit local resolution action."""
    from app.services import classifier

    monkeypatch.setattr(
        classifier,
        "classify_capture",
        lambda *args, **kwargs: {"status": "classified", "entries": [], "reminders": [], "actions": []},
    )
    item = client.post("/api/work-items", json={"title": "Pixel investigation"}).json()["work_item"]
    client.post("/api/capture", json={"text": "Pixel investigation"})
    client.post("/api/capture", json={"text": "unrelated thought"})

    listed = client.get("/api/work-inbox")
    assert listed.status_code == 200
    inbox = listed.json()["inbox"]
    assert len(inbox) == 2
    suggested = next(row for row in inbox if row["suggested_work_item_id"] == item["id"])
    unsuggested = next(row for row in inbox if row["suggested_work_item_id"] is None)
    assert suggested["confidence"] == 1.0

    resolved = client.post(f"/api/work-inbox/{suggested['id']}/resolve", json={"work_item_id": item["id"]})
    dismissed = client.post(f"/api/work-inbox/{unsuggested['id']}/dismiss")
    assert resolved.status_code == 200
    assert resolved.json()["inbox"]["status"] == "linked"
    assert dismissed.status_code == 200
    assert dismissed.json()["inbox"]["status"] == "dismissed"


def test_work_inbox_includes_capture_text_and_suggested_item_summary(client, conn):
    """Confident Inbox suggestions need enough context to choose without another request."""
    from app.services import work_inbox, work_items

    item = work_items.create_work_item(conn, title="Pixel investigation")
    capture_id = conn.execute(
        "INSERT INTO captures (raw_text, created_at) VALUES ('Pixel investigation blocker', 'now')"
    ).lastrowid
    conn.commit()
    inbox = work_inbox.suggest_capture_link(conn, capture_id, "Pixel investigation blocker")

    response = client.get("/api/work-inbox")

    assert response.status_code == 200
    row = response.json()["inbox"][0]
    assert row["id"] == inbox["id"]
    assert row["capture_text"] == "Pixel investigation blocker"
    assert row["suggested_work_item"] == {"id": item["id"], "title": "Pixel investigation"}
    assert row["suggested_work_item_id"] == item["id"]
    assert row["confidence"] == 1.0


def test_work_inbox_includes_capture_text_for_unmatched_rows(client, conn):
    """Unmatched Inbox rows retain their capture context and explicit null summary."""
    from app.services import work_inbox

    capture_id = conn.execute(
        "INSERT INTO captures (raw_text, created_at) VALUES ('An unrelated thought', 'now')"
    ).lastrowid
    conn.commit()
    inbox = work_inbox.suggest_capture_link(conn, capture_id, "An unrelated thought")

    response = client.get("/api/work-inbox")

    assert response.status_code == 200
    row = response.json()["inbox"][0]
    assert row["id"] == inbox["id"]
    assert row["capture_text"] == "An unrelated thought"
    assert row["suggested_work_item"] is None
    assert row["suggested_work_item_id"] is None
    assert row["confidence"] is None
