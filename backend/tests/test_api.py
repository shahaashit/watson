import json

import pytest

from app.services import classifier
from tests.conftest import seed_task
from tests.test_classifier import CANNED


def _capture(client, monkeypatch, text="met morgan about the iframe fix", canned=None):
    monkeypatch.setattr(
        classifier, "call_llm", lambda prompt: json.dumps(canned or CANNED)
    )
    return client.post("/api/capture", json={"text": text})


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_public_health_and_setup_snapshots_never_expose_secret_values(client, conn, monkeypatch):
    """Credentials are write-only even when the secret resolver has a value.

    Exercise every startup/settings response used by the Work OS rather than
    just the integration update route.  These are the easy places for a
    future response-shape refactor to accidentally serialize an effective
    credential.
    """
    from app.services import secret_store, user_meta

    sentinel = "watson-secret-leak-sentinel"
    monkeypatch.setattr(secret_store, "effective_secret", lambda *_args, **_kwargs: sentinel)
    monkeypatch.setattr(secret_store, "has_secret", lambda *_args, **_kwargs: True)
    user_meta.set_integration_health(conn, "clickup", {
        "status": "degraded",
        "message": "Cached work is available. Retry from Settings.",
    })

    responses = [
        client.get("/api/health"),
        client.get("/api/settings"),
        client.get("/api/onboarding"),
        client.get("/api/sync/status"),
        client.get("/api/today"),
    ]

    assert all(response.status_code == 200 for response in responses)
    assert all(sentinel not in response.text for response in responses)


def test_spa_work_route_returns_built_index(client, monkeypatch, tmp_path):
    from app import main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>Watson</main>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    monkeypatch.setattr(main, "_DIST", dist)

    response = client.get("/work/42", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == "<main>Watson</main>"
    assert client.get("/favicon.svg").text == "<svg></svg>"


def test_spa_fallback_does_not_mask_unknown_or_api_paths(client, monkeypatch, tmp_path):
    from app import main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>Watson</main>", encoding="utf-8")
    monkeypatch.setattr(main, "_DIST", dist)

    assert client.get("/unknown", headers={"accept": "text/html"}).status_code == 404
    assert client.get("/home", headers={"accept": "application/json"}).status_code == 404
    assert client.get("/api/not-a-route", headers={"accept": "text/html"}).status_code == 404
    assert client.get("/assets/missing.js").status_code == 404


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/", 200),
        ("/home///", 200),
        ("/team", 200),
        ("/work/42///", 200),
        ("/log", 200),
        ("/settings/integrations//", 200),
        ("/onboarding", 200),
        ("/settings/unknown", 404),
        ("/work/0", 404),
        ("/work/042", 404),
        ("/work/\u0664\u0662", 404),
        ("/work/1234567890123456", 404),
    ],
)
def test_spa_route_allowlist_matches_browser_route_policy(client, monkeypatch, tmp_path, path, status):
    from app import main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>Watson</main>", encoding="utf-8")
    monkeypatch.setattr(main, "_DIST", dist)

    response = client.get(path, headers={"accept": "text/html"})

    assert response.status_code == status


def test_spa_and_favicon_head_match_get_semantics(client, monkeypatch, tmp_path):
    from app import main

    dist = tmp_path / "dist"
    dist.mkdir()
    page = "<main>Watson</main>"
    icon = "<svg></svg>"
    (dist / "index.html").write_text(page, encoding="utf-8")
    (dist / "favicon.svg").write_text(icon, encoding="utf-8")
    monkeypatch.setattr(main, "_DIST", dist)

    work = client.head("/work/42//", headers={"accept": "text/html"})
    favicon = client.head("/favicon.svg")

    assert work.status_code == 200
    assert work.headers["content-type"].startswith("text/html")
    assert work.headers["content-length"] == str(len(page))
    assert work.content == b""
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert favicon.headers["content-length"] == str(len(icon))
    assert favicon.content == b""
    assert client.head("/settings/unknown", headers={"accept": "text/html"}).status_code == 404
    assert client.head("/work/42", headers={"accept": "application/json"}).status_code == 404


