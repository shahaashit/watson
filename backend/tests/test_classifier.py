import json
from datetime import datetime

from app.services import classifier

# 2026-06-10 is a Wednesday.
TODAY = datetime(2026, 6, 10, 9, 0, 0)


class TestResolveDueAt:
    def test_iso_passthrough(self):
        assert classifier.resolve_due_at("2026-07-01T15:00:00", TODAY) == "2026-07-01T15:00:00"

    def test_bare_weekday_is_upcoming(self):
        assert classifier.resolve_due_at("Friday", TODAY) == "2026-06-12T10:00:00"

    def test_bare_weekday_today_means_next_week(self):
        assert classifier.resolve_due_at("wednesday", TODAY) == "2026-06-17T10:00:00"

    def test_next_weekday_is_next_calendar_week(self):
        assert classifier.resolve_due_at("next tuesday", TODAY) == "2026-06-16T10:00:00"

    def test_tomorrow(self):
        assert classifier.resolve_due_at("tomorrow", TODAY) == "2026-06-11T10:00:00"

    def test_eod(self):
        assert classifier.resolve_due_at("EOD", TODAY) == "2026-06-10T18:00:00"

    def test_weekday_eod(self):
        assert classifier.resolve_due_at("friday EOD", TODAY) == "2026-06-12T18:00:00"

    def test_next_week(self):
        assert classifier.resolve_due_at("next week", TODAY) == "2026-06-15T10:00:00"

    def test_in_n_days(self):
        assert classifier.resolve_due_at("in 3 days", TODAY) == "2026-06-13T10:00:00"

    def test_unresolvable_returns_none(self):
        assert classifier.resolve_due_at("whenever morgan feels like it", TODAY) is None
        assert classifier.resolve_due_at("", TODAY) is None
        assert classifier.resolve_due_at(None, TODAY) is None


class TestExtractJson:
    def test_plain_json(self):
        assert classifier.extract_json('{"entries": []}') == {"entries": []}

    def test_fenced_json(self):
        text = '```json\n{"entries": [], "reminders": []}\n```'
        assert classifier.extract_json(text) == {"entries": [], "reminders": []}

    def test_json_with_prose_around_it(self):
        text = 'Here is the classification:\n{"entries": []}\nHope that helps!'
        assert classifier.extract_json(text) == {"entries": []}

    def test_no_json_raises(self):
        import pytest
        with pytest.raises(ValueError):
            classifier.extract_json("I could not classify this capture.")


CANNED = {
    "entries": [
        {
            "type": "discussion",
            "title": "Discussed iframe z-index fix with Morgan",
            "body": "He will send a patch.",
            "people": ["Morgan"],
            "tags": ["video"],
        }
    ],
    "reminders": [{"text": "Morgan to send z-index patch", "due_at": "2026-06-19T10:00:00"}],
    "clickup_actions": [
        {
            "kind": "clickup_comment",
            "task_match_query": "iframe z-index",
            "suggested_task_id": None,
            "draft": "Discussed with Morgan; patch expected Friday.",
        }
    ],
}


def _insert_capture(conn, text="had a chat with morgan"):
    cur = conn.execute(
        "INSERT INTO captures (raw_text, created_at) VALUES (?, '2026-06-10T09:00:00')",
        (text,),
    )
    conn.commit()
    return cur.lastrowid


