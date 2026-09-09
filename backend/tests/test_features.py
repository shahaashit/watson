"""Tests for self-attribution context, follow-up threading, and ask mode."""
import json
from datetime import datetime, timedelta

import pytest

from app.services import asker, classifier, clickup_client, completion, events, gitlab_client, link_proposer, merge_proposer, mr_review_tracker, orphan_detector, review_groups, sync_pipeline, work


def _insert_capture(conn, text="note"):
    cur = conn.execute(
        "INSERT INTO captures (raw_text, created_at) VALUES (?, '2026-06-10T09:00:00')",
        (text,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_entry(conn, title, type="discussion", tags=None, body="", parent=None,
                  people=None, created="2026-06-10T09:00:00"):
    cur = conn.execute(
        "INSERT INTO entries (capture_id, parent_entry_id, type, title, body, people,"
        " tags, created_at) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
        (parent, type, title, body, json.dumps(people or []),
         json.dumps(tags or []), created),
    )
    conn.commit()
    return cur.lastrowid


# ── follow-up threading ─────────────────────────────────────────────────

def _canned(follows_up=None, title="Patch landed", tags=None):
    return {
        "entries": [{
            "type": "status_update",
            "title": title,
            "body": "Morgan's patch is merged.",
            "people": ["Morgan"],
            "tags": tags or ["follow-up", "video"],
            "follows_up_entry_id": follows_up,
        }],
        "reminders": [],
        "clickup_actions": [],
    }


class TestFollowupThreading:
    def test_threads_under_existing_entry(self, conn, monkeypatch):
        parent = _insert_entry(conn, "Discussed iframe z-index fix with Morgan")
        monkeypatch.setattr(classifier, "call_llm",
                            lambda p: json.dumps(_canned(follows_up=parent)))
        cid = _insert_capture(conn, "morgan's patch merged")
        result = classifier.classify_capture(conn, cid, "morgan's patch merged")

        child = result["entries"][0]
        assert child["parent_entry_id"] == parent
        assert child["parent_title"] == "Discussed iframe z-index fix with Morgan"
        row = conn.execute("SELECT parent_entry_id FROM entries WHERE id = ?",
                           (child["id"],)).fetchone()
        assert row["parent_entry_id"] == parent

    def test_hallucinated_parent_id_ignored(self, conn, monkeypatch):
        monkeypatch.setattr(classifier, "call_llm",
                            lambda p: json.dumps(_canned(follows_up=99999)))
        cid = _insert_capture(conn)
        result = classifier.classify_capture(conn, cid, "x")
        assert result["entries"][0]["parent_entry_id"] is None

    def test_recent_entries_in_prompt(self, conn):
        _insert_entry(conn, "Reviewed Priya's caching MR", tags=["review"])
        prompt = classifier.build_prompt(conn, "follow up", today=None)
        assert "Reviewed Priya's caching MR" in prompt
        assert "User" in prompt  # default owner attribution
        assert "{recent_entries}" not in prompt


# ── ask mode ────────────────────────────────────────────────────────────

class TestAsk:
    def test_context_includes_entries_and_threads(self, conn):
        root = _insert_entry(conn, "Reviewed Morgan's iframe MR", tags=["review"])
        _insert_entry(conn, "Morgan revised, re-reviewed", parent=root, tags=["follow-up"])
        prompt = asker.build_prompt(conn, "what did I review?")
        assert "Reviewed Morgan's iframe MR" in prompt
        assert "Morgan revised, re-reviewed" in prompt
        assert "↳" in prompt  # follow-up rendered nested
        assert "what did I review?" in prompt

    def test_answer_extracts_cited_ids(self, conn, monkeypatch):
        _insert_entry(conn, "Reviewed Morgan's iframe MR")
        monkeypatch.setattr(classifier, "call_llm",
                            lambda p: "You reviewed Morgan's MR (#1) and synced with Priya (#1, #3).")
        result = asker.answer_question(conn, "what did I do?")
        assert result["entry_ids"] == [1, 3]
        assert "reviewed" in result["answer"].lower()


# ── completion sync (linked task closed → draft close) ──────────────────

def _managed(conn, my_task="MYTASK", related="R1", status="open"):
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, capture_id, related_clickup_task_id,"
        " category, status, created_at) VALUES (?, NULL, ?, 'Discussion', ?, '2026-06-15T10:00:00')",
        (my_task, related, status),
    )
    conn.commit()


class TestCompletionSync:
    def test_drafts_close_when_linked_task_done(self, conn, monkeypatch):
        _managed(conn)
        monkeypatch.setattr(clickup_client, "get_task",
                            lambda tid: {"name": "Related work", "status": {"status": "closed", "type": "closed"}})
        assert completion.check_completions(conn) == 1
        act = conn.execute(
            "SELECT target_id, status FROM pending_actions WHERE kind = 'clickup_close_task'"
        ).fetchone()
        assert act["target_id"] == "MYTASK"
        assert act["status"] == "pending"
        # dedup — a second run doesn't draft another
        assert completion.check_completions(conn) == 0

    def test_no_draft_when_linked_task_open(self, conn, monkeypatch):
        _managed(conn, my_task="MYTASK2", related="R2")
        monkeypatch.setattr(clickup_client, "get_task",
                            lambda tid: {"name": "R", "status": {"status": "in progress", "type": "open"}})
        assert completion.check_completions(conn) == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM pending_actions").fetchone()["n"] == 0


# ── ask.py: date_closed + recent_activity in the prompt ────────────────

class TestAskContext:
    def test_render_clickup_tasks_includes_date_closed(self, conn):
        # a managed_task pointing at an open cached task
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, category, status, created_at)"
            " VALUES ('MINE', NULL, 'REL', 'Discussion', 'open', '2026-07-01')"
        )
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, date_closed, synced_at)"
            " VALUES ('REL', 'Fix iframe z-index', 'Closed', 'closed', 'X',"
            " 'http://x', '[]', NULL, '2026-07-05T14:20:00', '2026-07-07T10:00:00')"
        )
        conn.commit()
        rendered = asker._render_clickup_tasks(conn)
        assert "closed_at: 2026-07-05T14:20:00" in rendered
        assert "Fix iframe z-index" in rendered

    def test_render_recent_activity_from_system_events(self, conn):
        recent = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO system_events (kind, subject, related_task_id, created_at)"
            " VALUES ('approval', 'Approved: Discussion - Foo', 'T1', ?)",
            (recent,),
        )
        conn.commit()
        rendered = asker._render_recent_activity(conn)
        assert "approval" in rendered
        assert "Approved: Discussion - Foo" in rendered
        assert "task_id: T1" in rendered


