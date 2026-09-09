import json
from dataclasses import FrozenInstanceError

import pytest
import requests

from app.services import (
    app_settings,
    review_automation,
    review_groups,
    review_semantic_grouper,
    work_items,
)


def seed_clickup_task(conn, task_id="86abc", name="Shared requirement"):
    conn.execute(
        "INSERT INTO clickup_tasks_cache "
        "(task_id, name, status, status_type, list_name, url, assignees, due_date, synced_at) "
        "VALUES (?, ?, 'open', 'open', '', '', '[]', NULL, '2026-09-08')",
        (task_id, name),
    )
    conn.commit()


def seed_mr(
    conn,
    mr_id,
    *,
    role="reviewer",
    state="opened",
    author="Author Name",
    author_username=None,
    title="Review change",
    description="Review description",
    branch="",
    project="group/project",
):
    conn.execute(
        "INSERT INTO gitlab_mrs_cache "
        "(mr_id, project, title, state, url, role, author, author_username, "
        "source_branch, description, updated_at, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-09-08', '2026-09-08')",
        (
            mr_id,
            project,
            title,
            state,
            f"https://gitlab.example/{project}/-/merge_requests/{mr_id.split('!')[1]}",
            role,
            author,
            author_username,
            branch,
            description,
        ),
    )
    conn.commit()


def test_candidates_are_immutable_and_normalize_author_identity(conn):
    seed_mr(conn, "1!10", author="Display Name", author_username="user.name")
    seed_mr(conn, "1!11", author="Fallback Name", author_username="")

    candidates = review_automation.collect_candidates(conn)

    assert [candidate.author_username for candidate in candidates] == [
        "user.name",
        "Fallback Name",
    ]
    with pytest.raises(FrozenInstanceError):
        candidates[0].title = "Changed"


def test_exact_clickup_group_can_cross_authors(conn):
    seed_clickup_task(conn)
    seed_mr(conn, "1!10", author="one", branch="x_clickup86abc")
    seed_mr(conn, "2!20", author="two", branch="y_clickup86abc")

    exact, unresolved = review_automation.plan_exact_groups(
        conn, review_automation.collect_candidates(conn)
    )

    assert [group.mr_ids for group in exact] == [("1!10", "2!20")]
    assert exact[0].fingerprint == "exact:clickup:86abc"
    assert exact[0].title == "Shared requirement"
    assert exact[0].provenance == "exact"
    assert exact[0].confidence == 1.0
    assert exact[0].related_clickup_task_id == "86abc"
    assert unresolved == []


def test_nonexact_candidates_remain_ordered_for_same_author_ai_grouping(conn):
    seed_mr(conn, "2!21", author="one", title="Client field")
    seed_mr(conn, "1!11", author="one", title="Server field")

    exact, unresolved = review_automation.plan_exact_groups(
        conn, review_automation.collect_candidates(conn)
    )

    assert exact == []
    assert [candidate.mr_id for candidate in unresolved] == ["1!11", "2!21"]
    assert {candidate.author_username for candidate in unresolved} == {"one"}


def test_collect_candidates_preserves_reviewer_and_engaged_eligibility(conn):
    seed_mr(conn, "1!30", role="author")
    seed_mr(conn, "1!31", role="reviewer")
    seed_mr(conn, "1!32", role="engaged")
    seed_mr(conn, "1!33", role="reviewer", state="merged")

    assert [
        candidate.mr_id for candidate in review_automation.collect_candidates(conn)
    ] == ["1!31", "1!32"]


def test_collect_candidates_honors_sticky_rejected_action(conn):
    seed_mr(conn, "1!40")
    seed_mr(conn, "1!41")
    conn.execute(
        "INSERT INTO pending_actions "
        "(capture_id, kind, target_id, payload_json, status, created_at) "
        "VALUES (NULL, 'clickup_create_task', NULL, ?, 'rejected', '2026-09-08')",
        (json.dumps({"related_mr_id": "1!40", "additional_mr_ids": ["1!41"]}),),
    )
    conn.commit()

    assert review_automation.collect_candidates(conn) == []


def test_collect_candidates_excludes_existing_group_membership(conn):
    seed_mr(conn, "1!50")
    review_groups.upsert_group(
        conn,
        fingerprint="singleton:1!50",
        author_username="author",
        title="Existing review",
        description="",
        provenance="singleton",
        confidence=1.0,
        mr_ids=["1!50"],
    )

    assert review_automation.collect_candidates(conn) == []