def test_spa_accepts_case_insensitive_html_media_type(client, monkeypatch, tmp_path):
    from app import main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>Watson</main>", encoding="utf-8")
    monkeypatch.setattr(main, "_DIST", dist)

    for method in (client.get, client.head):
        response = method("/work/42", headers={"accept": "TEXT/HTML"})
        assert response.status_code == 200


def test_existing_built_asset_supports_head(client):
    from app import main

    if not main._ASSETS.is_dir():
        pytest.skip("built assets are not available in this isolated test run")
    assets = [asset for asset in main._ASSETS.iterdir() if asset.is_file()]
    if not assets:
        pytest.skip("built assets are not available in this isolated test run")
    asset = assets[0]

    response = client.head(f"/assets/{asset.name}")

    assert response.status_code == 200
    assert response.headers["content-length"] == str(asset.stat().st_size)
    assert response.content == b""


def test_capture_fans_out(client, conn, monkeypatch):
    seed_task(conn)
    resp = _capture(client, monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["capture"]["status"] == "classified"
    assert body["result"]["status"] == "classified"
    assert len(body["result"]["entries"]) == 1
    assert body["result"]["actions"][0]["target_id"] == "abc123"

    # recent captures include the fan-out
    recent = client.get("/api/captures").json()["captures"]
    assert recent[0]["entries"][0]["title"].startswith("Discussed iframe")
    assert recent[0]["reminders"][0]["due_at"] == "2026-06-19T10:00:00"
    assert recent[0]["actions"][0]["status"] == "pending"


def test_capture_llm_failure_still_stores_raw(client, conn, monkeypatch, caplog):
    secret = "fake-anthropic-classifier-secret"
    def boom(prompt):
        raise RuntimeError(secret)
    monkeypatch.setattr(classifier, "call_llm", boom)
    resp = client.post("/api/capture", json={"text": "do not lose me"})
    assert resp.status_code == 200
    assert resp.json()["capture"]["status"] == "needs_review"
    assert resp.json()["capture"]["raw_text"] == "do not lose me"
    assert resp.json()["result"] == {
        "status": "needs_review",
        "error": {
            "source": "anthropic",
            "error_type": "RuntimeError",
            "error": "Classification failed. Capture saved for manual review.",
        },
    }
    stored = conn.execute(
        "SELECT classification_json FROM captures ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert stored["classification_json"] is None
    assert secret not in resp.text
    assert secret not in caplog.text


def test_entries_search_and_filters(client, conn, monkeypatch):
    _capture(client, monkeypatch)
    entries = client.get("/api/entries", params={"q": "z-index"}).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["capture_raw"] == "met morgan about the iframe fix"

    assert client.get("/api/entries", params={"type": "discussion"}).json()["entries"]
    assert not client.get("/api/entries", params={"type": "meeting"}).json()["entries"]
    assert client.get("/api/entries", params={"tag": "video"}).json()["entries"]
    assert not client.get("/api/entries", params={"tag": "nope"}).json()["entries"]


def test_reminder_done_and_snooze(client, conn, monkeypatch):
    _capture(client, monkeypatch)
    reminder = client.get("/api/reminders", params={"status": "pending"}).json()["reminders"][0]

    snoozed = client.post(
        f"/api/reminders/{reminder['id']}/snooze", json={"until": "2099-01-01T10:00:00"}
    ).json()["reminder"]
    assert snoozed["status"] == "snoozed"
    assert snoozed["snoozed_until"] == "2099-01-01T10:00:00"

    bad = client.post(f"/api/reminders/{reminder['id']}/snooze", json={"until": "???"})
    assert bad.status_code == 422

    done = client.post(f"/api/reminders/{reminder['id']}/done").json()["reminder"]
    assert done["status"] == "done"


def test_action_approve_executes_clickup_write(client, conn, monkeypatch):
    seed_task(conn)
    _capture(client, monkeypatch)
    action = client.get("/api/actions", params={"status": "pending"}).json()["actions"][0]

    calls = []
    from app.routers import actions as actions_router
    monkeypatch.setattr(
        actions_router.clickup_client, "execute_action",
        lambda kind, target_id, payload: calls.append((kind, target_id, payload)) or {"ok": True},
    )

    approved = client.post(f"/api/actions/{action['id']}/approve").json()["action"]
    assert approved["status"] == "executed"
    assert calls == [(
        "clickup_comment", "abc123",
        {"draft": "Discussed with Morgan; patch expected Friday.",
         "task_match_query": "iframe z-index",
         "related_task_id": None, "related_mr_id": None},
    )]

    # double-approve is rejected
    assert client.post(f"/api/actions/{action['id']}/approve").status_code == 409


def test_create_task_action_assigns_and_labels(client, conn, monkeypatch):
    seed_task(conn)
    # capture classified into a "create new task assigned to me" action
    canned = json.loads(json.dumps(CANNED))
    canned["reminders"] = []
    canned["clickup_actions"] = [{
        "kind": "clickup_create_task",
        "task_match_query": "iframe z-index",
        "suggested_task_id": "abc123",  # context only — must NOT become a write target
        "draft": {"name": "Review Morgan's iframe fix",
                  "description": "Related to: Fix iframe z-index on Playback player (abc123)",
                  "labels": ["review", "video"]},
    }]
    _capture(client, monkeypatch, canned=canned)

    action = client.get("/api/actions").json()["actions"][0]
    assert action["kind"] == "clickup_create_task"
    assert action["target_id"] is None  # never targets the existing task

    # configure ClickUp for the write path + intercept the actual call
    from app.config import settings
    monkeypatch.setattr(settings, "clickup_api_token", "pk_test")
    monkeypatch.setattr(settings, "clickup_create_list_id", "1234759")
    from app.routers import actions as actions_router
    cu = actions_router.clickup_client
    monkeypatch.setattr(cu, "current_user_id", lambda config=None: 617645)
    posts = []
    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append({"url": url, "json": json})
        class R:
            def raise_for_status(self): pass
            def json(self_inner): return {"id": "newtask1"}
        return R()
    monkeypatch.setattr(cu.requests, "post", fake_post)

    approved = client.post(f"/api/actions/{action['id']}/approve").json()["action"]
    assert approved["status"] == "executed"

    create = posts[0]
    assert "/list/1234759/task" in create["url"]          # the configured create list
    assert create["json"]["assignees"] == [617645]        # assigned to me
    assert create["json"]["tags"] == ["review", "video"]  # labels applied
    assert create["json"]["name"] == "Review Morgan's iframe fix"

    # second call links the new task to the matched task (context, not a mutation)
    link = posts[1]
    assert link["url"].endswith("/task/newtask1/link/abc123")


def test_action_approve_failure_marks_failed(client, conn, monkeypatch, caplog):
    seed_task(conn)
    _capture(client, monkeypatch)
    action = client.get("/api/actions").json()["actions"][0]

    from app.routers import actions as actions_router
    secret = "pk_clickup_action_credential_sentinel"
    def boom(kind, target_id, payload):
        raise RuntimeError(secret)
    monkeypatch.setattr(actions_router.clickup_client, "execute_action", boom)

    resp = client.post(f"/api/actions/{action['id']}/approve")
    assert resp.status_code == 502
    failed = client.get("/api/actions", params={"status": "failed"}).json()["actions"][0]
    assert failed["error"] == "ClickUp request failed. Check the integration and try again."
    assert resp.json()["detail"] == {
        "source": "clickup",
        "error_type": "RuntimeError",
        "error": "ClickUp request failed. Check the integration and try again.",
    }
    assert secret not in resp.text
    assert secret not in failed["error"]
    assert secret not in caplog.text


def test_direct_sync_routes_redact_external_exception_details(
    client, conn, monkeypatch, caplog
):
    from app.services import sync_pipeline, user_meta

    secret = "oauth-calendar-and-flock-credential-sentinel"
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: True)
    monkeypatch.setattr(
        sync_pipeline.flock_client,
        "sync",
        lambda _conn: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    monkeypatch.setattr(
        sync_pipeline.gcal_client,
        "sync",
        lambda _conn: (_ for _ in ()).throw(ValueError(secret)),
    )

    flock = client.post("/api/sync/flock")
    gcal = client.post("/api/sync/gcal")

    assert flock.status_code == 502
    assert flock.json()["detail"] == {
        "source": "flock",
        "error": "Flock request failed. Reconnect the integration and try again.",
    }
    assert gcal.status_code == 502
    assert gcal.json()["detail"] == {
        "source": "google-calendar",
        "error": "Google Calendar request failed. Reconnect the integration and try again.",
    }
    health = {item["source"]: item for item in user_meta.integration_health(conn)}
    assert health["flock"]["status"] == "degraded"
    assert health["google-calendar"]["status"] == "degraded"
    assert secret not in flock.text + gcal.text
    assert secret not in caplog.text


def test_other_live_external_routes_use_the_same_redacted_boundary(
    client, monkeypatch, caplog
):
    from app.routers import ask, clickup, gitlab, today

    secret = "external-route-credential-sentinel"

    def fail(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        clickup.sync_pipeline.clickup_client, "refresh_exact_tasks", fail
    )
    monkeypatch.setattr(gitlab.sync_pipeline.gitlab_client, "configured", lambda: True)
    monkeypatch.setattr(gitlab.sync_pipeline.gitlab_client, "sync", fail)
    monkeypatch.setattr(ask.asker, "answer_question", fail)
    monkeypatch.setattr(today.gcal_client, "events_for_date", fail)

    responses = [
        client.post("/api/sync/clickup"),
        client.post("/api/sync/gitlab"),
        client.post("/api/ask", json={"question": "what changed?"}),
        client.get("/api/meetings?date=2026-08-28"),
    ]

    assert [response.status_code for response in responses] == [502, 502, 502, 200]
    assert responses[0].json()["detail"]["source"] == "clickup"
    assert responses[1].json()["detail"]["source"] == "gitlab"
    assert responses[2].json()["detail"]["source"] == "anthropic"
    assert responses[3].json()["error"]["source"] == "google-calendar"
    assert all(secret not in response.text for response in responses)
    assert secret not in caplog.text


def test_action_edit_then_reject(client, conn, monkeypatch):
    seed_task(conn)
    _capture(client, monkeypatch)
    action = client.get("/api/actions").json()["actions"][0]

    edited = client.patch(
        f"/api/actions/{action['id']}",
        json={"payload": {"draft": "Reworded comment."}, "target_id": "abc123"},
    ).json()["action"]
    assert edited["payload"]["draft"] == "Reworded comment."
    assert edited["target_id"] == "abc123"
    assert edited["match_confidence"] == 1.0

    rejected = client.post(f"/api/actions/{action['id']}/reject").json()["action"]
    assert rejected["status"] == "rejected"
    assert client.patch(f"/api/actions/{action['id']}", json={"payload": {}}).status_code == 409


def _create_task_canned(title):
    canned = json.loads(json.dumps(CANNED))
    canned["reminders"] = []
    canned["clickup_actions"] = [{
        "kind": "clickup_create_task", "task_match_query": "", "suggested_task_id": None,
        "draft": {"name": title, "description": "", "labels": ["Work"]},
    }]
    return canned


def test_bulk_approve_records_managed_tasks(client, conn, monkeypatch):
    from app.config import settings
    from app.routers import actions as ar
    monkeypatch.setattr(settings, "clickup_api_token", "pk_test")
    monkeypatch.setattr(settings, "clickup_create_list_id", "1234759")

    _capture(client, monkeypatch, text="did A", canned=_create_task_canned("Work - A"))
    _capture(client, monkeypatch, text="did B", canned=_create_task_canned("Work - B"))
    ids = [a["id"] for a in client.get("/api/actions").json()["actions"]]
    assert len(ids) == 2

    # stub the ClickUp write; each create returns a distinct task id
    created = iter(["task_A", "task_B"])
    monkeypatch.setattr(ar.clickup_client, "execute_action",
                        lambda kind, target, payload: {"id": next(created)})

    resp = client.post("/api/actions/bulk", json={"op": "approve", "ids": ids}).json()
    assert set(resp["executed"]) == set(ids)
    assert resp["failed"] == []
    # nothing left pending
    assert client.get("/api/actions", params={"status": "pending"}).json()["actions"] == []
    # both created tasks are now tracked as managed
    managed = conn.execute("SELECT clickup_task_id FROM managed_tasks ORDER BY id").fetchall()
    assert {m["clickup_task_id"] for m in managed} == {"task_A", "task_B"}


def test_bulk_reject(client, conn, monkeypatch):
    _capture(client, monkeypatch, text="x", canned=_create_task_canned("Work - X"))
    _capture(client, monkeypatch, text="y", canned=_create_task_canned("Work - Y"))
    ids = [a["id"] for a in client.get("/api/actions").json()["actions"]]
    resp = client.post("/api/actions/bulk", json={"op": "reject", "ids": ids}).json()
    assert set(resp["rejected"]) == set(ids)
    assert client.get("/api/actions", params={"status": "pending"}).json()["actions"] == []
    assert len(client.get("/api/actions", params={"status": "rejected"}).json()["actions"]) == 2


def test_close_task_action_marks_managed_closed(client, conn, monkeypatch):
    from app.routers import actions as ar
    from app.config import settings
    monkeypatch.setattr(settings, "clickup_api_token", "pk_test")
    # a managed task + a pending close action targeting it
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, related_clickup_task_id, status, created_at)"
        " VALUES ('MYTASK', 'R1', 'open', '2026-06-15T10:00:00')")
    cur = conn.execute(
        "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json, status, created_at)"
        " VALUES (NULL, 'clickup_close_task', 'MYTASK', '{\"draft\":\"close it\"}', 'pending', '2026-06-15T10:00:00')")
    conn.commit()
    action_id = cur.lastrowid
    monkeypatch.setattr(ar.clickup_client, "execute_action", lambda k, t, p: {"id": t})

    approved = client.post(f"/api/actions/{action_id}/approve").json()["action"]
    assert approved["status"] == "executed"
    assert conn.execute("SELECT status FROM managed_tasks WHERE clickup_task_id='MYTASK'").fetchone()["status"] == "closed"