# ── system_events + activity Log ───────────────────────────────────────

class TestSystemEvents:
    def test_review_proposal_emits_event(self, conn):
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
            " author, source_branch, updated_at, synced_at)"
            " VALUES ('1!42', 'grp/p', 'Fix foo', 'opened', 'http://x', 'reviewer',"
            "         'morgan', '', '2026-06-15T10:00:00', '2026-06-15T10:00:00')"
        )
        conn.commit()
        mr_review_tracker.propose_review_tasks(conn)
        row = conn.execute(
            "SELECT kind, subject, related_mr_id FROM system_events ORDER BY id DESC"
        ).fetchone()
        assert row["kind"] == "proposal"
        assert "Fix foo" in row["subject"]
        assert row["related_mr_id"] == "1!42"

    def test_approval_emits_event(self, conn, monkeypatch):
        # a minimal drafted create-task action that we approve
        conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " status, created_at) VALUES (NULL, 'clickup_create_task', NULL,"
            " '{\"draft\":{\"name\":\"Review - Fix foo\"}}', 'pending', '2026-06-15')"
        )
        conn.commit()
        # short-circuit the external write
        monkeypatch.setattr(clickup_client, "execute_action",
                            lambda kind, target, payload: {"id": "NEW1"})
        monkeypatch.setattr(completion, "record_managed_task",
                            lambda *a, **kw: None)
        monkeypatch.setattr(clickup_client, "refresh_tasks",
                            lambda conn, ids: None)
        from app.routers import actions as actions_router
        row = conn.execute("SELECT * FROM pending_actions").fetchone()
        actions_router._execute_one(conn, row)

        ev = conn.execute(
            "SELECT kind, subject FROM system_events WHERE kind = 'approval'"
        ).fetchone()
        assert ev is not None
        assert "Approved" in ev["subject"]
        assert "Fix foo" in ev["subject"]

    def test_unified_log_endpoint_merges_by_created_at(self, client, conn):
        # insert an entry AND an event with distinct timestamps
        conn.execute(
            "INSERT INTO entries (capture_id, type, title, body, tags, created_at)"
            " VALUES (NULL, 'discussion', 'Talk with Morgan', '', '[]',"
            " '2026-06-20T09:00:00')"
        )
        conn.execute(
            "INSERT INTO system_events (kind, subject, created_at)"
            " VALUES ('sync', 'Sync — 40 MRs', '2026-06-20T10:00:00')"
        )
        conn.commit()
        r = client.get("/api/log").json()
        items = r["items"]
        assert len(items) == 2
        assert items[0]["source"] == "event"  # newer created_at first
        assert items[1]["source"] == "entry"
        assert set(r["kinds"]) == {"sync", "discussion"}

    def test_unified_log_source_filter(self, client, conn):
        conn.execute(
            "INSERT INTO entries (capture_id, type, title, body, tags, created_at)"
            " VALUES (NULL, 'note', 'a note', '', '[]', '2026-06-20T09:00:00')"
        )
        conn.execute(
            "INSERT INTO system_events (kind, subject, created_at)"
            " VALUES ('proposal', 'Drafted X', '2026-06-20T10:00:00')"
        )
        conn.commit()
        assert [i["source"] for i in client.get("/api/log?source=event").json()["items"]] == ["event"]
        assert [i["source"] for i in client.get("/api/log?source=entry").json()["items"]] == ["entry"]


# ── run_sync_pipeline: what "Sync now" and job_sync both call ──────────