def test_unknown_clickup_id_stays_unresolved(conn):
    seed_mr(conn, "1!60", branch="feature_clickup86missing")

    candidate = review_automation.collect_candidates(conn)[0]
    exact, unresolved = review_automation.plan_exact_groups(conn, [candidate])

    assert candidate.related_clickup_task_id is None
    assert exact == []
    assert unresolved == [candidate]


def test_exact_groups_and_members_have_stable_order(conn):
    seed_clickup_task(conn, "86bbb", "Second group")
    seed_clickup_task(conn, "86aaa", "First group")
    seed_mr(conn, "9!90", branch="x_clickup86bbb")
    seed_mr(conn, "2!20", branch="x_clickup86aaa")
    seed_mr(conn, "1!10", branch="x_clickup86aaa")

    exact, unresolved = review_automation.plan_exact_groups(
        conn, reversed(review_automation.collect_candidates(conn))
    )

    assert [group.fingerprint for group in exact] == [
        "exact:clickup:86aaa",
        "exact:clickup:86bbb",
    ]
    assert [group.mr_ids for group in exact] == [
        ("1!10", "2!20"),
        ("9!90",),
    ]
    assert unresolved == []


def test_exact_candidate_targets_existing_group_by_fingerprint(conn):
    seed_clickup_task(conn, "86existing", "Cached task title")
    existing = review_groups.upsert_group(
        conn,
        fingerprint="exact:clickup:86existing",
        author_username="original.author",
        title="Existing canonical title",
        description="Existing description",
        provenance="exact",
        confidence=1.0,
        mr_ids=["1!70"],
        related_clickup_task_id="86existing",
    )
    seed_mr(conn, "2!71", author="new.author", branch="x_clickup86existing")

    exact, unresolved = review_automation.plan_exact_groups(
        conn, review_automation.collect_candidates(conn)
    )

    assert len(exact) == 1
    assert exact[0].existing_group_id == existing["id"]
    assert exact[0].title == "Existing canonical title"
    assert exact[0].description == "Existing description"
    assert exact[0].mr_ids == ("2!71",)
    assert unresolved == []


def test_build_plan_calls_semantic_grouping_once_per_author(conn):
    seed_mr(conn, "1!80", author="One", author_username="one.user")
    seed_mr(conn, "2!81", author="One", author_username="one.user")
    seed_mr(conn, "3!82", author="Two", author_username="two.user")
    calls = []

    def semantic(_conn, candidates, existing_groups):
        calls.append(([candidate.mr_id for candidate in candidates], existing_groups))
        member_ids = tuple(candidate.mr_id for candidate in candidates)
        return review_semantic_grouper.SemanticPlan(
            groups=(review_semantic_grouper.SemanticGroup(
                member_mr_ids=member_ids,
                existing_group_id=None,
                title="One review",
                reasoning="Same rollout",
                confidence=0.9,
            ),),
            ambiguous=(),
        )

    plan = review_automation.build_plan(
        conn, ai_enabled=True, ai_ready=True, semantic_fn=semantic
    )

    assert [call[0] for call in calls] == [["1!80", "2!81"], ["3!82"]]
    assert [group.mr_ids for group in plan.groups] == [
        ("1!80", "2!81"),
        ("3!82",),
    ]
    assert plan.deferred is False


def test_build_plan_attaches_semantic_candidate_to_existing_open_group(conn):
    existing = review_groups.upsert_group(
        conn,
        fingerprint="ai:1!existing",
        author_username="one.user",
        title="Existing canonical review",
        description="Existing review context",
        provenance="ai",
        confidence=0.92,
        mr_ids=["1!existing"],
    )
    seed_mr(conn, "2!new", author="One", author_username="one.user")

    def semantic(_conn, candidates, existing_groups):
        assert [candidate.mr_id for candidate in candidates] == ["2!new"]
        assert [group["id"] for group in existing_groups] == [existing["id"]]
        return review_semantic_grouper.SemanticPlan(
            groups=(review_semantic_grouper.SemanticGroup(
                member_mr_ids=("2!new",),
                existing_group_id=existing["id"],
                title="Existing canonical review",
                reasoning="Same rollout",
                confidence=0.91,
            ),),
            ambiguous=(),
        )

    plan = review_automation.build_plan(
        conn, ai_enabled=True, ai_ready=True, semantic_fn=semantic
    )

    assert plan.deferred is False
    assert len(plan.groups) == 1
    assert plan.groups[0].existing_group_id == existing["id"]
    assert plan.groups[0].fingerprint == "ai:1!existing"
    assert plan.groups[0].mr_ids == ("2!new",)


