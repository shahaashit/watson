import json

import pytest

from app.services import review_automation, review_groups, review_semantic_grouper


def candidate(mr_id, author_username):
    return review_automation.ReviewCandidate(
        mr_id=mr_id,
        project=mr_id.split("!", 1)[0],
        title=f"Review {mr_id}",
        description="One coordinated rollout",
        url=f"https://gitlab.example/{mr_id}",
        author_username=author_username,
        author=author_username,
        source_branch=f"feature-{mr_id}",
        related_clickup_task_id=None,
    )


def response_for(member_ids, **overrides):
    group = {
        "member_mr_ids": member_ids,
        "existing_group_id": None,
        "title": "Client app keyword TTL review",
        "reasoning": "Server and cron changes implement one rollout",
        "confidence": 0.93,
    }
    group.update(overrides)
    return {"groups": [group]}


def test_accepts_high_confidence_same_author_cross_repo_group(conn):
    candidates = [candidate("1!10", "lee.dev"), candidate("2!20", "lee.dev")]

    plan = review_semantic_grouper.group_by_context(
        conn,
        candidates,
        [],
        completion_fn=lambda _prompt: response_for(["1!10", "2!20"]),
    )

    assert plan.groups[0].member_mr_ids == ("1!10", "2!20")
    assert plan.groups[0].title == "Client app keyword TTL review"
    assert plan.ambiguous == ()


@pytest.mark.parametrize(
    "response",
    [
        response_for(["unknown"]),
        response_for(["1!10", "1!10"]),
        {"groups": []},
        response_for(["1!10"], unexpected=True),
        response_for(["1!10"], confidence=1.01),
    ],
)
def test_rejects_untrusted_or_incomplete_membership(conn, response):
    with pytest.raises(review_semantic_grouper.SemanticGroupingDeferred):
        review_semantic_grouper.group_by_context(
            conn,
            [candidate("1!10", "lee.dev")],
            [],
            completion_fn=lambda _prompt: response,
        )


def test_rejects_membership_repeated_across_groups(conn):
    response = {
        "groups": [
            response_for(["1!10"])["groups"][0],
            response_for(["1!10"])["groups"][0],
        ]
    }
    with pytest.raises(review_semantic_grouper.SemanticGroupingDeferred):
        review_semantic_grouper.group_by_context(
            conn,
            [candidate("1!10", "lee.dev")],
            [],
            completion_fn=lambda _prompt: response,
        )


def test_rejects_cross_author_ai_grouping(conn):
    candidates = [candidate("1!10", "lee.dev"), candidate("2!20", "other.user")]
    with pytest.raises(review_semantic_grouper.SemanticGroupingDeferred):
        review_semantic_grouper.group_by_context(
            conn,
            candidates,
            [],
            completion_fn=lambda _prompt: response_for(["1!10", "2!20"]),
        )


@pytest.mark.parametrize("existing_group_id", [99999, 1])
def test_rejects_nonexistent_or_closed_existing_group(conn, existing_group_id):
    existing_groups = []
    if existing_group_id == 1:
        item = conn.execute(
            "INSERT INTO work_items (title, state, origin, created_at, updated_at) "
            "VALUES ('Old review', 'done', 'discovery', '2026-09-08', '2026-09-08')"
        ).lastrowid
        group = review_groups.upsert_group(
            conn,
            fingerprint="ai:old",
            author_username="lee.dev",
            title="Old review",
            description="",
            provenance="ai",
            confidence=0.9,
            mr_ids=["9!90"],
        )
        conn.execute(
            "UPDATE review_groups SET work_item_id=? WHERE id=?", (item, group["id"])
        )
        conn.commit()
        existing_group_id = group["id"]
        existing_groups = review_groups.open_groups(conn)

    with pytest.raises(review_semantic_grouper.SemanticGroupingDeferred):
        review_semantic_grouper.group_by_context(
            conn,
            [candidate("1!10", "lee.dev")],
            existing_groups,
            completion_fn=lambda _prompt: response_for(
                ["1!10"], existing_group_id=existing_group_id
            ),
        )


@pytest.mark.parametrize(
    "completion",
    [
        lambda _prompt: "not json",
        lambda _prompt: (_ for _ in ()).throw(TimeoutError("timed out")),
    ],
)
def test_parse_and_transport_failures_defer_the_batch(conn, completion):
    with pytest.raises(review_semantic_grouper.SemanticGroupingDeferred):
        review_semantic_grouper.group_by_context(
            conn,
            [candidate("1!10", "lee.dev")],
            [],
            completion_fn=completion,
        )


def test_confidence_below_threshold_becomes_ambiguous(conn):
    plan = review_semantic_grouper.group_by_context(
        conn,
        [candidate("1!10", "lee.dev")],
        [],
        completion_fn=lambda _prompt: response_for(["1!10"], confidence=0.84),
    )

    assert plan.groups == ()
    assert plan.ambiguous[0].confidence == 0.84


def test_prompt_bounds_untrusted_candidate_text(conn):
    long = "x" * 20_000
    oversized = candidate("1!10", "lee.dev")
    oversized = review_automation.ReviewCandidate(
        **{**oversized.__dict__, "title": long, "description": long}
    )
    prompts = []

    review_semantic_grouper.group_by_context(
        conn,
        [oversized],
        [],
        completion_fn=lambda prompt: prompts.append(prompt)
        or json.dumps(response_for(["1!10"])),
    )

    assert len(prompts[0]) < 10_000
    assert long not in prompts[0]