class TestSyncPipeline:
    def test_full_pipeline_in_correct_order(self, conn, monkeypatch):
        """Independent caches refresh first, then linked work is reconciled
        before legacy detectors and proposers read it."""
        calls = []
        def record(name, retval=0):
            def _f(_c): calls.append(name); return retval
            return _f

        # GitLab discovery creates local links that determine the exact ClickUp
        # IDs. A broad ClickUp list scan must never return to this pipeline.
        monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: True)
        monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: True)
        monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: False)
        monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: False)
        monkeypatch.setattr(
            sync_pipeline.clickup_client,
            "sync",
            lambda _c: pytest.fail("the broad ClickUp scan must not run"),
        )
        monkeypatch.setattr(sync_pipeline.gitlab_client, "sync", record("gitlab_sync", 17))
        monkeypatch.setattr(
            sync_pipeline.work_ingestion,
            "ingest_cached_mrs",
            record("ingest_mrs", {"created": 1, "updated": 0}),
        )
        monkeypatch.setattr(
            sync_pipeline.work_ingestion,
            "retire_terminal_mr_work",
            record("retire_terminal_mr_work", 1),
        )
        monkeypatch.setattr(
            sync_pipeline.work_ingestion,
            "linked_clickup_ids",
            record("linked_clickup_ids", ["86abc1234"]),
        )
        monkeypatch.setattr(
            sync_pipeline.clickup_client,
            "refresh_exact_tasks",
            lambda _c, ids: calls.append(("clickup_exact", tuple(ids)))
            or {"updated": 1, "failed": 0, "skipped": 0},
        )
        monkeypatch.setattr(
            sync_pipeline.work_ingestion,
            "reconcile_clickup_titles",
            record("reconcile_titles", 1),
        )
        monkeypatch.setattr(
            sync_pipeline.settings_service,
            "profile",
            lambda _c: {
                "auto_create_review_tasks": True,
                "ai_group_review_mrs": False,
            },
        )
        monkeypatch.setattr(
            sync_pipeline.review_automation,
            "run",
            lambda _c, **_kwargs: calls.append("review_automation")
            or {"created": 2},
        )
        monkeypatch.setattr(sync_pipeline.work, "backfill_managed_tasks", record("backfill", {"added": 0}))
        monkeypatch.setattr(sync_pipeline.work, "backfill_related_clickup_ids", record("relink", 0))
        monkeypatch.setattr(sync_pipeline.completion, "check_completions", record("completions", 1))
        monkeypatch.setattr(sync_pipeline.orphan_detector, "detect_orphans", record("orphans", 0))
        monkeypatch.setattr(sync_pipeline.link_proposer, "propose_mr_task_links", record("propose_links", 0))

        summary = sync_pipeline.run_sync_pipeline(conn)
        assert calls == [
            "gitlab_sync",
            "ingest_mrs",
            "retire_terminal_mr_work",
            "linked_clickup_ids",
            ("clickup_exact", ("86abc1234",)),
            "reconcile_titles",
            "review_automation",
            "backfill", "relink",                    # adopt + repair links
            "completions", "orphans",                # then detectors
            "propose_links",                          # then remaining proposer
        ]
        assert "clickup_sync" not in summary
        assert summary["clickup_exact"] == {"updated": 1, "failed": 0, "skipped": 0}
        assert summary["gitlab_sync"] == 17
        assert summary["review_automation"] == {"created": 2}

    def test_step_failure_does_not_abort_pipeline(self, conn, monkeypatch, caplog):
        monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: True)
        monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: False)
        monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: False)
        monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: False)
        secret = "pk_live_sync_credential_sentinel"
        def boom(_c, _ids): raise RuntimeError(secret)
        monkeypatch.setattr(sync_pipeline.work_ingestion, "ingest_cached_mrs", lambda _c: {"created": 0, "updated": 0})
        monkeypatch.setattr(sync_pipeline.work_ingestion, "linked_clickup_ids", lambda _c: ["86abc1234"])
        monkeypatch.setattr(sync_pipeline.clickup_client, "refresh_exact_tasks", boom)
        monkeypatch.setattr(sync_pipeline.work_ingestion, "reconcile_clickup_titles", lambda _c: 0)
        monkeypatch.setattr(sync_pipeline.work, "backfill_managed_tasks", lambda _c: {"added": 0})
        monkeypatch.setattr(sync_pipeline.work, "backfill_related_clickup_ids", lambda _c: 0)
        monkeypatch.setattr(sync_pipeline.completion, "check_completions", lambda _c: 5)
        monkeypatch.setattr(sync_pipeline.orphan_detector, "detect_orphans", lambda _c: 0)

        summary = sync_pipeline.run_sync_pipeline(conn)
        assert summary["clickup_exact"] == {
            "source": "clickup",
            "error": "ClickUp request failed. Check the integration and try again.",
        }
        assert summary["completions"] == 5   # kept running
        event = conn.execute(
            "SELECT details_json FROM system_events WHERE kind='sync' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event is not None
        assert secret not in event["details_json"]
        assert "error_type" not in event["details_json"]
        assert secret not in caplog.text

    def test_manual_and_scheduled_sync_share_the_same_pipeline(self, client, monkeypatch):
        from app import main

        calls = []

        def pipeline(_conn):
            calls.append("pipeline")
            return {"review_automation": {"created": 0}}

        monkeypatch.setattr(sync_pipeline, "run_sync_pipeline", pipeline)
        monkeypatch.setattr(main, "_with_conn", lambda fn: fn(None))

        response = client.post("/api/sync")
        main.job_sync()

        assert response.json() == {"review_automation": {"created": 0}}
        assert calls == ["pipeline", "pipeline"]


# ── close_task: pick the right terminal status ─────────────────────────

class TestClickUpClosedStatus:
    def test_prefers_type_closed_over_done(self, monkeypatch):
        # Same shape as a ClickUp list: 'abandoned' (done) comes before
        # 'Closed' (closed) in orderindex. Old code picked 'abandoned' — new
        # code must prefer type=closed regardless of order.
        class _R:
            def raise_for_status(self): pass
            def json(self):
                return {"statuses": [
                    {"status": "Open", "type": "open", "orderindex": 0},
                    {"status": "abandoned", "type": "done", "orderindex": 17},
                    {"status": "archived", "type": "done", "orderindex": 18},
                    {"status": "Closed", "type": "closed", "orderindex": 19},
                ]}
        monkeypatch.setattr(clickup_client.requests, "get",
                            lambda *a, **kw: _R())
        assert clickup_client._closed_status_name(
            "L1", {"token": "test-token", "list_ids": (), "create_list_id": ""}
        ) == "Closed"

    def test_falls_back_to_done_when_no_closed(self, monkeypatch):
        class _R:
            def raise_for_status(self): pass
            def json(self):
                return {"statuses": [
                    {"status": "Open", "type": "open", "orderindex": 0},
                    {"status": "Complete", "type": "done", "orderindex": 1},
                ]}
        monkeypatch.setattr(clickup_client.requests, "get",
                            lambda *a, **kw: _R())
        assert clickup_client._closed_status_name(
            "L1", {"token": "test-token", "list_ids": (), "create_list_id": ""}
        ) == "Complete"


# ── branch-name parsing ─────────────────────────────────────────────────

class TestBranchParse:
    def test_typical_branch_formats(self):
        cases = [
            ("feature/foo_clickup_86d3by9x6",  "86d3by9x6"),
            ("bugfix/_clickup-86abc1234/notes", "86abc1234"),
            ("taylor.dev/_clickup86d2unu7y",     "86d2unu7y"),
            ("clickup_86xyz/feature",          None),  # no leading underscore
            ("feature/no-tag-here",            None),
            ("",                               None),
            (None,                             None),
        ]
        for branch, expected in cases:
            assert gitlab_client.clickup_id_from_branch(branch) == expected, branch


class TestRelatedClickupBackfill:
    def _seed(self, conn, mr_id, branch, description, task_in_cache=None):
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
            " author, source_branch, description, updated_at, synced_at)"
            " VALUES (?, 'g/p', 't', 'opened', 'http://x', 'reviewer', 'a', ?, ?,"
            " '2026-07-01', '2026-07-01')",
            (mr_id, branch, description),
        )
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('MINE', NULL, NULL, ?, 'Review', 'open', '2026-07-01')",
            (mr_id,),
        )
        if task_in_cache:
            conn.execute(
                "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
                " list_name, url, assignees, due_date, synced_at)"
                " VALUES (?, 'X', 'Open', 'open', '', '', '[]', NULL, '2026-07-01')",
                (task_in_cache,),
            )
        conn.commit()

    def test_backfill_uses_description_when_branch_typoed(self, conn):
        self._seed(conn,
                   "1!100",
                   "double_clickyp86d3m9u6j",  # typo in tag
                   "See https://app.clickup.com/t/606026/86d3m9u6j",
                   task_in_cache="86d3m9u6j")
        assert work.backfill_related_clickup_ids(conn) == 1
        row = conn.execute("SELECT related_clickup_task_id FROM managed_tasks").fetchone()
        assert row["related_clickup_task_id"] == "86d3m9u6j"

    def test_backfill_rejects_ids_not_in_clickup_cache(self, conn):
        # description points at a task id that isn't in our ClickUp cache —
        # could be noise, so we don't blindly trust it
        self._seed(conn,
                   "1!101",
                   "no_tag_here",
                   "https://app.clickup.com/t/86nonexist",
                   task_in_cache=None)
        assert work.backfill_related_clickup_ids(conn) == 0

    def test_backfill_is_idempotent(self, conn):
        self._seed(conn,
                   "1!102",
                   "double_clickyp86d3m9u6j",
                   "See https://app.clickup.com/t/606026/86d3m9u6j",
                   task_in_cache="86d3m9u6j")
        assert work.backfill_related_clickup_ids(conn) == 1
        assert work.backfill_related_clickup_ids(conn) == 0  # already filled