def test_build_plan_discards_exact_and_partial_ai_results_when_any_author_fails(conn):
    seed_clickup_task(conn, "86exact")
    seed_mr(conn, "1!90", author_username="exact.user", branch="x_clickup86exact")
    seed_mr(conn, "2!91", author_username="one.user")
    seed_mr(conn, "3!92", author_username="two.user")
    calls = []

    def semantic(_conn, candidates, _existing_groups):
        calls.append(candidates[0].author_username)
        if candidates[0].author_username == "two.user":
            raise review_semantic_grouper.SemanticGroupingDeferred("bad response")
        return review_semantic_grouper.SemanticPlan(
            groups=(review_semantic_grouper.SemanticGroup(
                member_mr_ids=(candidates[0].mr_id,),
                existing_group_id=None,
                title="Review one",
                reasoning="One change",
                confidence=0.9,
            ),),
            ambiguous=(),
        )

    plan = review_automation.build_plan(
        conn, ai_enabled=True, ai_ready=True, semantic_fn=semantic
    )

    assert calls == ["one.user", "two.user"]
    assert plan.groups == ()
    assert plan.ambiguous == ()
    assert plan.deferred is True
    assert conn.execute("SELECT COUNT(*) FROM review_groups").fetchone()[0] == 0


def test_build_plan_uses_singletons_without_ai(conn):
    seed_mr(conn, "1!93", author="Author", author_username="author.user")

    plan = review_automation.build_plan(conn, ai_enabled=False, ai_ready=False)

    assert plan.deferred is False
    assert plan.groups[0].fingerprint == "singleton:1!93"
    assert plan.groups[0].provenance == "singleton"
    assert plan.groups[0].mr_ids == ("1!93",)


def seed_two_exact_reviewer_mrs(conn, clickup_id="86abc"):
    seed_clickup_task(conn, clickup_id, "Keyword TTL")
    seed_mr(conn, "1!10", author="Lee", author_username="lee.dev", branch=f"x_clickup{clickup_id}")
    seed_mr(conn, "2!20", author="Lee", author_username="lee.dev", branch=f"y_clickup{clickup_id}")


def test_auto_creation_creates_one_task_for_group_across_repeated_runs(conn):
    seed_two_exact_reviewer_mrs(conn)
    calls = []
    create = lambda payload: calls.append(payload) or {"id": "CU-REVIEW-1"}
    profile = {"auto_create_review_tasks": True, "ai_group_review_mrs": False}

    first = review_automation.run(
        conn, profile=profile, clickup_ready=True, ai_ready=False, create_fn=create
    )
    second = review_automation.run(
        conn, profile=profile, clickup_ready=True, ai_ready=False, create_fn=create
    )

    assert first["created"] == 1
    assert second["created"] == 0
    assert len(calls) == 1
    assert calls[0]["additional_mr_ids"] == ["2!20"]
    assert calls[0]["draft"]["name"] == "Review - Keyword TTL (Lee)"
    group = review_groups.open_groups(conn)[0]
    assert group["creation_state"] == "created"
    assert group["clickup_task_id"] == "CU-REVIEW-1"
    managed = conn.execute(
        "SELECT related_mr_id, additional_mr_ids FROM managed_tasks "
        "WHERE clickup_task_id='CU-REVIEW-1'"
    ).fetchone()
    assert managed["related_mr_id"] == "1!10"
    assert json.loads(managed["additional_mr_ids"]) == ["2!20"]


def test_auto_creation_logs_one_sanitized_user_readable_outcome(conn):
    seed_two_exact_reviewer_mrs(conn)

    review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": False},
        clickup_ready=True,
        ai_ready=False,
        create_fn=lambda _payload: {"id": "CU-LOGGED"},
    )

    rows = conn.execute(
        "SELECT kind, subject, details_json, related_task_id FROM system_events "
        "WHERE kind='review_creation'"
    ).fetchall()
    group_id = review_groups.open_groups(conn)[0]["id"]
    assert [tuple(row) for row in rows] == [
        (
            "review_creation",
            "Created ClickUp review task: Review - Keyword TTL (Lee)",
            json.dumps({"group_id": group_id, "mr_count": 2}),
            "CU-LOGGED",
        )
    ]


