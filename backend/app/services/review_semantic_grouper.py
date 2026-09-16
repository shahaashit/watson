"""Validated, same-author semantic grouping for unresolved review MRs."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import classifier


PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "group_reviews.txt"
AI_AUTO_GROUP_THRESHOLD = 0.85
MAX_TITLE = 300
MAX_DESCRIPTION = 1200
MAX_BRANCH = 300
MAX_EXISTING_TITLE = 300
REQUEST_TIMEOUT_SECONDS = 30.0


class SemanticGroupingDeferred(RuntimeError):
    """The model result is unsafe to apply; defer the complete batch."""


@dataclass(frozen=True)
class SemanticGroup:
    member_mr_ids: tuple[str, ...]
    existing_group_id: int | None
    title: str
    reasoning: str
    confidence: float


@dataclass(frozen=True)
class SemanticPlan:
    groups: tuple[SemanticGroup, ...]
    ambiguous: tuple[SemanticGroup, ...]


class _ResponseGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_mr_ids: list[str]
    existing_group_id: int | None = None
    title: str
    reasoning: str
    confidence: float = Field(ge=0, le=1)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[_ResponseGroup]


def _bounded(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _build_prompt(candidates, existing_groups) -> str:
    candidate_rows = [
        {
            "mr_id": candidate.mr_id,
            "project": _bounded(candidate.project, MAX_TITLE),
            "title": _bounded(candidate.title, MAX_TITLE),
            "description": _bounded(candidate.description, MAX_DESCRIPTION),
            "source_branch": _bounded(candidate.source_branch, MAX_BRANCH),
        }
        for candidate in candidates
    ]
    existing_rows = [
        {
            "id": int(group["id"]),
            "title": _bounded(group.get("title"), MAX_EXISTING_TITLE),
            "mr_ids": list(group.get("mr_ids") or []),
        }
        for group in existing_groups
    ]
    return (
        PROMPT_PATH.read_text()
        .replace("{candidates}", json.dumps(candidate_rows, ensure_ascii=False))
        .replace("{existing_groups}", json.dumps(existing_rows, ensure_ascii=False))
    )


def _call_model(prompt: str):
    import anthropic

    config = classifier.anthropic_config()
    if not config["api_key"] or not config["model"]:
        raise RuntimeError("Anthropic is not configured")
    client = anthropic.Anthropic(
        api_key=config["api_key"],
        base_url=config["base_url"] or None,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    message = client.messages.create(
        model=config["model"],
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def _is_open_target(conn, group_id: int) -> bool:
    from . import review_groups, work_suppression
    row = conn.execute(
        "SELECT work_item_id, clickup_task_id FROM review_groups WHERE id=?",
        (group_id,),
    ).fetchone()
    if row is None:
        return False
    if work_suppression.group_removed(conn, review_groups._get_group(conn, group_id)):
        return False
    if row["work_item_id"] is not None:
        item = conn.execute(
            "SELECT state, origin FROM work_items WHERE id=?", (row["work_item_id"],)
        ).fetchone()
        if item is None or item["state"] == "done" or item["origin"] == "ignored":
            return False
    if row["clickup_task_id"]:
        managed = conn.execute(
            "SELECT status FROM managed_tasks WHERE clickup_task_id=?",
            (row["clickup_task_id"],),
        ).fetchone()
        if managed is not None and managed["status"] != "open":
            return False
    return True


def group_by_context(
    conn,
    candidates,
    existing_groups,
    *,
    completion_fn: Callable[[str], object] | None = None,
) -> SemanticPlan:
    """Call the model once and accept only a complete, trusted partition."""
    if not candidates:
        return SemanticPlan(groups=(), ambiguous=())
    from . import work_suppression
    if any(work_suppression.mr_removed(conn, candidate.mr_id) for candidate in candidates):
        raise SemanticGroupingDeferred('work was removed before grouping')
    supplied = {candidate.mr_id: candidate for candidate in candidates}
    if len(supplied) != len(candidates):
        raise SemanticGroupingDeferred("duplicate supplied MR")
    completion = completion_fn or _call_model
    try:
        raw = completion(_build_prompt(candidates, existing_groups))
        data = raw if isinstance(raw, dict) else classifier.extract_json(str(raw))
        parsed = _Response.model_validate(data)
    except (Exception, ValidationError) as exc:
        raise SemanticGroupingDeferred("semantic grouping unavailable") from exc

    existing_by_id = {int(group["id"]): group for group in existing_groups}
    seen: set[str] = set()
    accepted = []
    ambiguous = []
    for parsed_group in parsed.groups:
        members = tuple(parsed_group.member_mr_ids)
        if not members or len(members) != len(set(members)):
            raise SemanticGroupingDeferred("invalid membership")
        if any(mr_id not in supplied for mr_id in members) or seen.intersection(members):
            raise SemanticGroupingDeferred("unknown or repeated MR")
        authors = {supplied[mr_id].author_username for mr_id in members}
        if len(authors) != 1:
            raise SemanticGroupingDeferred("cross-author AI group")
        target_id = parsed_group.existing_group_id
        if target_id is not None:
            target = existing_by_id.get(target_id)
            if (
                target is None
                or target.get("author_username") not in authors
                or not _is_open_target(conn, target_id)
            ):
                raise SemanticGroupingDeferred("invalid existing group target")
        seen.update(members)
        group = SemanticGroup(
            member_mr_ids=members,
            existing_group_id=target_id,
            title=parsed_group.title.strip(),
            reasoning=parsed_group.reasoning.strip(),
            confidence=parsed_group.confidence,
        )
        if not group.title or not group.reasoning:
            raise SemanticGroupingDeferred("blank semantic group metadata")
        (accepted if group.confidence >= AI_AUTO_GROUP_THRESHOLD else ambiguous).append(group)

    if seen != set(supplied):
        raise SemanticGroupingDeferred("incomplete membership")
    return SemanticPlan(groups=tuple(accepted), ambiguous=tuple(ambiguous))