class TestDescriptionParse:
    def test_extracts_clickup_url_from_description(self):
        cases = [
            # bare URL
            ("Closes https://app.clickup.com/t/86d3fux03", "86d3fux03"),
            # workspace BEFORE /t/  (older ClickUp shape)
            ("https://app.clickup.com/9012345/t/86abc1234 -- please review", "86abc1234"),
            # workspace AFTER /t/  (newer shape — this is what real MRs have)
            ("### [ClickUp Task](https://app.clickup.com/t/606026/86d3m9u6j)", "86d3m9u6j"),
            ("no link here", None),
            ("", None),
            (None, None),
        ]
        for body, expected in cases:
            assert gitlab_client.clickup_id_from_description(body) == expected, body

    def test_clickup_id_from_mr_uses_branch_first(self):
        # branch tag wins even when the description also has a link
        mr = {"source_branch": "feature/_clickup_86abc1234",
              "description": "Closes https://app.clickup.com/t/86zzzzzzz"}
        assert gitlab_client.clickup_id_from_mr(mr) == "86abc1234"

    def test_clickup_id_from_mr_falls_back_to_description(self):
        # typo in branch tag → description saves us
        mr = {"source_branch": "feature/_clickvp_86abc1234",
              "description": "Related task: https://app.clickup.com/t/86xyz7890"}
        assert gitlab_client.clickup_id_from_mr(mr) == "86xyz7890"

    def test_clickup_id_from_mr_tolerates_missing_keys(self):
        assert gitlab_client.clickup_id_from_mr({}) is None


# ── GitLab events → engaged MR ids ─────────────────────────────────────

class TestEngagementParse:
    def test_parses_comment_and_approval_events(self, monkeypatch):
        # Two pages of events: comments via Note + an approval via MergeRequest.
        # `pushed to` should NOT count (too noisy: pushes from CI / branch sync).
        pages = {
            1: [
                {  # comment on MR !4373 in project 14020
                    "action_name": "commented on", "target_type": "Note",
                    "project_id": 14020,
                    "note": {"noteable_type": "MergeRequest", "noteable_iid": 4373},
                },
                {  # comment on MR !4372 in project 14020
                    "action_name": "commented on", "target_type": "Note",
                    "project_id": 14020,
                    "note": {"noteable_type": "MergeRequest", "noteable_iid": 4372},
                },
                {  # approval — should count
                    "action_name": "approved", "target_type": "MergeRequest",
                    "project_id": 7, "target_iid": 99,
                },
                {  # comment on an Issue — should NOT count
                    "action_name": "commented on", "target_type": "Note",
                    "project_id": 14020,
                    "note": {"noteable_type": "Issue", "noteable_iid": 1},
                },
                {  # branch push event — should NOT count
                    "action_name": "pushed to", "target_type": None,
                    "project_id": 14020,
                },
            ],
        }

        class _FakeResp:
            def __init__(self, body): self._body = body
            def raise_for_status(self): pass
            def json(self): return self._body

        def fake_get(url, headers=None, params=None, timeout=None):
            if url.endswith("/user"):
                return _FakeResp({"id": 649, "username": "taylor.dev"})
            page = (params or {}).get("page", 1)
            return _FakeResp(pages.get(page, []))

        monkeypatch.setattr(gitlab_client.requests, "get", fake_get)
        ids = gitlab_client.fetch_engaged_mr_ids(
            "2026-06-10",
            {
                "base_url": "https://gitlab.example.com",
                "username": "",
                "token": "test-token",
            },
        )
        assert ids == {"14020!4373", "14020!4372", "7!99"}


# ── MR-merge → close my task (Phase 2) ──────────────────────────────────