def test_auto_creation_disabled_persists_group_without_calling_clickup(conn):
    seed_two_exact_reviewer_mrs(conn)
    calls = []

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": False, "ai_group_review_mrs": False},
        clickup_ready=True,
        ai_ready=False,
        create_fn=lambda payload: calls.append(payload),
    )

    assert result["created"] == 0
    assert result["disabled"] == 1
    assert calls == []
    assert review_groups.open_groups(conn)[0]["creation_state"] == "disabled"


def test_ai_batch_failure_makes_zero_calls_and_persists_no_exact_group(conn):
    seed_two_exact_reviewer_mrs(conn)
    seed_mr(conn, "3!30", author_username="other.user")
    calls = []

    def semantic(*_args):
        raise review_semantic_grouper.SemanticGroupingDeferred("invalid response")

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": True},
        clickup_ready=True,
        ai_ready=True,
        semantic_fn=semantic,
        create_fn=lambda payload: calls.append(payload),
    )

    assert result["status"] == "deferred"
    assert calls == []
    assert review_groups.open_groups(conn) == []


def test_ai_deferral_logs_only_a_safe_user_readable_reason(conn):
    seed_mr(conn, "3!30", author_username="other.user")

    def semantic(*_args):
        raise review_semantic_grouper.SemanticGroupingDeferred(
            "Authorization: Bearer secret-token raw provider body"
        )

    review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": True},
        clickup_ready=True,
        ai_ready=True,
        semantic_fn=semantic,
        create_fn=lambda _payload: pytest.fail("deferred batch must not create"),
    )

    row = conn.execute(
        "SELECT kind, subject, details_json FROM system_events "
        "WHERE kind='review_deferral'"
    ).fetchone()
    assert tuple(row) == (
        "review_deferral",
        "Review grouping deferred: AI grouping is temporarily unavailable",
        json.dumps({"stage": "grouping"}),
    )
    serialized = json.dumps(dict(row))
    assert "secret-token" not in serialized
    assert "Authorization" not in serialized


def _http_error(status_code):
    error = requests.HTTPError(f"HTTP {status_code}")
    error.response = type("Response", (), {"status_code": status_code})()
    return error


def test_definite_4xx_failure_is_terminal_until_manual_retry(conn):
    seed_two_exact_reviewer_mrs(conn)
    calls = []

    def fail(payload):
        calls.append(payload)
        raise _http_error(400)

    profile = {"auto_create_review_tasks": True, "ai_group_review_mrs": False}
    first = review_automation.run(conn, profile=profile, clickup_ready=True, ai_ready=False, create_fn=fail)
    second = review_automation.run(conn, profile=profile, clickup_ready=True, ai_ready=False, create_fn=fail)

    assert first["failed"] == 1
    assert second["failed"] == 0
    assert len(calls) == 1
    assert review_groups.open_groups(conn)[0]["creation_state"] == "failed"


def test_transport_timeout_is_uncertain_and_never_retried(conn):
    seed_two_exact_reviewer_mrs(conn)
    calls = []

    def timeout(payload):
        calls.append(payload)
        raise requests.Timeout("response lost")

    profile = {"auto_create_review_tasks": True, "ai_group_review_mrs": False}
    first = review_automation.run(conn, profile=profile, clickup_ready=True, ai_ready=False, create_fn=timeout)
    second = review_automation.run(conn, profile=profile, clickup_ready=True, ai_ready=False, create_fn=timeout)

    assert first["uncertain"] == 1
    assert second["uncertain"] == 0
    assert len(calls) == 1
    assert review_groups.open_groups(conn)[0]["creation_state"] == "uncertain"


@pytest.mark.parametrize(
    "create",
    [
        lambda _payload: {},
        lambda _payload: (_ for _ in ()).throw(_http_error(503)),
    ],
)
def test_invalid_success_or_5xx_is_terminal_uncertain(conn, create):
    seed_two_exact_reviewer_mrs(conn)

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": False},
        clickup_ready=True,
        ai_ready=False,
        create_fn=create,
    )

    assert result["uncertain"] == 1
    assert review_groups.open_groups(conn)[0]["creation_state"] == "uncertain"


def test_post_create_local_failure_rolls_back_reconciliation_and_becomes_uncertain(conn, monkeypatch):
    seed_two_exact_reviewer_mrs(conn)
    monkeypatch.setattr(
        review_groups,
        "mark_created",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("local failure")),
    )

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": False},
        clickup_ready=True,
        ai_ready=False,
        create_fn=lambda _payload: {"id": "CU-PARTIAL"},
    )

    assert result["uncertain"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM managed_tasks WHERE clickup_task_id='CU-PARTIAL'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM work_links WHERE source_type='clickup' AND external_id='CU-PARTIAL'"
    ).fetchone()[0] == 0


