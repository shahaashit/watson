"""Plan, reconcile, and safely create canonical review tasks."""

import json
import re
from dataclasses import dataclass

import requests

from ..config import settings
from ..models import now_iso
from . import (
    app_settings,
    clickup_client,
    completion,
    events,
    gitlab_client,
    mr_review_tracker,
    review_groups,
    review_semantic_grouper,
    work_items,
)


MIGRATION_KEY = "review_automation.migration_version"
MIGRATION_VERSION = 1


@dataclass(frozen=True)
class ReviewCandidate:
    mr_id: str
    project: str
    title: str
    description: str
    url: str
    author_username: str
    author: str
    source_branch: str
    related_clickup_task_id: str | None


@dataclass(frozen=True)
class PlannedReviewGroup:
    fingerprint: str
    author_username: str
    title: str
    description: str
    provenance: str
    confidence: float
    mr_ids: tuple[str, ...]
    related_clickup_task_id: str | None = None
    existing_group_id: int | None = None
    reasoning: str = ""


@dataclass(frozen=True)
class ReviewBatchPlan:
    groups: tuple[PlannedReviewGroup, ...]
    ambiguous: tuple[PlannedReviewGroup, ...]
    deferred: bool = False
    error: str | None = None


def _text(row, key: str) -> str:
    return str(row[key] or "").strip()


def _cached_clickup_id(conn, mr) -> str | None:
    related_id = gitlab_client.clickup_id_from_mr(mr)
    if not related_id:
        return None
    row = conn.execute(
        "SELECT task_id FROM clickup_tasks_cache WHERE task_id = ? COLLATE NOCASE",
        (related_id,),
    ).fetchone()
    return row["task_id"] if row else None


def collect_candidates(conn) -> list[ReviewCandidate]:
    """Collect eligible, untracked review MRs from Watson's local caches."""
    rows = conn.execute(
        "SELECT mr_id, project, title, description, url, author_username, author, "
        "source_branch FROM gitlab_mrs_cache "
        "WHERE state='opened' AND role IN ('reviewer','engaged') ORDER BY mr_id"
    ).fetchall()
    candidates = []
    for row in rows:
        if mr_review_tracker._already_tracked(conn, row["mr_id"]):
            continue
        author = _text(row, "author")
        candidates.append(
            ReviewCandidate(
                mr_id=_text(row, "mr_id"),
                project=_text(row, "project"),
                title=_text(row, "title"),
                description=_text(row, "description"),
                url=_text(row, "url"),
                author_username=_text(row, "author_username") or author,
                author=author,
                source_branch=_text(row, "source_branch"),
                related_clickup_task_id=_cached_clickup_id(conn, row),
            )
        )
    return candidates


def _existing_exact_group(conn, fingerprint: str):
    from . import work_suppression
    row = conn.execute(
        "SELECT id, author_username, title, description FROM review_groups "
        "WHERE fingerprint=?",
        (fingerprint,),
    ).fetchone()
    if row and work_suppression.group_removed(conn, review_groups._get_group(conn, row['id'])):
        return None
    return row


def _clickup_title(conn, task_id: str) -> str:
    row = conn.execute(
        "SELECT name FROM clickup_tasks_cache WHERE task_id=?", (task_id,)
    ).fetchone()
    return _text(row, "name") if row else ""


def plan_exact_groups(
    conn, candidates
) -> tuple[list[PlannedReviewGroup], list[ReviewCandidate]]:
    """Partition exact ClickUp matches before same-author semantic grouping."""
    exact_members: dict[str, list[ReviewCandidate]] = {}
    unresolved = []
    for candidate in candidates:
        if candidate.related_clickup_task_id:
            exact_members.setdefault(candidate.related_clickup_task_id, []).append(candidate)
        else:
            unresolved.append(candidate)

    exact = []
    for task_id in sorted(exact_members, key=lambda value: (value.casefold(), value)):
        members = sorted(exact_members[task_id], key=lambda candidate: candidate.mr_id)
        fingerprint = f"exact:clickup:{task_id}"
        existing = _existing_exact_group(conn, fingerprint)
        author_usernames = {member.author_username for member in members}
        if existing:
            author_username = existing["author_username"]
            title = existing["title"]
            description = existing["description"]
        else:
            author_username = (
                next(iter(author_usernames)) if len(author_usernames) == 1 else ""
            )
            title = (
                _clickup_title(conn, task_id) or members[0].title or members[0].mr_id
            )
            description = members[0].description
        exact.append(
            PlannedReviewGroup(
                fingerprint=fingerprint,
                author_username=author_username,
                title=title,
                description=description,
                provenance="exact",
                confidence=1.0,
                mr_ids=tuple(member.mr_id for member in members),
                related_clickup_task_id=task_id,
                existing_group_id=existing["id"] if existing else None,
            )
        )

    unresolved.sort(
        key=lambda candidate: (
            candidate.author_username.casefold(),
            candidate.author_username,
            candidate.mr_id,
        )
    )
    return exact, unresolved