def _seed_mr(conn, mr_id, title, state, source_branch):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
        " author, source_branch, updated_at, synced_at)"
        " VALUES (?, 'grp/proj', ?, ?, 'http://x', 'author', 'me', ?,"
        "         '2026-06-15T10:00:00', '2026-06-15T10:00:00')",
        (mr_id, title, state, source_branch),
    )
    conn.commit()


class TestPhase2MergeSync:
    def _related_open(self, monkeypatch):
        """Linked ClickUp task itself is NOT done — so only MR-merge can trigger."""
        monkeypatch.setattr(clickup_client, "get_task",
                            lambda tid: {"name": "still in progress",
                                         "status": {"status": "in progress", "type": "open"}})

    def test_merged_mr_drafts_close(self, conn, monkeypatch):
        self._related_open(monkeypatch)
        _managed(conn, my_task="MYTASK", related="86abc1234")
        _seed_mr(conn, "1!42", "Fix that bug", "merged",
                 "feature/foo_clickup_86abc1234")
        assert completion.check_completions(conn) == 1
        row = conn.execute(
            "SELECT target_id, payload_json FROM pending_actions"
            " WHERE kind = 'clickup_close_task'"
        ).fetchone()
        assert row["target_id"] == "MYTASK"
        assert "merged" in row["payload_json"].lower()
        # idempotent
        assert completion.check_completions(conn) == 0

    def test_open_mr_does_not_trigger(self, conn, monkeypatch):
        self._related_open(monkeypatch)
        _managed(conn, my_task="MYTASK3", related="86abc1234")
        _seed_mr(conn, "1!43", "Still open MR", "opened",
                 "feature/foo_clickup_86abc1234")
        assert completion.check_completions(conn) == 0

    def test_merged_mr_with_different_clickup_id_does_not_trigger(self, conn, monkeypatch):
        self._related_open(monkeypatch)
        _managed(conn, my_task="MYTASK4", related="86abc1234")
        _seed_mr(conn, "1!44", "Unrelated merge", "merged",
                 "feature/foo_clickup_86xxxxxxx")
        assert completion.check_completions(conn) == 0


# ── auto-propose review tasks for assigned MRs ──────────────────────────

import json as _json


