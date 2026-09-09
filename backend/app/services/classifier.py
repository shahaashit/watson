"""LLM classification pipeline: raw capture → entries, reminders, drafts.

Contract:
  * NEVER raises. On any error (network, parse, validation), the capture is
    marked classified_at with classification_json=NULL — the UI then shows a
    `needs manual review` badge. Raw text MUST NOT be lost (capture-first).
  * Drafts ClickUp actions but does NOT execute them — that's actions.py on
    user approve (see CLAUDE.md: approve-before-write).
  * The prompt at `prompts/classify.txt` is USER-EDITABLE and hot-read on
    every call. Keep the {placeholders} the build_prompt step substitutes.
  * Allowed categories in the output are ONLY 'Discussion' or 'Review' —
    the prompt enforces this; do not relax it without an explicit user ask.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from ..config import settings
from ..models import _json_list, now_iso
from ..schemas import Classification
from . import app_settings, external_errors, matcher, secret_store

log = logging.getLogger("watson.classifier")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "classify.txt"

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


# --- date resolution -----------------------------------------------------

def resolve_due_at(value, today: datetime = None):
    """Resolve an ISO or relative date phrase to an ISO datetime string.

    The LLM is asked to return ISO already; this is a defensive fallback for
    phrases like "Friday", "tomorrow", "EOD", "next tuesday", "in 3 days".
    Returns None when the value can't be resolved.
    """
    if value is None:
        return None
    today = today or datetime.now()
    v = str(value).strip()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v).isoformat(timespec="seconds")
    except ValueError:
        pass

    low = re.sub(r"\s+", " ", v.lower()).strip()
    base = today.replace(hour=10, minute=0, second=0, microsecond=0)

    if low in ("today",):
        return base.isoformat(timespec="seconds")
    if low in ("eod", "end of day", "today eod"):
        return today.replace(hour=18, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if low == "tomorrow":
        return (base + timedelta(days=1)).isoformat(timespec="seconds")
    if low == "next week":
        days = 7 - today.weekday()  # next Monday
        return (base + timedelta(days=days)).isoformat(timespec="seconds")

    m = re.fullmatch(r"in (\d+) days?", low)
    if m:
        return (base + timedelta(days=int(m.group(1)))).isoformat(timespec="seconds")
    m = re.fullmatch(r"in (\d+) hours?", low)
    if m:
        return (today + timedelta(hours=int(m.group(1)))).replace(microsecond=0).isoformat(timespec="seconds")

    m = re.fullmatch(r"(next )?([a-z]+)( eod)?", low)
    if m and m.group(2) in WEEKDAYS:
        target = WEEKDAYS[m.group(2)]
        if m.group(1):  # "next X" = X of next calendar week
            days = (7 - today.weekday()) + target
        else:  # bare weekday = next occurrence (never today)
            days = (target - today.weekday()) % 7 or 7
        resolved = base + timedelta(days=days)
        if m.group(3):
            resolved = resolved.replace(hour=18)
        return resolved.isoformat(timespec="seconds")

    return None


# --- prompt construction -------------------------------------------------

def _known_values(conn, column: str, limit: int = 200) -> list:
    rows = conn.execute(
        f"SELECT {column} FROM entries ORDER BY id DESC LIMIT {int(limit)}"
    ).fetchall()
    seen = []
    for row in rows:
        try:
            for val in json.loads(row[0] or "[]"):
                if val and val not in seen:
                    seen.append(val)
        except (json.JSONDecodeError, TypeError):
            continue
    return seen


def build_prompt(conn, raw_text: str, today: datetime = None) -> str:
    today = today or datetime.now()
    template = PROMPT_PATH.read_text()

    rows = conn.execute(
        "SELECT task_id, name, status FROM clickup_tasks_cache ORDER BY synced_at DESC LIMIT 200"
    ).fetchall()
    tasks = "\n".join(f"{r['task_id']} | {r['name']} | {r['status']}" for r in rows) or "(none cached)"

    people = ", ".join(_known_values(conn, "people")) or "(none yet)"
    tags = ", ".join(_known_values(conn, "tags")) or "(none yet)"

    erows = conn.execute(
        "SELECT id, type, title, tags, created_at FROM entries"
        " ORDER BY id DESC LIMIT 30"
    ).fetchall()
    recent_entries = "\n".join(
        f"#{r['id']} | {r['created_at'][:10]} | {r['type']} | {r['title']}"
        f" | tags: {', '.join(_json_list(r['tags'])) or '-'}"
        for r in erows
    ) or "(no prior entries)"

    # Open managed_tasks (Watson cards) so the LLM can route captures about
    # existing work back onto the right card instead of drafting a duplicate
    # task. Each row gives: this card's related-ClickUp id (the strongest
    # match signal), its name, and the MRs already linked to it.
    mt_rows = conn.execute(
        "SELECT mt.id AS mt_id, mt.related_clickup_task_id, mt.related_mr_id,"
        "       mt.additional_mr_ids, ct.name AS related_name, ct.status AS related_status"
        " FROM managed_tasks mt"
        " LEFT JOIN clickup_tasks_cache ct ON ct.task_id = mt.related_clickup_task_id"
        " WHERE mt.status = 'open' AND mt.related_clickup_task_id IS NOT NULL"
        " ORDER BY mt.id DESC LIMIT 40"
    ).fetchall()
    def _mt_line(r):
        extras = []
        try:
            for x in json.loads(r["additional_mr_ids"] or "[]"):
                if x:
                    extras.append(x)
        except Exception:
            pass
        mrs = ", ".join(filter(None, [r["related_mr_id"]] + extras)) or "(none)"
        return (f"{r['related_clickup_task_id']} | {(r['related_name'] or '')[:80]}"
                f" | status:{r['related_status'] or '-'} | mrs:{mrs}")
    managed_tasks_block = "\n".join(_mt_line(r) for r in mt_rows) or "(no open Watson cards)"

    # GitLab MRs the user is involved in (cached) — gives the model real titles
    # to use when the capture mentions an MR by number/branch/project.
    mrows = conn.execute(
        "SELECT mr_id, project, title, state, author, source_branch"
        " FROM gitlab_mrs_cache ORDER BY updated_at DESC LIMIT 60"
    ).fetchall()
    def _mr_line(r):
        parts = [r["mr_id"], r["project"], (r["title"] or "")[:90], r["state"]]
        if r["author"]:
            parts.append(f"by {r['author']}")
        if r["source_branch"]:
            parts.append(f"branch:{r['source_branch']}")
        return " | ".join(parts)
    gitlab_mrs = "\n".join(_mr_line(r) for r in mrows) or "(no MRs cached)"

    # Contacts — name → email pairs harvested from past calendar attendees.
    # The LLM uses this to populate `attendees` on gcal_create_event drafts:
    # when the capture says "Morgan", the model finds "Morgan Lee
    # <morgan.lee@…>" here and includes the email. Populated incrementally by
    # gcal sync, or in bulk by `python -m app.auth.gcal_backfill`.
    from . import gcal_client
    try:
        contact_pairs = gcal_client.known_contacts(conn, limit=60)
    except Exception:
        contact_pairs = []
    if contact_pairs:
        gcal_contacts = "\n".join(
            f"- {name or '(no name)'} <{email}>" for name, email in contact_pairs
        )
    else:
        gcal_contacts = "(no contacts known yet — run `python -m app.auth.gcal_backfill`)"

    # .replace, not .format: the template contains literal JSON braces and is
    # user-edited, so format() would explode on them.
    return (
        template
        .replace("{today}", today.strftime("%Y-%m-%d"))
        .replace("{weekday}", today.strftime("%A"))
        .replace("{tz}", settings.tz)
        .replace("{user_name}", settings.watson_user_name)
        .replace("{clickup_tasks}", tasks)
        .replace("{known_people}", people)
        .replace("{known_tags}", tags)
        .replace("{recent_entries}", recent_entries)
        .replace("{managed_tasks}", managed_tasks_block)
        .replace("{gitlab_mrs}", gitlab_mrs)
        .replace("{gcal_contacts}", gcal_contacts)
        .replace("{capture_text}", raw_text)
    )


# --- LLM call + parsing --------------------------------------------------

def anthropic_config() -> dict:
    """Resolve the current Anthropic-compatible client settings per call."""
    if app_settings.integration_disabled("anthropic"):
        return MappingProxyType({"api_key": "", "base_url": "", "model": ""})
    try:
        api_key = secret_store.effective_secret(
            "anthropic.api_key", settings.anthropic_api_key
        )
        base_url = app_settings.effective_nonsecret(
            "integration.anthropic.base_url", settings.anthropic_base_url
        )
        model = app_settings.effective_nonsecret(
            "integration.anthropic.model", settings.anthropic_model
        )
    except Exception:
        return MappingProxyType({"api_key": "", "base_url": "", "model": ""})
    return MappingProxyType({
        "api_key": api_key,
        "base_url": str(base_url or ""),
        "model": str(model or ""),
    })


def call_llm(prompt: str) -> str:
    import anthropic

    config = anthropic_config()
    if not config["api_key"]:
        raise RuntimeError("Anthropic is not configured")
    client = anthropic.Anthropic(
        api_key=config["api_key"],
        base_url=config["base_url"] or None,
    )
    message = client.messages.create(
        model=config["model"],
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def extract_json(text: str) -> dict:
    """Pull a JSON object out of model output, tolerating fences and prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(t[start:end + 1])