def test_recovery_from_existing_creating_claim_marks_uncertain_without_retry(conn):
    group = review_groups.upsert_group(
        conn, fingerprint="singleton:1!10", author_username="lee.dev",
        title="Keyword TTL", description="", provenance="singleton",
        confidence=1.0, mr_ids=["1!10"],
    )
    assert review_groups.claim_creation(conn, group["id"])
    calls = []

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": False},
        clickup_ready=True,
        ai_ready=False,
        create_fn=lambda payload: calls.append(payload),
    )

    assert result["uncertain"] == 1
    assert calls == []
    assert review_groups.open_groups(conn)[0]["creation_state"] == "uncertain"


def test_ai_deferral_still_recovers_creating_claim_for_reconciliation(
    client, conn
):
    stale = review_groups.upsert_group(
        conn,
        fingerprint="singleton:1!stale",
        author_username="stale.user",
        title="Stale review creation",
        description="",
        provenance="singleton",
        confidence=1.0,
        mr_ids=["1!stale"],
    )
    assert review_groups.claim_creation(conn, stale["id"])
    seed_mr(conn, "2!new", author_username="new.user")

    def semantic(*_args):
        raise review_semantic_grouper.SemanticGroupingDeferred("provider unavailable")

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": True},
        clickup_ready=True,
        ai_ready=True,
        semantic_fn=semantic,
        create_fn=lambda _payload: pytest.fail("AI deferral must not create"),
    )

    assert result["status"] == "deferred"
    assert result["deferred"] == 1
    assert result["uncertain"] == 1
    assert review_groups._get_group(conn, stale["id"])["creation_state"] == "uncertain"
    suggestions = client.get("/api/review-automation/suggestions").json()[
        "suggestions"
    ]
    assert [(item["group_id"], item["status"], item["retryable"]) for item in suggestions] == [
        (stale["id"], "uncertain", False)
    ]


def test_clickup_preflight_unavailable_defers_without_claiming_network(conn):
    seed_two_exact_reviewer_mrs(conn)
    calls = []

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": False},
        clickup_ready=False,
        ai_ready=False,
        create_fn=lambda payload: calls.append(payload),
    )

    assert result["deferred"] == 1
    assert calls == []
    assert review_groups.open_groups(conn)[0]["creation_state"] == "deferred"


def test_pending_legacy_review_proposal_executes_only_after_group_task_is_stored(conn):
    seed_mr(conn, "1!55", author="Lee", author_username="lee.dev")
    payload = {
        "draft": {"name": "Review - Legacy review (Lee)", "description": "Legacy", "labels": ["Review"]},
        "related_task_id": None,
        "related_mr_id": "1!55",
        "additional_mr_ids": [],
        "auto_proposed": True,
    }
    action_id = conn.execute(
        "INSERT INTO pending_actions (kind, payload_json, status, created_at) "
        "VALUES ('clickup_create_task', ?, 'pending', '2026-09-08')",
        (json.dumps(payload),),
    ).lastrowid
    conn.commit()

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": False},
        clickup_ready=True,
        ai_ready=False,
        create_fn=lambda _payload: {"id": "CU-LEGACY"},
    )

    assert result["created"] == 1
    assert conn.execute("SELECT status FROM pending_actions WHERE id=?", (action_id,)).fetchone()[0] == "executed"
    assert conn.execute("SELECT value_json FROM app_settings WHERE key='review_automation.migration_version'").fetchone()[0] == "1"


def _seed_action(conn, kind, status, payload=None, *, target_id=None):
    action_id = conn.execute(
        "INSERT INTO pending_actions "
        "(capture_id, kind, target_id, payload_json, status, created_at) "
        "VALUES (NULL, ?, ?, ?, ?, '2026-09-08')",
        (kind, target_id, json.dumps(payload or {}), status),
    ).lastrowid
    conn.commit()
    return action_id


def _seed_group(conn, creation_state, *, fingerprint=None):
    group = review_groups.upsert_group(
        conn,
        fingerprint=fingerprint or f"singleton:1!{creation_state}",
        author_username="reviewer.author",
        title="Keyword TTL",
        description="Grouped review",
        provenance="singleton",
        confidence=1.0,
        mr_ids=[f"1!{creation_state}"],
    )
    conn.execute(
        "UPDATE review_groups SET creation_state=?, last_error=? WHERE id=?",
        (creation_state, "ClickUp review task creation failed", group["id"]),
    )
    conn.commit()
    return group["id"]


