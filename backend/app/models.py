"""Row-to-dict serializers for API responses. Storage is raw sqlite3."""
import json
from datetime import datetime


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_list(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _json_dict(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def person_dict(row) -> dict:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "is_self": bool(row["is_self"]),
        "is_tracked": bool(row["is_tracked"]),
        "lane_position": row["lane_position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def work_item_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"] or "",
        "title_is_manual": bool(row["title_is_manual"]),
        "state": row["state"],
        "owner_person_id": row["owner_person_id"],
        "owner_display": row["owner_display"] or "",
        "position": row["position"],
        "priority_position": row["priority_position"],
        "origin": row["origin"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def work_link_dict(row) -> dict:
    return {
        "id": row["id"],
        "work_item_id": row["work_item_id"],
        "source_type": row["source_type"],
        "external_id": row["external_id"],
        "url": row["url"] or "",
        "label": row["label"] or "",
        "created_at": row["created_at"],
    }


def work_activity_dict(row) -> dict:
    return {
        "id": row["id"],
        "work_item_id": row["work_item_id"],
        "activity_type": row["activity_type"],
        "body": row["body"] or "",
        "metadata": _json_dict(row["metadata_json"]),
        "capture_id": row["capture_id"],
        "reminder_id": row["reminder_id"],
        "pending_action_id": row["pending_action_id"],
        "created_at": row["created_at"],
    }


def work_inbox_dict(row) -> dict:
    return {
        "id": row["id"],
        "capture_id": row["capture_id"],
        "suggested_work_item_id": row["suggested_work_item_id"],
        "confidence": row["confidence"],
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def capture_dict(row) -> dict:
    classified_at = row["classified_at"]
    has_result = bool(row["classification_json"])
    if not classified_at:
        status = "pending"
    elif has_result:
        status = "classified"
    else:
        status = "needs_review"
    return {
        "id": row["id"],
        "raw_text": row["raw_text"],
        "created_at": row["created_at"],
        "classified_at": classified_at,
        "status": status,
    }


def entry_dict(row) -> dict:
    keys = row.keys()
    d = {
        "id": row["id"],
        "capture_id": row["capture_id"],
        "parent_entry_id": row["parent_entry_id"] if "parent_entry_id" in keys else None,
        "type": row["type"],
        "title": row["title"],
        "body": row["body"] or "",
        "people": _json_list(row["people"]),
        "tags": _json_list(row["tags"]),
        "created_at": row["created_at"],
    }
    if "capture_raw" in keys:
        d["capture_raw"] = row["capture_raw"]
    return d


def reminder_dict(row) -> dict:
    return {
        "id": row["id"],
        "entry_id": row["entry_id"],
        "text": row["text"],
        "due_at": row["due_at"],
        "status": row["status"],
        "snoozed_until": row["snoozed_until"],
        "created_at": row["created_at"],
    }


def action_dict(row) -> dict:
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        "id": row["id"],
        "capture_id": row["capture_id"],
        "kind": row["kind"],
        "target_id": row["target_id"],
        "payload": payload,
        "match_confidence": row["match_confidence"],
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "error": row["error"],
    }


def task_dict(row) -> dict:
    return {
        "task_id": row["task_id"],
        "name": row["name"],
        "status": row["status"],
        "list_name": row["list_name"],
        "url": row["url"],
        "assignees": _json_list(row["assignees"]),
        "due_date": row["due_date"],
        "synced_at": row["synced_at"],
    }


def mr_dict(row) -> dict:
    # Import here to avoid a top-level cycle (services → models → services).
    from .services.gitlab_client import clickup_id_from_mr
    keys = row.keys()
    roles = _json_list(row["roles"]) if "roles" in keys else []
    if not roles and row["role"]:
        roles = [row["role"]]
    return {
        "mr_id": row["mr_id"],
        "project": row["project"],
        "title": row["title"],
        "state": row["state"],
        "url": row["url"],
        "role": row["role"],
        "roles": roles,
        "author": row["author"] if "author" in keys else "",
        "author_username": (
            row["author_username"] if "author_username" in keys else ""
        ) or "",
        "assignee_usernames": (
            _json_list(row["assignee_usernames"])
            if "assignee_usernames" in keys else []
        ),
        "reviewer_usernames": (
            _json_list(row["reviewer_usernames"])
            if "reviewer_usernames" in keys else []
        ),
        "source_branch": row["source_branch"] if "source_branch" in keys else "",
        "updated_at": row["updated_at"],
        "synced_at": row["synced_at"],
        # ClickUp task id extracted from the branch tag (or MR description
        # fallback), when the MR carries one. Two MRs sharing this id are
        # the SAME logical change split across repos — the Today MR table
        # groups them into one row so the review queue reads as tasks, not
        # individual repos. Empty string when no tag is present.
        "clickup_id": clickup_id_from_mr(row) or "",
        # Review lifecycle tags computed during sync from state + labels
        # + comment scan. An MR can carry multiple concurrently, e.g.
        # ["reviewed_by_me", "qa"] means "I signed off, then it moved
        # to QA". Terminal states (merged/closed) stand alone. Author's
        # own MRs get []. See gitlab_client._compute_stages.
        "stages": _json_list(row["stages"]) if "stages" in keys else [],
        "labels": _json_list(row["labels"]) if "labels" in keys else [],
    }


def gcal_event_dict(row) -> dict:
    keys = row.keys()
    return {
        "event_id": row["event_id"],
        "calendar_id": row["calendar_id"],
        "title": row["title"],
        "description": row["description"] or "",
        "location": row["location"] or "",
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "all_day": bool(row["all_day"]),
        "organizer": row["organizer"] or "",
        "attendees": _json_list(row["attendees"]),
        "my_response": row["my_response"] or "",
        "html_link": row["html_link"] or "",
        # meet_link is a migrated column — guard for pre-migration rows.
        "meet_link": (row["meet_link"] if "meet_link" in keys else "") or "",
        "synced_at": row["synced_at"],
    }


def flock_mention_dict(row) -> dict:
    keys = row.keys()
    return {
        "jid": row["jid"],
        "name": row["name"],
        "is_group": bool(row["is_group"]),
        "has_mention": bool(row["has_mention"]),
        "unread_count": row["unread_count"],
        "last_message_time": row["last_message_time"],
        "is_muted": bool(row["is_muted"]),
        "notify_on": row["notify_on"] or "",
        "bucket": row["bucket"] or "",
        "mentions": _json_list(row["mentions_json"]) if "mentions_json" in keys else [],
        "synced_at": row["synced_at"],
    }


def flock_webhook_mention_dict(row) -> dict:
    """Serializer for a row from flock_webhook_mentions. Shaped to be merged
    into the same `flock` array as sidebar-DM entries — the Today card renders
    both through one path."""
    return {
        "jid": row["channel_jid"],
        "name": row["channel_name"],
        "is_group": True,
        "has_mention": True,     # webhook filter already asserted this
        "unread_count": 1,        # one message per row; UI aggregates by channel
        "last_message_time": row["received_at"],
        "is_muted": False,
        "notify_on": "MENTIONS",
        "bucket": "webhook",      # distinct from sidebar 'open' / 'muted' buckets
        "mentions": [{
            "id": row["id"],
            "sender": row["sender_jid"],
            "sender_name": row["sender_jid"].split("@")[0],  # best-effort until name resolution
            "text": row["text"],
        }],
        "synced_at": row["received_at"],
    }