def test_today_aggregation(client, conn, monkeypatch):
    seed_task(conn)
    # overdue reminder via canned classification
    canned = json.loads(json.dumps(CANNED))
    canned["reminders"][0]["due_at"] = "2020-01-01T10:00:00"
    _capture(client, monkeypatch, canned=canned)

    # stale reviewer MR + fresh one
    conn.execute(
        "INSERT INTO gitlab_mrs_cache"
        " (mr_id, project, title, state, url, role, author, updated_at, synced_at) VALUES"
        " ('1!10', 'grp/proj', 'Old MR', 'opened', 'http://x', 'reviewer', 'Morgan',"
        "  '2020-01-01T00:00:00', '2026-06-12T09:00:00'),"
        " ('1!11', 'grp/proj', 'Fresh MR', 'opened', 'http://y', 'reviewer', 'Priya',"
        "  '2099-01-01T00:00:00', '2026-06-12T09:00:00')"
    )
    conn.commit()

    today = client.get("/api/today").json()
    assert len(today["reminders_due"]) == 1
    assert [m["title"] for m in today["stale_mrs"]] == ["Old MR"]
    assert today["pending_count"] == 1


def test_daily_export_writes_markdown(client, conn, monkeypatch):
    from datetime import date
    from app.services import exporter

    _capture(client, monkeypatch)
    path = exporter.export_day(conn, date.today())
    content = path.read_text()
    assert "met morgan about the iframe fix" in content
    assert "Discussed iframe z-index fix with Morgan" in content