def test_migration_preserves_historical_review_action_matrix_idempotently(conn):
    seed_clickup_task(conn, "86legacyexec", "Existing legacy review")
    seed_mr(conn, "1!101", author="Pending Author", author_username="pending.author")
    seed_mr(conn, "1!102", author="Rejected Author", author_username="rejected.author")
    seed_mr(conn, "1!103", author="Failed Author", author_username="failed.author")
    seed_mr(
        conn,
        "1!104",
        author="Executed Author",
        author_username="executed.author",
        branch="legacy_clickup86legacyexec",
    )
    seed_mr(
        conn,
        "1!105",
        author="Executed Author",
        author_username="executed.author",
        branch="followup_clickup86legacyexec",
    )

    def legacy_payload(mr_id, topic, *, related_task_id=None):
        return {
            "draft": {
                "name": f"Review - {topic}",
                "description": f"Historical proposal for {mr_id}",
                "labels": ["Review"],
            },
            "related_task_id": related_task_id,
            "related_mr_id": mr_id,
            "additional_mr_ids": [],
            "auto_proposed": True,
        }

    pending_id = _seed_action(
        conn, "clickup_create_task", "pending", legacy_payload("1!101", "Pending review")
    )
    _seed_action(
        conn, "clickup_create_task", "rejected", legacy_payload("1!102", "Rejected review")
    )
    failed_id = _seed_action(
        conn, "clickup_create_task", "failed", legacy_payload("1!103", "Failed review")
    )
    _seed_action(
        conn,
        "clickup_create_task",
        "executed",
        legacy_payload(
            "1!104", "Existing legacy review", related_task_id="86legacyexec"
        ),
    )
    conn.execute(
        "INSERT INTO managed_tasks "
        "(clickup_task_id, related_clickup_task_id, related_mr_id, additional_mr_ids, "
        "category, status, created_at) VALUES (?, ?, ?, '[]', 'Review', 'open', ?)",
        ("CU-EXECUTED", "86legacyexec", "1!104", "2026-09-08"),
    )
    conn.commit()

    first = review_automation._migrate_legacy_proposals(conn)
    second = review_automation._migrate_legacy_proposals(conn)

    assert first == {"pending": 1, "rejected": 1, "failed": 1, "executed": 1}
    assert second == {"pending": 0, "rejected": 0, "failed": 0, "executed": 0}
    assert conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 4
    assert app_settings.get(conn, "review_automation.migration_version") == 1
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id=?", (pending_id,)
    ).fetchone()[0] == "pending"
    assert review_groups.is_separated(conn, "singleton:1!102")
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id=?", (failed_id,)
    ).fetchone()[0] == "failed"
    failed_payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM pending_actions WHERE id=?", (failed_id,)
        ).fetchone()[0]
    )
    failed_group_id = failed_payload["review_group_id"]
    assert review_groups._get_group(conn, failed_group_id)["creation_state"] == "failed"
    pre_run_candidates = {
        candidate.mr_id for candidate in review_automation.collect_candidates(conn)
    }
    assert "1!102" not in pre_run_candidates
    assert "1!103" not in pre_run_candidates

    calls = []

    def create_pending(payload):
        calls.append(payload)
        pending_group = next(
            group
            for group in review_groups.open_groups(conn)
            if group["fingerprint"] == "singleton:1!101"
        )
        assert pending_group["creation_state"] == "creating"
        assert pending_group["clickup_task_id"] is None
        assert conn.execute(
            "SELECT status FROM pending_actions WHERE id=?", (pending_id,)
        ).fetchone()[0] == "pending"
        return {"id": "CU-PENDING"}

    result = review_automation.run(
        conn,
        profile={"auto_create_review_tasks": True, "ai_group_review_mrs": False},
        clickup_ready=True,
        ai_ready=False,
        create_fn=create_pending,
    )

    assert result["created"] == 1
    assert len(calls) == 1
    assert calls[0]["related_mr_id"] == "1!101"
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id=?", (pending_id,)
    ).fetchone()[0] == "executed"
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id=?", (failed_id,)
    ).fetchone()[0] == "failed"
    assert {
        candidate.mr_id for candidate in review_automation.collect_candidates(conn)
    } == set()
    executed_group = next(
        group
        for group in review_groups.open_groups(conn)
        if group["fingerprint"] == "exact:clickup:86legacyexec"
    )
    assert executed_group["clickup_task_id"] == "CU-EXECUTED"
    assert executed_group["mr_ids"] == ["1!104", "1!105"]
    pending_group = next(
        group
        for group in review_groups.open_groups(conn)
        if group["fingerprint"] == "singleton:1!101"
    )
    assert pending_group["clickup_task_id"] == "CU-PENDING"

    retried = review_automation.retry_group_creation(
        conn,
        failed_group_id,
        create_fn=lambda _payload: {"id": "CU-RETRIED"},
    )
    assert retried["creation_state"] == "created"
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id=?", (failed_id,)
    ).fetchone()[0] == "executed"


