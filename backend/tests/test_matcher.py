from app.services import matcher

TASKS = [
    {"task_id": "t1", "name": "Fix iframe z-index on Playback player"},
    {"task_id": "t2", "name": "Partner integration: platform module rollout"},
    {"task_id": "t3", "name": "Upgrade cache serving to Go 1.22"},
]


def test_exact_phrase_matches_strongly():
    task, score = matcher.best_match("iframe z-index", TASKS)
    assert task["task_id"] == "t1"
    assert score >= matcher.LOW_CONFIDENCE


def test_word_order_and_partials():
    task, score = matcher.best_match("rollout of the partner platform module", TASKS)
    assert task["task_id"] == "t2"
    assert score >= matcher.LOW_CONFIDENCE


def test_gibberish_is_low_confidence():
    _, score = matcher.best_match("quarterly travel reimbursement form", TASKS)
    assert score < matcher.LOW_CONFIDENCE


def test_empty_inputs():
    assert matcher.best_match("", TASKS) == (None, 0.0)
    assert matcher.best_match("anything", []) == (None, 0.0)