def _semantic_planned_group(conn, candidates_by_id, semantic_group) -> PlannedReviewGroup:
    members = tuple(semantic_group.member_mr_ids)
    first = candidates_by_id[members[0]]
    fingerprint = "ai:" + "+".join(sorted(members))
    if semantic_group.existing_group_id is not None:
        existing = next(
            group
            for group in review_groups.open_groups(
                conn, candidates_by_id[members[0]].author_username
            )
            if group["id"] == semantic_group.existing_group_id
        )
        fingerprint = existing["fingerprint"]
    return PlannedReviewGroup(
        fingerprint=fingerprint,
        author_username=first.author_username,
        title=semantic_group.title,
        description=first.description,
        provenance="ai",
        confidence=semantic_group.confidence,
        mr_ids=members,
        existing_group_id=semantic_group.existing_group_id,
        reasoning=semantic_group.reasoning,
    )


def _singleton(candidate: ReviewCandidate) -> PlannedReviewGroup:
    return PlannedReviewGroup(
        fingerprint=f"singleton:{candidate.mr_id}",
        author_username=candidate.author_username,
        title=candidate.title or candidate.mr_id,
        description=candidate.description,
        provenance="singleton",
        confidence=1.0,
        mr_ids=(candidate.mr_id,),
    )


def build_plan(
    conn,
    *,
    ai_enabled: bool,
    ai_ready: bool,
    semantic_fn=None,
) -> ReviewBatchPlan:
    """Build one all-or-nothing review grouping plan without persisting it."""
    candidates = collect_candidates(conn)
    exact, unresolved = plan_exact_groups(conn, candidates)
    if not unresolved:
        return ReviewBatchPlan(groups=tuple(exact), ambiguous=())
    if not ai_enabled:
        return ReviewBatchPlan(
            groups=tuple(exact + [_singleton(candidate) for candidate in unresolved]),
            ambiguous=(),
        )
    if not ai_ready:
        return ReviewBatchPlan(
            groups=(),
            ambiguous=(),
            deferred=True,
            error="semantic grouping unavailable",
        )

    semantic = semantic_fn or review_semantic_grouper.group_by_context
    by_author: dict[str, list[ReviewCandidate]] = {}
    for candidate in unresolved:
        by_author.setdefault(candidate.author_username, []).append(candidate)
    planned = list(exact)
    ambiguous = []
    candidates_by_id = {candidate.mr_id: candidate for candidate in unresolved}
    try:
        for author_username in sorted(by_author, key=lambda value: (value.casefold(), value)):
            author_candidates = by_author[author_username]
            existing = review_groups.open_groups(conn, author_username)
            semantic_plan = semantic(conn, author_candidates, existing)
            planned.extend(
                _semantic_planned_group(conn, candidates_by_id, group)
                for group in semantic_plan.groups
            )
            ambiguous.extend(
                _semantic_planned_group(conn, candidates_by_id, group)
                for group in semantic_plan.ambiguous
            )
    except review_semantic_grouper.SemanticGroupingDeferred as exc:
        return ReviewBatchPlan(
            groups=(), ambiguous=(), deferred=True, error=str(exc)
        )
    return ReviewBatchPlan(groups=tuple(planned), ambiguous=tuple(ambiguous))


def _payload_mr_ids(payload: dict) -> tuple[str, ...]:
    values = [payload.get("related_mr_id"), *(payload.get("additional_mr_ids") or [])]
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _legacy_fingerprint(payload: dict, members: tuple[str, ...]) -> str:
    related = payload.get("related_task_id")
    if related:
        return f"exact:clickup:{related}"
    if len(members) == 1:
        return f"singleton:{members[0]}"
    return "ai:" + "+".join(sorted(members))