class TestMrReviewTracker:
    def _seed_reviewer_mr(self, conn, mr_id, title="some change", branch=""):
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
            " author, source_branch, updated_at, synced_at)"
            " VALUES (?, 'grp/proj', ?, 'opened', 'http://x', 'reviewer', 'morgan',"
            "         ?, '2026-06-15T10:00:00', '2026-06-15T10:00:00')",
            (mr_id, title, branch),
        )
        conn.commit()

    def test_drafts_pending_action_for_new_reviewer_mr(self, conn):
        self._seed_reviewer_mr(conn, "1!42", "Fix the thing")
        n = mr_review_tracker.propose_review_tasks(conn)
        assert n == 1
        row = conn.execute(
            "SELECT kind, target_id, payload_json FROM pending_actions"
        ).fetchone()
        assert row["kind"] == "clickup_create_task"
        assert row["target_id"] is None
        payload = _json.loads(row["payload_json"])
        assert payload["related_mr_id"] == "1!42"
        assert payload["auto_proposed"] is True
        assert payload["draft"]["labels"] == ["Review"]
        assert "Fix the thing" in payload["draft"]["name"]
        assert payload["draft"]["name"].startswith("Review - ")

    def test_skips_author_mr(self, conn):
        # author MRs aren't proposed — only reviewer
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
            " author, source_branch, updated_at, synced_at)"
            " VALUES ('1!50', 'grp/p', 't', 'opened', 'http://x', 'author', 'me',"
            "         '', '2026-06-15T10:00:00', '2026-06-15T10:00:00')"
        )
        conn.commit()
        assert mr_review_tracker.propose_review_tasks(conn) == 0

    def test_drafts_pending_action_for_engaged_mr(self, conn):
        # engaged role: user commented or approved but isn't formally a reviewer
        # (e.g. they got reassigned mid-review). Should produce a draft.
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
            " author, source_branch, updated_at, synced_at)"
            " VALUES ('1!43', 'grp/proj', 'Engaged change', 'opened', 'http://x',"
            "         'engaged', 'morgan', '', '2026-06-20T10:00:00', '2026-06-20T10:00:00')"
        )
        conn.commit()
        assert mr_review_tracker.propose_review_tasks(conn) == 1
        payload = _json.loads(conn.execute(
            "SELECT payload_json FROM pending_actions"
        ).fetchone()["payload_json"])
        assert payload["related_mr_id"] == "1!43"

    def test_skips_already_tracked_via_managed_task(self, conn):
        self._seed_reviewer_mr(conn, "1!51")
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('CT1', NULL, NULL, '1!51', 'Review', 'open', '2026-06-15')"
        )
        conn.commit()
        assert mr_review_tracker.propose_review_tasks(conn) == 0

    def test_skips_when_pending_action_already_exists(self, conn):
        """A dismiss (rejected) sticks — we don't re-propose what the user
        already saw and acted on."""
        self._seed_reviewer_mr(conn, "1!52")
        # simulate user previously rejected a Watson proposal for this MR
        conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " status, created_at) VALUES (NULL, 'clickup_create_task', NULL,"
            "  ?, 'rejected', '2026-06-15')",
            (_json.dumps({"related_mr_id": "1!52", "draft": {}}),),
        )
        conn.commit()
        assert mr_review_tracker.propose_review_tasks(conn) == 0

    def test_idempotent_across_runs(self, conn):
        self._seed_reviewer_mr(conn, "1!53")
        assert mr_review_tracker.propose_review_tasks(conn) == 1
        # second run shouldn't add another proposal for the same MR
        assert mr_review_tracker.propose_review_tasks(conn) == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM pending_actions"
        ).fetchone()["n"] == 1

    def test_branch_tag_links_to_clickup_task(self, conn):
        # cache a real clickup task that the branch references
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('86abc1234', 'related', 'in progress', 'open', '', 'http://c',"
            "         '[]', NULL, '2026-06-15')"
        )
        self._seed_reviewer_mr(conn, "1!60", branch="feat/foo_clickup_86abc1234")
        mr_review_tracker.propose_review_tasks(conn)
        payload = _json.loads(conn.execute(
            "SELECT payload_json FROM pending_actions"
        ).fetchone()["payload_json"])
        assert payload["related_task_id"] == "86abc1234"

    def test_groups_multi_repo_mrs_by_shared_branch_tag(self, conn):
        """Two reviewer MRs across different repos that share the same
        `_clickup<id>` branch tag must collapse into ONE create-task draft."""
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('86xy', 'Cross-repo refactor', 'open', 'open', '', 'http://c',"
            "         '[]', NULL, '2026-06-15')"
        )
        # MR in repo A
        self._seed_reviewer_mr(conn, "1!70", title="Refactor in cm-serving",
                               branch="feat/refactor_clickup_86xy")
        # MR in repo B — same branch tag → same requirement
        self._seed_reviewer_mr(conn, "2!80", title="Refactor in ad-renderer",
                               branch="taylor.dev/_clickup-86xy/foo")
        mr_review_tracker.propose_review_tasks(conn)

        actions = conn.execute(
            "SELECT kind, payload_json FROM pending_actions WHERE status = 'pending'"
        ).fetchall()
        # exactly one draft, not two
        assert len(actions) == 1
        assert actions[0]["kind"] == "clickup_create_task"
        payload = _json.loads(actions[0]["payload_json"])
        # the first MR (by mr_id order) is primary; the second is folded in
        assert payload["related_mr_id"] == "1!70"
        assert payload["additional_mr_ids"] == ["2!80"]
        # description picks up the second MR too
        assert "Refactor in ad-renderer" in payload["draft"]["description"]

    def test_groups_multi_repo_mrs_by_fuzzy_title(self, conn):
        """Two reviewer MRs with no branch tag but near-identical titles
        across repos should still collapse into one draft."""
        self._seed_reviewer_mr(conn, "1!71", title="Fix playback button unclickable")
        self._seed_reviewer_mr(conn, "2!81", title="Playback button unclickable fix")
        mr_review_tracker.propose_review_tasks(conn)

        actions = conn.execute(
            "SELECT kind, payload_json FROM pending_actions WHERE status = 'pending'"
        ).fetchall()
        assert len(actions) == 1
        payload = _json.loads(actions[0]["payload_json"])
        assert payload["related_mr_id"] == "1!71"
        assert "2!81" in payload["additional_mr_ids"]

    def test_links_to_existing_managed_task_via_branch_tag(self, conn):
        """If a managed task already covers a requirement (branch tag), a
        new reviewer MR for the same requirement becomes a link proposal,
        not a create proposal."""
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('86qq', 'Shared requirement', 'open', 'open', '', 'http://c',"
            "         '[]', NULL, '2026-06-15')"
        )
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('CT9', NULL, '86qq', '1!90', 'Review', 'open', '2026-06-15')"
        )
        conn.commit()
        # a NEW reviewer MR in another repo, same tag
        self._seed_reviewer_mr(conn, "2!91", title="Cross-repo piece",
                               branch="x/_clickup-86qq/y")
        mr_review_tracker.propose_review_tasks(conn)

        action = conn.execute(
            "SELECT kind, payload_json FROM pending_actions WHERE status = 'pending'"
        ).fetchone()
        assert action["kind"] == "link_mr_to_task"
        payload = _json.loads(action["payload_json"])
        assert payload["related_mr_id"] == "2!91"
        assert payload["task_name"] == "Shared requirement"

    def test_dedup_sees_additional_mr_ids(self, conn):
        """Once an MR is folded into a draft's additional_mr_ids, subsequent
        runs must NOT re-propose it as a fresh draft."""
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('86zz', 'Topic', 'open', 'open', '', 'http://c',"
            "         '[]', NULL, '2026-06-15')"
        )
        self._seed_reviewer_mr(conn, "1!95", title="Piece A",
                               branch="a/_clickup-86zz/x")
        self._seed_reviewer_mr(conn, "2!96", title="Piece B",
                               branch="b/_clickup-86zz/y")
        assert mr_review_tracker.propose_review_tasks(conn) == 2  # 1 new + 1 grouped
        # second run is a no-op
        assert mr_review_tracker.propose_review_tasks(conn) == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM pending_actions WHERE status = 'pending'"
        ).fetchone()["n"] == 1


# ── fuzzy MR↔task link proposer ─────────────────────────────────────────

def _seed_clickup_task(conn, task_id, name):
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
        " list_name, url, assignees, due_date, synced_at)"
        " VALUES (?, ?, 'open', 'open', '', '', '[]', NULL, '2026-06-15')",
        (task_id, name),
    )


def _seed_managed(conn, *, my_task, related, status="open"):
    conn.execute(
        "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
        " related_clickup_task_id, related_mr_id, additional_mr_ids, category,"
        " status, created_at) VALUES (?, NULL, ?, NULL, NULL, 'Discussion', ?,"
        "                              '2026-06-15T10:00:00')",
        (my_task, related, status),
    )


def _seed_open_mr(conn, mr_id, title, branch=""):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
        " author, source_branch, updated_at, synced_at) VALUES (?, 'grp/p', ?,"
        " 'opened', 'http://x', 'reviewer', 'someone', ?, '2026-06-15T10:00:00',"
        " '2026-06-15T10:00:00')",
        (mr_id, title, branch),
    )