def test_entries_nest_followups(client, conn, monkeypatch):
    # parent capture
    _capture(client, monkeypatch)
    parent_id = client.get("/api/entries").json()["entries"][0]["id"]

    # follow-up capture that threads under the parent
    followup = json.loads(json.dumps(CANNED))
    followup["entries"][0]["title"] = "Morgan's patch merged"
    followup["entries"][0]["follows_up_entry_id"] = parent_id
    followup["reminders"] = []
    _capture(client, monkeypatch, text="morgan merged it", canned=followup)

    entries = client.get("/api/entries").json()["entries"]
    # only the parent shows at top level, with the follow-up nested
    titles = [e["title"] for e in entries]
    assert "Morgan's patch merged" not in titles
    parent = next(e for e in entries if e["id"] == parent_id)
    assert [f["title"] for f in parent["follow_ups"]] == ["Morgan's patch merged"]

    # thread endpoint returns root + follow-ups regardless of which id is asked
    child_id = parent["follow_ups"][0]["id"]
    thread = client.get(f"/api/entries/{child_id}/thread").json()["thread"]
    assert thread["id"] == parent_id
    assert thread["follow_ups"][0]["id"] == child_id

    # searching finds the follow-up flat
    found = client.get("/api/entries", params={"q": "merged"}).json()["entries"]
    assert any(e["title"] == "Morgan's patch merged" for e in found)