def _topic_from_draft(payload: dict, fallback: str) -> str:
    draft = payload.get("draft")
    name = draft.get("name", "") if isinstance(draft, dict) else ""
    topic = str(name or fallback).strip()
    if topic.startswith("Review - "):
        topic = topic[len("Review - "):]
    if topic.endswith(")") and " (" in topic:
        topic = topic.rsplit(" (", 1)[0]
    return topic or fallback


def _author_for_members(conn, members) -> str:
    if not members:
        return ""
    row = conn.execute(
        "SELECT COALESCE(NULLIF(author_username, ''), author, '') AS author_username "
        "FROM gitlab_mrs_cache WHERE mr_id=?",
        (members[0],),
    ).fetchone()
    return str(row["author_username"] or "") if row else ""


def _managed_for_members(conn, members):
    wanted = set(members)
    for row in conn.execute("SELECT * FROM managed_tasks ORDER BY id"):
        known = {row["related_mr_id"]} if row["related_mr_id"] else set()
        try:
            known.update(json.loads(row["additional_mr_ids"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass
        if wanted.intersection(known):
            return row
    return None


def _migrate_legacy_proposals(conn) -> dict[str, int]:
    migrated = {status: 0 for status in ("pending", "rejected", "failed", "executed")}
    if int(app_settings.get(conn, MIGRATION_KEY, 0) or 0) >= MIGRATION_VERSION:
        return migrated
    rows = conn.execute(
        "SELECT * FROM pending_actions WHERE kind='clickup_create_task' ORDER BY id"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if payload.get("auto_proposed") is not True:
            continue
        members = _payload_mr_ids(payload)
        from . import work_suppression
        if any(work_suppression.mr_removed(conn, mr_id) for mr_id in members):
            continue
        if not members:
            continue
        fingerprint = _legacy_fingerprint(payload, members)
        if row["status"] == "rejected":
            migrated["rejected"] += 1
            review_groups.record_separation(conn, fingerprint)
            continue
        if row["status"] == "failed":
            migrated["failed"] += 1
        elif row["status"] not in ("pending", "executed"):
            continue
        else:
            migrated[row["status"]] += 1
        existing = conn.execute(
            "SELECT id FROM review_groups WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        existing_members = review_groups._get_group(conn, existing["id"])["mr_ids"] if existing else []
        if existing and work_suppression.group_removed(conn, review_groups._get_group(conn, existing['id'])):
            continue
        group = review_groups.upsert_group(
            conn,
            fingerprint=fingerprint,
            author_username=_author_for_members(conn, members),
            title=_topic_from_draft(payload, members[0]),
            description=(payload.get("draft") or {}).get("description", "")
            if isinstance(payload.get("draft"), dict) else "",
            provenance=("exact" if payload.get("related_task_id") else "singleton" if len(members) == 1 else "ai"),
            confidence=1.0,
            mr_ids=[*existing_members, *members],
            related_clickup_task_id=payload.get("related_task_id"),
        )
        if row["status"] == "failed":
            conn.execute(
                "UPDATE review_groups SET creation_state='failed', last_error=? WHERE id=?",
                (row["error"] or "ClickUp review task creation failed", group["id"]),
            )
            payload["review_group_id"] = group["id"]
            conn.execute(
                "UPDATE pending_actions SET payload_json=? WHERE id=?",
                (json.dumps(payload), row["id"]),
            )
        if row["status"] == "executed":
            managed = _managed_for_members(conn, members)
            if managed:
                linked = conn.execute(
                    "SELECT work_item_id FROM work_links WHERE source_type='clickup' AND external_id=?",
                    (managed["clickup_task_id"],),
                ).fetchone()
                review_groups.mark_created(
                    conn,
                    group["id"],
                    managed["clickup_task_id"],
                    work_item_id=linked["work_item_id"] if linked else None,
                )
    review_groups._reconcile_all_managed_review_tasks(conn)
    app_settings.set_value(conn, MIGRATION_KEY, MIGRATION_VERSION, commit=False)
    return migrated


def _mark_legacy_executed(conn, group: dict) -> None:
    group_members = set(group["mr_ids"])
    for row in conn.execute(
        "SELECT id, payload_json FROM pending_actions "
        "WHERE kind='clickup_create_task' AND status IN ('pending','failed')"
    ).fetchall():
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        members = set(_payload_mr_ids(payload))
        if payload.get("auto_proposed") is True and members and members.issubset(group_members):
            conn.execute(
                "UPDATE pending_actions SET status='executed', resolved_at=?, error=NULL WHERE id=?",
                (now_iso(), row["id"]),
            )


def _creation_payload(conn, group: dict) -> dict:
    mr_rows = {
        row["mr_id"]: row
        for row in conn.execute(
            "SELECT mr_id, title, url, author FROM gitlab_mrs_cache WHERE mr_id IN ("
            + ",".join("?" for _ in group["mr_ids"])
            + ")",
            group["mr_ids"],
        )
    }
    members = list(group["mr_ids"])
    primary = members[0]
    author_names = sorted({
        str(mr_rows[mr_id]["author"] or "").strip()
        for mr_id in members if mr_id in mr_rows and str(mr_rows[mr_id]["author"] or "").strip()
    })
    author = author_names[0] if len(author_names) == 1 else group["author_username"] or "multiple authors"
    topic = re.sub(r"^(?:\[[^]]+\]\s*)+", "", group["title"]).strip()
    lines = []
    for mr_id in members:
        row = mr_rows.get(mr_id)
        lines.append(
            f"MR: {(row['title'] if row else mr_id) or mr_id}\nMR url: {(row['url'] if row else '') or ''}"
        )
    return {
        "draft": {
            "name": f"Review - {topic} ({author})",
            "description": "Reviewing grouped merge requests.\n\n" + "\n\n".join(lines),
            "labels": ["Review"],
        },
        "related_task_id": group["related_clickup_task_id"],
        "related_mr_id": primary,
        "additional_mr_ids": members[1:],
        "review_group_id": group["id"],
        "auto_proposed": True,
    }


def _ensure_group_work_item(conn, group: dict, task_name: str) -> int:
    if group["work_item_id"] is not None:
        return group["work_item_id"]
    owner = work_items.ensure_self_person(conn, settings.watson_user_name)
    item = work_items.create_work_item(
        conn, title=task_name, owner_person_id=owner, origin="discovery"
    )
    return item["id"]


def _record_creation(conn, group: dict, payload: dict, created: dict) -> None:
    with review_groups._atomic(conn):
        work_item_id = _ensure_group_work_item(conn, group, payload["draft"]["name"])
        completion.record_managed_task(
            conn,
            created["id"],
            None,
            payload.get("related_task_id"),
            "Review",
            payload.get("related_mr_id"),
            payload.get("additional_mr_ids"),
            commit=False,
        )
        for mr_id in group["mr_ids"]:
            work_items.add_work_link(
                conn, work_item_id, source_type="gitlab_mr", external_id=mr_id
            )
        work_items.add_work_link(
            conn, work_item_id, source_type="clickup", external_id=created["id"]
        )
        clickup_client.refresh_tasks(
            conn, [created["id"], payload.get("related_task_id")], commit=False
        )
        stored = review_groups.mark_created(
            conn, group["id"], created["id"], work_item_id=work_item_id
        )
        _mark_legacy_executed(conn, stored)
        events.record(
            conn,
            "review_creation",
            f"Created ClickUp review task: {payload['draft']['name']}",
            details={"group_id": group["id"], "mr_count": len(group["mr_ids"])},
            task_id=created["id"],
        )


def _creation_failure_state(error: Exception) -> str:
    if isinstance(error, clickup_client.CircuitOpenError):
        return "deferred"
    if isinstance(error, requests.HTTPError):
        status = getattr(getattr(error, "response", None), "status_code", None)
        return "failed" if status is not None and 400 <= status < 500 else "uncertain"
    if isinstance(error, requests.RequestException):
        return "uncertain"
    if isinstance(error, (RuntimeError, ValueError)):
        return "deferred"
    return "uncertain"


def _mark_creation_error(conn, group_id: int, state: str) -> None:
    safe_error = "ClickUp review task creation failed"
    if state == "failed":
        review_groups.mark_failed(conn, group_id, safe_error)
    elif state == "deferred":
        review_groups.mark_deferred(conn, group_id, safe_error)
        group = review_groups._get_group(conn, group_id)
        events.record(
            conn,
            "review_deferral",
            f"Review task creation deferred: {group['title']}",
            details={"stage": "creation", "group_id": group_id},
        )
        conn.commit()
    else:
        review_groups.mark_uncertain(conn, group_id, safe_error)


def run(
    conn,
    *,
    profile: dict,
    clickup_ready: bool,
    ai_ready: bool,
    semantic_fn=None,
    create_fn=None,
) -> dict[str, int | str]:
    """Run one idempotent grouping and automatic Review-task creation batch."""
    result = {
        "exact_grouped": 0,
        "ai_grouped": 0,
        "attached": 0,
        "created": 0,
        "disabled": 0,
        "deferred": 0,
        "ambiguous": 0,
        "failed": 0,
        "uncertain": 0,
    }
    with review_groups._atomic(conn):
        result["uncertain"] += review_groups.recover_creating(conn)
    conn.commit()
    plan = build_plan(
        conn,
        ai_enabled=bool(profile.get("ai_group_review_mrs")),
        ai_ready=ai_ready,
        semantic_fn=semantic_fn,
    )
    if plan.deferred:
        result["status"] = "deferred"
        result["deferred"] = 1
        events.record(
            conn,
            "review_deferral",
            "Review grouping deferred: AI grouping is temporarily unavailable",
            details={"stage": "grouping"},
        )
        conn.commit()
        return result

    with review_groups._atomic(conn):
        _migrate_legacy_proposals(conn)
        result.update(review_groups.reconcile_plan(conn, plan.groups, plan.ambiguous))
        review_groups._reconcile_all_managed_review_tasks(conn)
    # The orchestration boundary owns this batch. Persist local reconciliation
    # before any claim, then each claim is itself committed before ClickUp.
    conn.commit()

    automatic = bool(profile.get("auto_create_review_tasks"))
    create = create_fn or clickup_client.create_review_task
    for group in review_groups.open_groups(conn):
        state = group["creation_state"]
        if state in ("created", "failed", "uncertain", "creating"):
            continue
        if not automatic:
            review_groups.mark_disabled(conn, group["id"])
            result["disabled"] += 1
            continue
        if not clickup_ready:
            review_groups.mark_deferred(conn, group["id"], "ClickUp is unavailable")
            events.record(
                conn,
                "review_deferral",
                f"Review task creation deferred: {group['title']}",
                details={"stage": "creation", "group_id": group["id"]},
            )
            conn.commit()
            result["deferred"] += 1
            continue
        if not review_groups.claim_creation(conn, group["id"]):
            continue
        payload = _creation_payload(conn, review_groups._get_group(conn, group["id"]))
        try:
            created = create(payload)
        except Exception as error:
            state = _creation_failure_state(error)
            _mark_creation_error(conn, group["id"], state)
            result[state] += 1
            continue
        if not isinstance(created, dict) or not created.get("id"):
            _mark_creation_error(conn, group["id"], "uncertain")
            result["uncertain"] += 1
            continue
        try:
            _record_creation(conn, group, payload, created)
        except Exception:
            _mark_creation_error(conn, group["id"], "uncertain")
            result["uncertain"] += 1
            continue
        result["created"] += 1
    return result


def retry_group_creation(conn, group_id: int, *, create_fn=None) -> dict:
    """Explicitly retry one definite failed/deferred ClickUp creation claim."""
    group = review_groups._get_group(conn, group_id)
    if group["creation_state"] not in ("failed", "deferred"):
        raise ValueError("review group is not retryable")
    if not review_groups.claim_creation(conn, group_id, allow_failed=True):
        raise ValueError("review group is not retryable")

    group = review_groups._get_group(conn, group_id)
    payload = _creation_payload(conn, group)
    create = create_fn or clickup_client.create_review_task
    try:
        created = create(payload)
    except Exception as error:
        _mark_creation_error(conn, group_id, _creation_failure_state(error))
        return review_groups._get_group(conn, group_id)
    if not isinstance(created, dict) or not created.get("id"):
        _mark_creation_error(conn, group_id, "uncertain")
        return review_groups._get_group(conn, group_id)
    try:
        _record_creation(conn, group, payload, created)
    except Exception:
        _mark_creation_error(conn, group_id, "uncertain")
    return review_groups._get_group(conn, group_id)