class TestLinkProposer:
    def test_strong_title_overlap_proposes_link(self, conn):
        _seed_clickup_task(conn, "CT1", "[Video module] Sponsor slide rendering")
        _seed_managed(conn, my_task="MYTASK", related="CT1")
        _seed_open_mr(conn, "9!1", "[Video module] Add sponsor slide rendering for ad-companion")
        conn.commit()

        n = link_proposer.propose_mr_task_links(conn)
        assert n == 1
        row = conn.execute("SELECT kind, payload_json FROM pending_actions").fetchone()
        assert row["kind"] == "link_mr_to_task"
        payload = _json.loads(row["payload_json"])
        assert payload["related_mr_id"] == "9!1"
        assert payload["match_score"] >= 70

    def test_low_overlap_does_not_propose(self, conn):
        _seed_clickup_task(conn, "CT2", "[Video module] Sponsor slide rendering")
        _seed_managed(conn, my_task="MYTASK2", related="CT2")
        _seed_open_mr(conn, "9!2", "Update README typos")
        conn.commit()
        assert link_proposer.propose_mr_task_links(conn) == 0

    def test_skips_when_branch_tag_already_links(self, conn):
        # branch encodes the same ClickUp id → already linked, skip
        _seed_clickup_task(conn, "86d3bv2h7", "Skip button bug")
        _seed_managed(conn, my_task="MYTASK3", related="86d3bv2h7")
        _seed_open_mr(conn, "9!3", "Skip button hover state fix",
                      branch="feat/_clickup_86d3bv2h7")
        conn.commit()
        assert link_proposer.propose_mr_task_links(conn) == 0

    def test_skips_when_already_referenced_by_pending_action(self, conn):
        """Reviewer-tracker drafted a 'create task' for this MR earlier — link
        proposer should not duplicate the proposal."""
        _seed_clickup_task(conn, "CT4", "Some change with shared words here")
        _seed_managed(conn, my_task="MYTASK4", related="CT4")
        _seed_open_mr(conn, "9!4", "Some change with shared words here")
        conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " status, created_at) VALUES (NULL, 'clickup_create_task', NULL, ?,"
            " 'pending', '2026-06-15')",
            (_json.dumps({"related_mr_id": "9!4", "draft": {}}),),
        )
        conn.commit()
        assert link_proposer.propose_mr_task_links(conn) == 0

    def test_idempotent_across_runs(self, conn):
        _seed_clickup_task(conn, "CT5", "Video module sponsor slide rendering work")
        _seed_managed(conn, my_task="MYTASK5", related="CT5")
        _seed_open_mr(conn, "9!5", "Video module sponsor slide rendering for ads")
        conn.commit()
        assert link_proposer.propose_mr_task_links(conn) == 1
        assert link_proposer.propose_mr_task_links(conn) == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM pending_actions").fetchone()["n"] == 1

    def test_approval_appends_to_additional_mr_ids(self, conn):
        _seed_clickup_task(conn, "CT6", "Sponsor slide rendering work")
        _seed_managed(conn, my_task="MYTASK6", related="CT6")
        _seed_open_mr(conn, "9!6", "Sponsor slide rendering for ads")
        conn.commit()
        link_proposer.propose_mr_task_links(conn)
        action = conn.execute(
            "SELECT id, payload_json FROM pending_actions"
        ).fetchone()
        payload = _json.loads(action["payload_json"])
        # simulate approval — apply the side-effect
        link_proposer.apply_link_approval(conn, payload)
        conn.commit()
        mt = conn.execute(
            "SELECT additional_mr_ids FROM managed_tasks WHERE clickup_task_id = 'MYTASK6'"
        ).fetchone()
        assert _json.loads(mt["additional_mr_ids"]) == ["9!6"]


# ── post-hoc twin detector ──────────────────────────────────────────────


class TestMergeProposer:
    def _seed_twin_pair(self, conn):
        """Two open managed_tasks sharing related_clickup_task_id — the
        classic 'multi-repo MRs already approved before grouping' case."""
        _seed_clickup_task(conn, "PARENT1", "Refactor X")
        _seed_clickup_task(conn, "CT_KEEP", "Review - Refactor X (alice)")
        _seed_clickup_task(conn, "CT_DUP",  "Review - Refactor X in renderer (alice)")
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('CT_KEEP', NULL, 'PARENT1', '1!100', 'Review', 'open', '2026-06-15')"
        )
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('CT_DUP', NULL, 'PARENT1', '2!200', 'Review', 'open', '2026-06-15')"
        )
        conn.commit()

    def test_drafts_merge_for_open_twins(self, conn):
        self._seed_twin_pair(conn)
        n = merge_proposer.propose_merges(conn)
        assert n == 1
        row = conn.execute(
            "SELECT kind, payload_json FROM pending_actions"
            " WHERE kind = 'merge_managed_tasks'"
        ).fetchone()
        assert row is not None
        p = _json.loads(row["payload_json"])
        # lowest id wins survivor
        assert p["survivor_clickup_task_id"] == "CT_KEEP"
        assert p["duplicate_clickup_task_id"] == "CT_DUP"
        assert p["duplicate_mr_id"] == "2!200"
        assert p["parent_clickup_task_id"] == "PARENT1"

    def test_idempotent_across_runs(self, conn):
        self._seed_twin_pair(conn)
        assert merge_proposer.propose_merges(conn) == 1
        assert merge_proposer.propose_merges(conn) == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM pending_actions WHERE kind = 'merge_managed_tasks'"
        ).fetchone()["n"] == 1

    def test_skips_when_no_twin(self, conn):
        _seed_clickup_task(conn, "PARENT2", "Solo work")
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('CT_SOLO', NULL, 'PARENT2', '1!300', 'Review', 'open', '2026-06-15')"
        )
        conn.commit()
        assert merge_proposer.propose_merges(conn) == 0

    def test_apply_merges_locally_and_queues_close(self, conn):
        self._seed_twin_pair(conn)
        merge_proposer.propose_merges(conn)
        action = conn.execute(
            "SELECT payload_json FROM pending_actions"
            " WHERE kind = 'merge_managed_tasks'"
        ).fetchone()
        payload = _json.loads(action["payload_json"])

        result = merge_proposer.apply_merge_approval(conn, payload)
        assert result["linked_mrs"] == ["2!200"]

        # survivor now lists the duplicate's MR
        survivor = conn.execute(
            "SELECT additional_mr_ids, status FROM managed_tasks WHERE clickup_task_id = 'CT_KEEP'"
        ).fetchone()
        assert _json.loads(survivor["additional_mr_ids"]) == ["2!200"]
        assert survivor["status"] == "open"

        # duplicate is closed locally
        dup = conn.execute(
            "SELECT status FROM managed_tasks WHERE clickup_task_id = 'CT_DUP'"
        ).fetchone()
        assert dup["status"] == "closed"

        # a close-task action is queued for the duplicate ClickUp task
        close = conn.execute(
            "SELECT status, payload_json FROM pending_actions"
            " WHERE kind = 'clickup_close_task' AND target_id = 'CT_DUP'"
        ).fetchone()
        assert close is not None
        assert close["status"] == "pending"
        assert _json.loads(close["payload_json"])["duplicate_of"] == "CT_KEEP"