def test_migration_canonicalizes_duplicate_executed_review_tasks_once(conn):
    seed_clickup_task(conn, "86shared", "Shared review")
    seed_mr(conn, "1!201", branch="one_clickup86shared")
    seed_mr(conn, "2!202", branch="two_clickup86shared")
    owner = work_items.ensure_self_person(conn, "Taylor")

    for index, (mr_id, task_id) in enumerate(
        (("1!201", "CU-OLD"), ("2!202", "CU-DUP")), start=1
    ):
        item = work_items.create_work_item(
            conn,
            title=f"Review card {index}",
            owner_person_id=owner,
            origin="discovery",
        )
        work_items.add_work_link(
            conn, item["id"], source_type="gitlab_mr", external_id=mr_id
        )
        work_items.add_work_link(
            conn, item["id"], source_type="clickup", external_id=task_id
        )
        conn.execute(
            "INSERT INTO managed_tasks "
            "(clickup_task_id, related_clickup_task_id, related_mr_id, "
            "additional_mr_ids, category, status, created_at) "
            "VALUES (?, '86shared', ?, '[]', 'Review', 'open', ?)",
            (task_id, mr_id, f"2026-09-0{index}"),
        )
        _seed_action(
            conn,
            "clickup_create_task",
            "executed",
            {
                "draft": {"name": "Review - Shared review", "labels": ["Review"]},
                "related_task_id": "86shared",
                "related_mr_id": mr_id,
                "additional_mr_ids": [],
                "auto_proposed": True,
            },
        )
    conn.commit()

    first = review_automation._migrate_legacy_proposals(conn)
    second = review_automation._migrate_legacy_proposals(conn)

    assert first["executed"] == 2
    assert second["executed"] == 0
    group = review_groups.open_groups(conn)[0]
    assert group["clickup_task_id"] == "CU-OLD"
    assert group["creation_state"] == "created"
    assert conn.execute(
        "SELECT status FROM managed_tasks WHERE clickup_task_id='CU-DUP'"
    ).fetchone()[0] == "closed"
    cleanup = conn.execute(
        "SELECT target_id, status FROM pending_actions "
        "WHERE kind='clickup_close_task'"
    ).fetchall()
    assert [tuple(row) for row in cleanup] == [("CU-DUP", "pending")]


def test_suggestions_include_review_decisions_but_not_unrelated_actions(client, conn):
    ambiguous = _seed_action(
        conn,
        "group_review_mrs",
        "pending",
        {
            "fingerprint": "ai:1!10+2!20",
            "suggested_title": "Keyword TTL",
            "member_mr_ids": ["1!10", "2!20"],
            "confidence": 0.78,
        },
        target_id="ai:1!10+2!20",
    )
    failed = _seed_action(
        conn,
        "clickup_create_task",
        "failed",
        {
            "auto_proposed": True,
            "review_group_id": 7,
            "draft": {"name": "Review - Keyword TTL"},
        },
    )
    duplicate = _seed_action(
        conn,
        "clickup_close_task",
        "pending",
        {"reason": "Duplicate after merge approval"},
        target_id="CU-DUPLICATE",
    )
    _seed_action(conn, "clickup_comment", "pending")
    _seed_action(
        conn,
        "clickup_create_task",
        "failed",
        {"auto_proposed": False, "draft": {"name": "Discussion - Other"}},
    )

    response = client.get("/api/review-automation/suggestions")

    assert response.status_code == 200
    items = response.json()["suggestions"]
    assert [(item["id"], item["kind"], item["status"]) for item in items] == [
        (duplicate, "clickup_close_task", "pending"),
        (failed, "clickup_create_task", "failed"),
        (ambiguous, "group_review_mrs", "pending"),
    ]
    assert all("technical" not in item and "technical_error" not in item for item in items)