def parse_classification(text: str) -> Classification:
    return Classification.model_validate(extract_json(text))


# --- orchestration -------------------------------------------------------

def classify_capture(conn, capture_id: int, raw_text: str, work_item_id: int | None = None) -> dict:
    """Classify a stored capture and fan out entries/reminders/drafts.

    Never raises: on any failure the capture is marked classified_at with a
    null classification_json so the UI shows a "needs manual review" badge.
    """
    try:
        # if the capture pastes an MR URL we don't yet know about, fetch & cache
        # it before building the prompt — gives the LLM the real title to use
        # (and lets the Work card surface the MR even without prior sync).
        from . import gitlab_client
        try:
            gitlab_client.ensure_mrs_cached(conn, raw_text)
        except Exception as exc:
            external_errors.log_failure(
                log, "ensure_mrs_cached", "gitlab", exc
            )
        prompt = build_prompt(conn, raw_text)
        raw_response = call_llm(prompt)
        result = parse_classification(raw_response)
    except Exception as exc:  # LLM/network/parse failure: keep the capture
        external_errors.log_failure(
            log, f"classification for capture {capture_id}", "anthropic", exc
        )
        conn.execute(
            "UPDATE captures SET classified_at = ? WHERE id = ?",
            (now_iso(), capture_id),
        )
        conn.commit()
        return {
            "status": "needs_review",
            "error": external_errors.details("anthropic", exc),
        }

    created = {"entries": [], "reminders": [], "actions": []}
    first_entry_id = None

    for entry in result.entries:
        # only thread under a real, pre-existing entry; ignore hallucinated ids
        parent_id, parent_title = None, None
        if entry.follows_up_entry_id:
            prow = conn.execute(
                "SELECT id, title FROM entries WHERE id = ?", (entry.follows_up_entry_id,)
            ).fetchone()
            if prow:
                parent_id, parent_title = prow["id"], prow["title"]
        cur = conn.execute(
            "INSERT INTO entries (capture_id, parent_entry_id, work_item_id, type, title, body,"
            " people, tags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, parent_id, work_item_id, entry.type, entry.title, entry.body,
             json.dumps(entry.people), json.dumps(entry.tags), now_iso()),
        )
        if first_entry_id is None:
            first_entry_id = cur.lastrowid
        created["entries"].append({
            "id": cur.lastrowid, "type": entry.type, "title": entry.title,
            "parent_entry_id": parent_id, "parent_title": parent_title,
        })

    for reminder in result.reminders:
        due = resolve_due_at(reminder.due_at)
        if not due:
            log.warning("unresolvable due date %r, skipping reminder", reminder.due_at)
            continue
        cur = conn.execute(
            "INSERT INTO reminders (entry_id, capture_id, work_item_id, text, due_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (first_entry_id, capture_id, work_item_id, reminder.text, due, now_iso()),
        )
        created["reminders"].append({"id": cur.lastrowid, "text": reminder.text, "due_at": due})

    for action in result.clickup_actions:
        # Context-aware routing: if the LLM identified this capture as being
        # about work that already has a Watson card, redirect the action away
        # from "create a new task" — either link the MR to the existing card,
        # or suppress the action entirely. NEVER duplicates a task.
        if action.kind == "clickup_create_task" and action.existing_work_clickup_id:
            mt_row = conn.execute(
                "SELECT id, related_mr_id, additional_mr_ids FROM managed_tasks"
                " WHERE status = 'open' AND related_clickup_task_id = ?",
                (action.existing_work_clickup_id,),
            ).fetchone()
            if mt_row:
                # only draft a link if there's an MR to link AND it's a real,
                # cached MR not already attached to that card
                if action.suggested_mr_id:
                    already = (action.suggested_mr_id == mt_row["related_mr_id"]
                               or action.suggested_mr_id in _json_list(mt_row["additional_mr_ids"]))
                    mr_cached = conn.execute(
                        "SELECT title FROM gitlab_mrs_cache WHERE mr_id = ?",
                        (action.suggested_mr_id,),
                    ).fetchone()
                    if mr_cached and not already:
                        ct_row = conn.execute(
                            "SELECT name FROM clickup_tasks_cache WHERE task_id = ?",
                            (action.existing_work_clickup_id,),
                        ).fetchone()
                        payload = {
                            "managed_task_id": mt_row["id"],
                            "related_mr_id": action.suggested_mr_id,
                            "mr_title": mr_cached["title"] or action.suggested_mr_id,
                            "task_name": (ct_row["name"] if ct_row else "") or action.existing_work_clickup_id,
                            "match_score": 100,  # the LLM matched explicitly, not by fuzz
                            "via_capture": True,
                        }
                        cur = conn.execute(
                            "INSERT INTO pending_actions (capture_id, kind, target_id,"
                            " payload_json, match_confidence, created_at)"
                            " VALUES (?, 'link_mr_to_task', ?, ?, 1.0, ?)",
                            (capture_id, str(mt_row["id"]), json.dumps(payload), now_iso()),
                        )
                        created["actions"].append({
                            "id": cur.lastrowid, "kind": "link_mr_to_task",
                            "target_id": str(mt_row["id"]), "match_confidence": 1.0,
                        })
                # whether we linked or not, NEVER fall through to create
                continue
            # invalid existing_work_clickup_id (no matching managed_task) →
            # fall through and treat as a normal create_task draft below
        # We only create NEW tasks assigned to the user — never target someone
        # else's task. A matched task is kept as context, not as a write target.
        target_id, confidence, related_task_id, related_mr_id = None, None, None, None
        if action.kind == "clickup_create_task":
            if action.suggested_task_id and conn.execute(
                "SELECT 1 FROM clickup_tasks_cache WHERE task_id = ?",
                (action.suggested_task_id,),
            ).fetchone():
                related_task_id = action.suggested_task_id  # real task to link to
                confidence = 0.95  # informational: a related task exists
            # validate the suggested MR against the cache too — never trust IDs
            # the model might invent, and let `_clickup<id>` branches also link
            # us back to a related ClickUp task when one wasn't suggested.
            if action.suggested_mr_id:
                mr_row = conn.execute(
                    "SELECT source_branch FROM gitlab_mrs_cache WHERE mr_id = ?",
                    (action.suggested_mr_id,),
                ).fetchone()
                if mr_row:
                    related_mr_id = action.suggested_mr_id
                    if not related_task_id:
                        from . import gitlab_client
                        bid = gitlab_client.clickup_id_from_branch(mr_row["source_branch"])
                        if bid and conn.execute(
                            "SELECT 1 FROM clickup_tasks_cache WHERE task_id = ?", (bid,)
                        ).fetchone():
                            related_task_id = bid
        else:
            # legacy comment/status paths still resolve a target if ever emitted
            target_id = action.suggested_task_id
            if target_id and not conn.execute(
                "SELECT 1 FROM clickup_tasks_cache WHERE task_id = ?", (target_id,)
            ).fetchone():
                target_id = None
            if not target_id:
                match, score = matcher.match_tasks_from_cache(
                    conn, action.task_match_query or str(action.draft))
                if match and score >= matcher.LOW_CONFIDENCE:
                    target_id, confidence = match["task_id"], score
        payload = {"draft": action.draft, "task_match_query": action.task_match_query,
                   "related_task_id": related_task_id, "related_mr_id": related_mr_id}
        cur = conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " match_confidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (capture_id, action.kind, target_id, json.dumps(payload), confidence, now_iso()),
        )
        created["actions"].append({
            "id": cur.lastrowid, "kind": action.kind,
            "target_id": target_id, "match_confidence": confidence,
        })

    # LLM-drafted calendar events. Never executed here — dropped into
    # pending_actions so the user sees them in the "Pulling on you" inbox
    # and clicks Approve to actually create the event (approve-before-write).
    for gce in result.gcal_events:
        title = (gce.title or "").strip()
        start_at = resolve_due_at(gce.start_at) or (gce.start_at or "").strip()
        end_at = resolve_due_at(gce.end_at) if gce.end_at else ""
        if not (title and start_at):
            log.warning("skipping gcal_event draft — missing title/start_at: %r", gce)
            continue
        if not end_at:
            try:
                start_dt = datetime.fromisoformat(start_at)
                end_dt = start_dt + timedelta(minutes=max(5, gce.duration_minutes or 30))
                end_at = end_dt.isoformat(timespec="seconds")
            except (ValueError, TypeError):
                log.warning("could not derive end_at from start_at %r; skipping", start_at)
                continue
        # Filter obvious placeholder attendees the LLM may hallucinate — only
        # keep entries that look like email addresses. The user can add more
        # via the Edit affordance before approving.
        attendees = [a for a in (gce.attendees or []) if "@" in a and "." in a.split("@")[-1]]
        payload = {
            "draft": {
                "title": title,
                "start_at": start_at,
                "end_at": end_at,
                "attendees": attendees,
                "description": gce.description or "",
                "add_meet": bool(gce.add_meet),
            }
        }
        cur = conn.execute(
            "INSERT INTO pending_actions (capture_id, kind, target_id, payload_json,"
            " match_confidence, created_at) VALUES (?, 'gcal_create_event', NULL, ?, 1.0, ?)",
            (capture_id, json.dumps(payload), now_iso()),
        )
        created["actions"].append({
            "id": cur.lastrowid, "kind": "gcal_create_event",
            "target_id": None, "match_confidence": 1.0,
        })

    conn.execute(
        "UPDATE captures SET classified_at = ?, classification_json = ? WHERE id = ?",
        (now_iso(), result.model_dump_json(), capture_id),
    )
    conn.commit()
    created["status"] = "classified"
    return created