def test_delete_entry_cascades_thread_and_reminders(client, conn, monkeypatch):
    # parent capture (CANNED also creates a reminder)
    _capture(client, monkeypatch)
    parent_id = client.get("/api/entries").json()["entries"][0]["id"]

    # follow-up threaded under the parent
    followup = json.loads(json.dumps(CANNED))
    followup["entries"][0]["title"] = "threaded update"
    followup["entries"][0]["follows_up_entry_id"] = parent_id
    followup["reminders"] = []
    _capture(client, monkeypatch, text="update", canned=followup)
    child_id = client.get(f"/api/entries/{parent_id}/thread").json()["thread"]["follow_ups"][0]["id"]

    assert client.get("/api/reminders").json()["reminders"]  # at least one exists

    # deleting the parent removes the parent, the follow-up, and the reminders
    resp = client.request("DELETE", f"/api/entries/{parent_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert set(resp.json()["ids"]) == {parent_id, child_id}

    remaining = [e["id"] for e in client.get("/api/entries").json()["entries"]]
    assert parent_id not in remaining and child_id not in remaining
    assert client.get("/api/reminders").json()["reminders"] == []

    # deleting a missing entry 404s
    assert client.request("DELETE", f"/api/entries/{parent_id}").status_code == 404


def test_search_returns_results_across_kinds(client, conn, monkeypatch):
    # seed one of each kind that should match "iframe"
    conn.execute(
        "INSERT INTO captures (id, raw_text, created_at) VALUES (101,"
        " 'caught the iframe bug live', '2026-06-15T10:00:00')"
    )
    conn.execute(
        "INSERT INTO entries (id, capture_id, type, title, body, people, tags, created_at)"
        " VALUES (202, 101, 'note', 'Reviewed iframe MR', '', '[]', '[]', '2026-06-15T10:00:00')"
    )
    conn.execute(
        "INSERT INTO clickup_tasks_cache"
        " (task_id, name, status, status_type, list_name, url, assignees, due_date, synced_at)"
        " VALUES ('CT1', 'iframe z-index fix on Playback', 'open', 'open', 'CM', 'http://c', '[]', NULL, '2026-06-15')"
    )
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role, author,"
        " source_branch, updated_at, synced_at) VALUES ('9!42', 'cm/x', 'Fix iframe',"
        " 'opened', 'http://g', 'author', 'me', 'feat/iframe', '2026-06-14T09:00:00',"
        " '2026-06-15T09:00:00')"
    )
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, capture_id, related_clickup_task_id,"
        " category, status, created_at) VALUES ('CT1', 101, NULL, 'Review', 'open',"
        " '2026-06-15T10:00:00')"
    )
    conn.commit()

    by_kind = {}
    for r in client.get("/api/search", params={"q": "iframe"}).json()["results"]:
        by_kind.setdefault(r["kind"], []).append(r)
    assert set(by_kind) == {"entry", "capture", "clickup_task", "mr"}
    assert by_kind["mr"][0]["url"] == "http://g"

    # empty query returns nothing
    assert client.get("/api/search", params={"q": ""}).json()["results"] == []


def test_ask_endpoint(client, conn, monkeypatch):
    _capture(client, monkeypatch)
    eid = client.get("/api/entries").json()["entries"][0]["id"]
    monkeypatch.setattr(
        classifier, "call_llm",
        lambda prompt: f"You discussed the iframe z-index fix with Morgan (#{eid}).",
    )
    resp = client.post("/api/ask", json={"question": "what did I do today?"})
    assert resp.status_code == 200
    body = resp.json()
    assert "Morgan" in body["answer"]
    assert [e["id"] for e in body["entries"]] == [eid]