def test_ambiguous_grouping_approval_and_rejection_log_user_resolutions(client, conn):
    for mr_id in ("1!10", "2!20", "1!11", "2!21"):
        seed_mr(conn, mr_id, author_username="same.author")
    merge_id = _seed_action(
        conn,
        "group_review_mrs",
        "pending",
        {
            "fingerprint": "ai:1!10+2!20",
            "suggested_title": "Keyword TTL",
            "member_mr_ids": ["1!10", "2!20"],
            "confidence": 0.8,
            "reasoning": "Authorization: Bearer secret-token",
        },
        target_id="ai:1!10+2!20",
    )
    separate_id = _seed_action(
        conn,
        "group_review_mrs",
        "pending",
        {
            "fingerprint": "ai:1!11+2!21",
            "suggested_title": "Separate rollout",
            "member_mr_ids": ["1!11", "2!21"],
            "confidence": 0.7,
            "reasoning": "raw provider body secret-token",
        },
        target_id="ai:1!11+2!21",
    )

    assert client.post(f"/api/actions/{merge_id}/approve").status_code == 200
    assert client.post(f"/api/actions/{separate_id}/reject").status_code == 200

    rows = conn.execute(
        "SELECT kind, subject, details_json, related_action_id FROM system_events "
        "WHERE kind='review_resolution' ORDER BY id"
    ).fetchall()
    assert [(row["subject"], row["related_action_id"]) for row in rows] == [
        ("Merged review grouping: Keyword TTL", merge_id),
        ("Kept review MRs separate: Separate rollout", separate_id),
    ]
    serialized = json.dumps([dict(row) for row in rows])
    assert "secret-token" not in serialized
    assert "Authorization" not in serialized


def test_suggestions_include_uncertain_group_as_reconciliation_only(client, conn):
    group_id = _seed_group(conn, "uncertain")

    response = client.get("/api/review-automation/suggestions")

    assert response.status_code == 200
    assert response.json()["suggestions"] == [{
        "id": f"review-group-{group_id}",
        "group_id": group_id,
        "kind": "review_group_reconciliation",
        "status": "uncertain",
        "payload": {
            "suggested_title": "Keyword TTL",
            "member_mr_ids": ["1!uncertain"],
            "creation_state": "uncertain",
        },
        "error": "ClickUp review task creation failed",
        "retryable": False,
    }]


def test_suggestions_prefer_uncertain_group_over_historical_failed_create_action(
    client, conn
):
    group_id = _seed_group(conn, "failed")
    action_id = _seed_action(
        conn,
        "clickup_create_task",
        "failed",
        {
            "auto_proposed": True,
            "review_group_id": group_id,
            "draft": {"name": "Review - Keyword TTL"},
        },
    )
    review_groups.mark_uncertain(
        conn, group_id, "ClickUp creation outcome is uncertain"
    )

    response = client.get("/api/review-automation/suggestions")

    assert response.status_code == 200
    items = response.json()["suggestions"]
    assert len(items) == 1
    assert items[0]["id"] == f"review-group-{group_id}"
    assert items[0]["status"] == "uncertain"
    assert items[0]["retryable"] is False
    assert items[0]["id"] != action_id


@pytest.mark.parametrize("creation_state", ["uncertain", "creating", "created"])
def test_suggestion_retry_rejects_nonretryable_creation_states(
    client, conn, creation_state
):
    group_id = _seed_group(conn, creation_state)

    response = client.post(f"/api/review-automation/groups/{group_id}/retry")

    assert response.status_code == 409


@pytest.mark.parametrize("creation_state", ["failed", "deferred"])
def test_suggestion_retry_reuses_durable_claim_for_definite_failures(
    client, conn, monkeypatch, creation_state
):
    group_id = _seed_group(conn, creation_state)
    calls = []
    monkeypatch.setattr(
        review_automation.clickup_client,
        "create_review_task",
        lambda payload: calls.append(payload) or {"id": f"CU-{creation_state}"},
    )
    monkeypatch.setattr(
        review_automation.clickup_client,
        "refresh_tasks",
        lambda *_args, **_kwargs: None,
    )

    response = client.post(f"/api/review-automation/groups/{group_id}/retry")

    assert response.status_code == 200
    assert response.json()["group"]["creation_state"] == "created"
    assert len(calls) == 1