class TestClassifyCapture:
    def test_happy_path_fans_out(self, conn, monkeypatch):
        from tests.conftest import seed_task
        seed_task(conn)
        monkeypatch.setattr(classifier, "call_llm", lambda prompt: json.dumps(CANNED))

        cid = _insert_capture(conn)
        result = classifier.classify_capture(conn, cid, "had a chat with morgan")

        assert result["status"] == "classified"
        assert len(result["entries"]) == 1
        assert len(result["reminders"]) == 1
        # matcher should have resolved the comment to the seeded task
        assert result["actions"][0]["target_id"] == "abc123"
        assert result["actions"][0]["match_confidence"] >= 0.6

        row = conn.execute("SELECT * FROM captures WHERE id = ?", (cid,)).fetchone()
        assert row["classified_at"] is not None
        assert row["classification_json"] is not None

    def test_malformed_response_marks_needs_review(self, conn, monkeypatch):
        monkeypatch.setattr(classifier, "call_llm", lambda prompt: "totally not json, sorry")

        cid = _insert_capture(conn, "something")
        result = classifier.classify_capture(conn, cid, "something")

        assert result["status"] == "needs_review"
        row = conn.execute("SELECT * FROM captures WHERE id = ?", (cid,)).fetchone()
        assert row["raw_text"] == "something"  # never lose input
        assert row["classification_json"] is None
        assert row["classified_at"] is not None

    def test_llm_exception_marks_needs_review(self, conn, monkeypatch):
        def boom(prompt):
            raise RuntimeError("api key expired")
        monkeypatch.setattr(classifier, "call_llm", boom)

        cid = _insert_capture(conn)
        result = classifier.classify_capture(conn, cid, "something")
        assert result["status"] == "needs_review"
        assert result["error"] == {
            "source": "anthropic",
            "error_type": "RuntimeError",
            "error": "Classification failed. Capture saved for manual review.",
        }

    def test_unknown_suggested_task_id_falls_back_to_matcher(self, conn, monkeypatch):
        from tests.conftest import seed_task
        seed_task(conn)
        canned = json.loads(json.dumps(CANNED))
        canned["clickup_actions"][0]["suggested_task_id"] = "hallucinated999"
        monkeypatch.setattr(classifier, "call_llm", lambda prompt: json.dumps(canned))

        cid = _insert_capture(conn)
        result = classifier.classify_capture(conn, cid, "iframe stuff")
        assert result["actions"][0]["target_id"] == "abc123"

    def test_prompt_contains_context(self, conn):
        from tests.conftest import seed_task
        seed_task(conn)
        prompt = classifier.build_prompt(conn, "my capture text", today=TODAY)
        assert "my capture text" in prompt
        assert "2026-06-10" in prompt
        assert "Wednesday" in prompt
        assert "Fix iframe z-index on Playback player" in prompt
        assert "{capture_text}" not in prompt
        # the new managed_tasks block must be substituted, not left as the placeholder
        assert "{managed_tasks}" not in prompt
        assert "Open Watson cards" in prompt

    def test_prompt_lists_open_managed_tasks(self, conn):
        # seed a related ClickUp task + an open managed_task pointing at it
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('PARENT_X', 'Cross-repo retry config', 'open', 'open', '', '',"
            "         '[]', NULL, '2026-06-15')"
        )
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, additional_mr_ids,"
            " category, status, created_at)"
            " VALUES ('OWN1', NULL, 'PARENT_X', '1!100', '[\"2!200\"]',"
            "         'Review', 'open', '2026-06-15')"
        )
        conn.commit()
        prompt = classifier.build_prompt(conn, "capture", today=TODAY)
        # the LLM must see the parent id (the match signal it'll quote back)
        # and at least one of the linked MRs
        assert "PARENT_X" in prompt
        assert "Cross-repo retry config" in prompt
        assert "1!100" in prompt

    def test_existing_work_match_redirects_to_link_mr_to_task(self, conn, monkeypatch):
        # seed: a managed_task for parent CP1 with one MR already linked, AND a
        # second MR cached that the new capture mentions
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('CP1', 'Refactor X', 'open', 'open', '', '', '[]', NULL, '2026-06-15')"
        )
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('MYTASK1', NULL, 'CP1', '1!500', 'Review', 'open', '2026-06-15')"
        )
        conn.execute(
            "INSERT INTO gitlab_mrs_cache (mr_id, project, title, state, url, role,"
            " author, source_branch, updated_at, synced_at)"
            " VALUES ('2!600', 'grp/p', 'Other repo piece', 'opened', 'http://x', 'reviewer',"
            "         'morgan', '', '2026-06-15T10:00:00', '2026-06-15T10:00:00')"
        )
        conn.commit()
        canned = {
            "entries": [{"type": "note", "title": "Cross-repo piece", "body": "",
                         "people": [], "tags": ["review"]}],
            "reminders": [],
            "clickup_actions": [{
                "kind": "clickup_create_task",
                "task_match_query": "refactor",
                "suggested_task_id": None,
                "suggested_mr_id": "2!600",
                "existing_work_clickup_id": "CP1",  # LLM matched to existing card
                "draft": {"name": "Review - X", "description": "", "labels": ["Review"]},
            }],
        }
        monkeypatch.setattr(classifier, "call_llm", lambda p: json.dumps(canned))

        cid = _insert_capture(conn, "found another repo piece")
        result = classifier.classify_capture(conn, cid, "found another repo piece")

        assert result["status"] == "classified"
        # the create_task was REPLACED by a link_mr_to_task — no duplicate task
        assert len(result["actions"]) == 1
        assert result["actions"][0]["kind"] == "link_mr_to_task"
        payload = json.loads(conn.execute(
            "SELECT payload_json FROM pending_actions WHERE id = ?",
            (result["actions"][0]["id"],),
        ).fetchone()["payload_json"])
        assert payload["related_mr_id"] == "2!600"
        assert payload["task_name"] == "Refactor X"
        assert payload["via_capture"] is True

    def test_existing_work_match_without_mr_suppresses_create(self, conn, monkeypatch):
        # If the LLM matches an existing card but the capture has no MR to link,
        # we drop the action entirely — entries still get logged.
        conn.execute(
            "INSERT INTO clickup_tasks_cache (task_id, name, status, status_type,"
            " list_name, url, assignees, due_date, synced_at)"
            " VALUES ('CP2', 'Topic', 'open', 'open', '', '', '[]', NULL, '2026-06-15')"
        )
        conn.execute(
            "INSERT INTO managed_tasks (clickup_task_id, capture_id,"
            " related_clickup_task_id, related_mr_id, category, status, created_at)"
            " VALUES ('MYTASK2', NULL, 'CP2', NULL, 'Discussion', 'open', '2026-06-15')"
        )
        conn.commit()
        canned = {
            "entries": [{"type": "note", "title": "Status note", "body": "",
                         "people": [], "tags": []}],
            "reminders": [],
            "clickup_actions": [{
                "kind": "clickup_create_task",
                "suggested_task_id": None,
                "suggested_mr_id": None,
                "existing_work_clickup_id": "CP2",
                "draft": {"name": "Discussion - X", "description": "", "labels": ["Discussion"]},
            }],
        }
        monkeypatch.setattr(classifier, "call_llm", lambda p: json.dumps(canned))

        cid = _insert_capture(conn, "update on the topic")
        result = classifier.classify_capture(conn, cid, "update on the topic")

        # entry logged, but NO clickup action drafted (no duplicate task)
        assert result["status"] == "classified"
        assert len(result["entries"]) == 1
        assert result["actions"] == []

    def test_invalid_existing_work_id_falls_back_to_create(self, conn, monkeypatch):
        # If the LLM hallucinates a managed_task match, the capture should
        # still go through the normal create_task path — not get dropped.
        canned = json.loads(json.dumps(CANNED))
        canned["clickup_actions"] = [{
            "kind": "clickup_create_task",
            "task_match_query": "iframe",
            "suggested_task_id": None,
            "suggested_mr_id": None,
            "existing_work_clickup_id": "DOES_NOT_EXIST",
            "draft": {"name": "Review - X", "description": "", "labels": ["Review"]},
        }]
        monkeypatch.setattr(classifier, "call_llm", lambda p: json.dumps(canned))

        cid = _insert_capture(conn)
        result = classifier.classify_capture(conn, cid, "x")
        assert len(result["actions"]) == 1
        assert result["actions"][0]["kind"] == "clickup_create_task"