def _seed_group_review_action(conn):
    _seed_open_mr(conn, "1!410", "Server part")
    _seed_open_mr(conn, "2!420", "Client part")
    conn.execute(
        "UPDATE gitlab_mrs_cache SET author_username='same.author' "
        "WHERE mr_id IN ('1!410','2!420')"
    )
    payload = {
        "kind": "group_review_mrs",
        "fingerprint": "ai:1!410+2!420",
        "member_mr_ids": ["1!410", "2!420"],
        "candidate_work_item_ids": [],
        "suggested_title": "One coordinated review",
        "reasoning": "One rollout",
        "confidence": 0.84,
        "auto_proposed": True,
    }
    action_id = conn.execute(
        "INSERT INTO pending_actions "
        "(capture_id, kind, target_id, payload_json, status, created_at) "
        "VALUES (NULL, 'group_review_mrs', ?, ?, 'pending', '2026-09-08')",
        (payload["fingerprint"], json.dumps(payload)),
    ).lastrowid
    conn.commit()
    return action_id, payload["fingerprint"]


def test_approving_ambiguous_group_reconciles_locally_with_user_provenance(client, conn, monkeypatch):
    action_id, fingerprint = _seed_group_review_action(conn)
    monkeypatch.setattr(
        clickup_client, "execute_action",
        lambda *_args, **_kwargs: pytest.fail("local grouping crossed ClickUp boundary"),
    )

    response = client.post(f"/api/actions/{action_id}/approve")

    assert response.status_code == 200
    row = conn.execute(
        "SELECT provenance FROM review_groups WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    assert row["provenance"] == "user"
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id=?", (action_id,)
    ).fetchone()[0] == "executed"


def test_rejecting_ambiguous_group_records_sticky_separation(client, conn):
    action_id, fingerprint = _seed_group_review_action(conn)

    response = client.post(f"/api/actions/{action_id}/reject")

    assert response.status_code == 200
    assert review_groups.is_separated(conn, fingerprint) is True


# ── orphan detector ────────────────────────────────────────────────────


class TestOrphanDetector:
    def _seed_open_managed(self, conn, my_task="ORPHAN1"):
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES (?, NULL, NULL, NULL, 'Review', 'open', '2026-06-15')",
            (my_task,),
        )
        conn.commit()

    def test_drafts_close_for_404(self, conn, monkeypatch):
        self._seed_open_managed(conn, "GONE_TASK")
        # the cache does NOT contain GONE_TASK (orphan signal). Stub
        # get_task to raise a fake HTTPError with status 404.
        import requests

        class _Resp:
            status_code = 404

        def _fake_get_task(task_id):
            err = requests.HTTPError("404 Not Found")
            err.response = _Resp()
            raise err

        monkeypatch.setattr(orphan_detector.clickup_client, "get_task", _fake_get_task)
        n = orphan_detector.detect_orphans(conn)
        assert n == 1
        row = conn.execute(
            "SELECT kind, target_id, payload_json FROM pending_actions"
            " WHERE kind = 'close_orphan_card'"
        ).fetchone()
        assert row["target_id"] == "GONE_TASK"
        p = _json.loads(row["payload_json"])
        assert p["clickup_task_id"] == "GONE_TASK"

    def test_does_not_propose_when_task_exists(self, conn, monkeypatch):
        self._seed_open_managed(conn, "STILL_HERE")
        # the cache lacks it, but live GET returns a real task (not 404)
        monkeypatch.setattr(
            orphan_detector.clickup_client, "get_task",
            lambda task_id: {"id": task_id, "name": "still here"},
        )
        assert orphan_detector.detect_orphans(conn) == 0

    def test_skips_when_in_cache(self, conn, monkeypatch):
        self._seed_open_managed(conn, "CACHED1")
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('CACHED1', 'in cache', 'open', 'open', '', '', '[]', NULL, '2026-06-15')"
        )
        conn.commit()

        # if this is called, the test would be buggy — cache hit must short-circuit
        def _boom(task_id):
            raise AssertionError("get_task should not be called when task is in cache")
        monkeypatch.setattr(orphan_detector.clickup_client, "get_task", _boom)
        assert orphan_detector.detect_orphans(conn) == 0

    def test_idempotent(self, conn, monkeypatch):
        self._seed_open_managed(conn, "GONE2")
        import requests

        class _Resp:
            status_code = 404

        def _fake(t):
            err = requests.HTTPError("404")
            err.response = _Resp()
            raise err
        monkeypatch.setattr(orphan_detector.clickup_client, "get_task", _fake)

        assert orphan_detector.detect_orphans(conn) == 1
        assert orphan_detector.detect_orphans(conn) == 0  # already proposed → skip

    def test_apply_closes_locally(self, conn):
        self._seed_open_managed(conn, "GONE3")
        mt_id = conn.execute("SELECT id FROM managed_tasks WHERE clickup_task_id = 'GONE3'"
                             ).fetchone()["id"]
        orphan_detector.apply_orphan_close(conn, {
            "managed_task_id": mt_id, "clickup_task_id": "GONE3",
        })
        assert conn.execute(
            "SELECT status FROM managed_tasks WHERE id = ?", (mt_id,)
        ).fetchone()["status"] == "closed"
