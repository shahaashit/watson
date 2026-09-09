"""Fuzzy-match capture text against the local ClickUp task cache."""
from rapidfuzz import fuzz

# Below this score a match is considered low-confidence and the draft is
# surfaced without a target task, letting the user pick manually.
LOW_CONFIDENCE = 0.6


def best_match(query: str, tasks: list) -> tuple:
    """Return (task_dict_or_None, confidence 0..1) for the best fuzzy match.

    tasks: iterable of dicts with at least "task_id" and "name".
    """
    if not query or not tasks:
        return None, 0.0
    best, best_score = None, 0.0
    q = query.lower()
    for task in tasks:
        name = (task.get("name") or "").lower()
        if not name:
            continue
        score = fuzz.token_set_ratio(q, name) / 100.0
        if score > best_score:
            best, best_score = task, score
    return best, best_score


def match_tasks_from_cache(conn, query: str) -> tuple:
    rows = conn.execute(
        "SELECT task_id, name FROM clickup_tasks_cache"
    ).fetchall()
    tasks = [{"task_id": r["task_id"], "name": r["name"]} for r in rows]
    return best_match(query, tasks)
