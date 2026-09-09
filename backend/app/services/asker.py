"""Ask mode: answer natural-language questions over stored entries + reminders.

Right-sized retrieval for a single-user local dataset — no embeddings. We hand the
model a compact, dated view of recent records and let it filter and summarize.
"""
import logging
import re
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..models import _json_list
from . import classifier

log = logging.getLogger("watson.asker")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "ask.txt"

# bound the context so a long history doesn't blow the token budget
MAX_ENTRIES = 300
MAX_REMINDERS = 120


def call_llm(prompt: str) -> str:
    """Use the classifier's call-time Anthropic boundary for Ask requests."""
    return classifier.call_llm(prompt)


def _render_entries(conn) -> str:
    rows = conn.execute(
        "SELECT id, parent_entry_id, type, title, body, people, tags, created_at"
        " FROM entries ORDER BY id DESC LIMIT ?",
        (MAX_ENTRIES,),
    ).fetchall()
    if not rows:
        return "(no entries logged yet)"

    children = {}
    roots = []
    for r in rows:
        if r["parent_entry_id"]:
            children.setdefault(r["parent_entry_id"], []).append(r)
        else:
            roots.append(r)

    def line(r, indent=""):
        people = ", ".join(_json_list(r["people"]))
        tags = ", ".join(_json_list(r["tags"]))
        meta = " | ".join(x for x in (
            r["created_at"][:16].replace("T", " "),
            r["type"],
            f"tags: {tags}" if tags else "",
            f"with: {people}" if people else "",
        ) if x)
        out = f"{indent}[#{r['id']}] {meta}\n{indent}  {r['title']}"
        if r["body"]:
            body = r["body"].strip().replace("\n", " ")
            out += f"\n{indent}  {body}"
        return out

    blocks = []
    for r in roots:
        block = [line(r)]
        for c in sorted(children.get(r["id"], []), key=lambda x: x["id"]):
            block.append(line(c, indent="    ↳ "))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _render_reminders(conn) -> str:
    rows = conn.execute(
        "SELECT text, due_at, status, snoozed_until FROM reminders"
        " ORDER BY due_at DESC LIMIT ?",
        (MAX_REMINDERS,),
    ).fetchall()
    if not rows:
        return "(no reminders)"
    out = []
    for r in rows:
        when = (r["snoozed_until"] or r["due_at"] or "")[:16].replace("T", " ")
        out.append(f"- [{r['status']}] {r['text']} (due {when})")
    return "\n".join(out)


def _render_gitlab_mrs(conn) -> str:
    """MRs in the local cache, one per line, with title + url. The Ask prompt
    feeds this so the model can resolve `!iid` references in entry bodies to
    real titles and emit clickable markdown links."""
    rows = conn.execute(
        "SELECT mr_id, project, title, state, url, author FROM gitlab_mrs_cache"
        " ORDER BY updated_at DESC LIMIT 80"
    ).fetchall()
    if not rows:
        return "(no MRs cached)"
    return "\n".join(
        f"- {r['mr_id']} | {r['project']} | {r['title']} | {r['state']}"
        f" | by {r['author'] or '?'} | url: {r['url']}"
        for r in rows
    )


def _render_clickup_tasks(conn) -> str:
    """ClickUp tasks referenced by Watson's managed tasks. Two kinds land here:

      * `mine`   — tasks Watson created for the user (e.g. "Review — <MR title>")
                   or tasks where the user is an assignee. THESE are `{user_name}`'s
                   own tasks.
      * `linked` — tasks Watson's review/discussion tasks REFERENCE (the original
                   dev work by someone else on the team). Included so questions
                   about "what did I review" can find the underlying task; do
                   NOT list them under "my tasks" questions.

    Each line includes an `owner:` tag with the assignees list so the LLM can
    filter correctly per question. date_closed is included for date-range
    questions ('closed last 3 days')."""
    my_name = (settings.watson_user_name or "").strip().lower()
    rows = conn.execute(
        "SELECT ct.task_id, ct.name, ct.status, ct.date_closed, ct.url, ct.assignees,"
        "       mt.clickup_task_id AS mt_created,"
        "       mt.related_clickup_task_id AS mt_related"
        " FROM clickup_tasks_cache ct"
        " LEFT JOIN managed_tasks mt ON ct.task_id IN (mt.clickup_task_id, mt.related_clickup_task_id)"
        " WHERE ct.name IS NOT NULL AND ct.name != ''"
    ).fetchall()
    if not rows:
        return "(no tasks)"
    # Dedup — a task can appear via both created and related links.
    seen: dict = {}
    for r in rows:
        tid = r["task_id"]
        assignees = _json_list(r["assignees"])
        is_mine = (r["mt_created"] == tid) or any(
            my_name in (str(a) or "").lower() for a in assignees
        )
        kind = "mine" if is_mine else "linked"
        # Prefer "mine" classification if the same task appears both ways.
        prev = seen.get(tid)
        if prev is None or (kind == "mine" and prev.get("kind") == "linked"):
            seen[tid] = {"row": r, "kind": kind, "assignees": assignees}
    lines = []
    for entry in seen.values():
        r = entry["row"]
        closed = f" | closed_at: {r['date_closed']}" if r["date_closed"] else ""
        owners = ", ".join(entry["assignees"]) or "(unassigned)"
        lines.append(
            f"- [{entry['kind']}] {r['task_id']} | {r['name']} | {r['status']} | owner: {owners}"
            f"{closed} | url: {r['url']}"
        )
    return "\n".join(lines)


def _render_recent_activity(conn, days: int = 14) -> str:
    """Recent system_events (sync-detected changes, drafts, approvals) — the
    most accurate source of 'what happened when' for questions like
    'which tasks were closed in the last 3 days'."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT created_at, kind, subject, related_task_id, related_mr_id"
        " FROM system_events WHERE created_at >= ?"
        " ORDER BY created_at DESC LIMIT 200",
        (cutoff,),
    ).fetchall()
    if not rows:
        return "(no recent activity)"
    lines = []
    for r in rows:
        ref = ""
        if r["related_task_id"]:
            ref = f" | task_id: {r['related_task_id']}"
        elif r["related_mr_id"]:
            ref = f" | mr_id: {r['related_mr_id']}"
        lines.append(f"- {r['created_at']} | {r['kind']} | {r['subject']}{ref}")
    return "\n".join(lines)


def _render_flock_messages(conn, days: int = 7) -> str:
    """Render recent Flock activity: webhook-sourced @-mentions (real
    message text) + sidebar-DM entries (channel + unread count). Bounded
    lookback keeps the prompt from ballooning on a busy channel.

    Webhook rows carry actual text; sidebar-DM rows carry only metadata
    (Flock's read semantics erase message content once it hits the client).
    Both surfaces matter for "who messaged me" / "what did X say" questions.
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    lines = []

    # JID → display-name lookup so senders show as "Nikhil Kumar" instead
    # of raw "u:2thc2ap..." where we have the mapping.
    jid_to_name = {
        r["jid"]: r["name"]
        for r in conn.execute("SELECT jid, name FROM flock_contacts")
    }

    webhook_rows = conn.execute(
        "SELECT channel_name, sender_jid, text, received_at"
        " FROM flock_webhook_mentions WHERE received_at >= ?"
        " ORDER BY received_at DESC LIMIT 200",
        (cutoff,),
    ).fetchall()
    for r in webhook_rows:
        sender = jid_to_name.get(r["sender_jid"], r["sender_jid"])
        text = (r["text"] or "").replace("\n", " ").strip()[:280]
        lines.append(f"- [{r['received_at'][:16]}] #{r['channel_name']} — {sender}: {text}")

    sidebar_rows = conn.execute(
        "SELECT name, is_group, has_mention, unread_count, last_message_time"
        " FROM flock_mentions_cache"
        " ORDER BY has_mention DESC, last_message_time DESC LIMIT 50"
    ).fetchall()
    for r in sidebar_rows:
        prefix = "#" if r["is_group"] else "@"
        tag = "@-mention" if r["has_mention"] else f"{r['unread_count']} unread"
        ts = (r["last_message_time"] or "")[:16]
        lines.append(f"- [{ts}] {prefix}{r['name']} — {tag} (no content available)")

    if not lines:
        return "(no recent Flock activity)"
    return "\n".join(lines)


def _render_calendar(conn) -> str:
    """Today's calendar events, INCLUDING leave markers.

    The Today timeline filters leave markers out (they're noise on a
    schedule view), but Ask needs them so questions like "who's out today"
    can be answered. Reads gcal_events_cache directly rather than going
    through gcal_client.todays_events which applies the leave filter.

    Range: any event that overlaps today. Multi-day leave blocks that span
    today are included (that's exactly what makes them relevant).
    """
    from datetime import timedelta
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_end = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                 + timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT title, start_at, end_at, all_day, organizer, attendees, my_response"
        " FROM gcal_events_cache"
        " WHERE start_at < ? AND (end_at IS NULL OR end_at = '' OR end_at > ?)"
        " ORDER BY all_day DESC, start_at",
        (today_end, today_start),
    ).fetchall()
    if not rows:
        return "(no meetings on today's calendar)"

    lines = []
    for r in rows:
        if r["all_day"]:
            tag = "all-day"
        else:
            # bare HH:MM range for the human — full ISO in DB is noisy.
            s = (r["start_at"] or "")[:16].replace("T", " ")
            e = (r["end_at"] or "")[:16].replace("T", " ")
            tag = f"{s[-5:]}–{e[-5:]}" if s and e else s
        organizer = f" (organizer: {r['organizer']})" if r["organizer"] else ""
        lines.append(f"- [{tag}] {r['title'] or '(untitled)'}{organizer}")
    return "\n".join(lines)


def build_prompt(conn, question: str, today: datetime = None) -> str:
    today = today or datetime.now()
    template = PROMPT_PATH.read_text()
    return (
        template
        .replace("{today}", today.strftime("%Y-%m-%d"))
        .replace("{weekday}", today.strftime("%A"))
        .replace("{user_name}", settings.watson_user_name)
        .replace("{entries}", _render_entries(conn))
        .replace("{reminders}", _render_reminders(conn))
        .replace("{gitlab_mrs}", _render_gitlab_mrs(conn))
        .replace("{clickup_tasks}", _render_clickup_tasks(conn))
        .replace("{recent_activity}", _render_recent_activity(conn))
        .replace("{calendar}", _render_calendar(conn))
        .replace("{question}", question)
    )


def answer_question(conn, question: str) -> dict:
    """Return {answer, entry_ids}. entry_ids are the #N ids cited in the answer."""
    prompt = build_prompt(conn, question)
    answer = call_llm(prompt).strip()
    cited = []
    for m in re.findall(r"#(\d+)", answer):
        i = int(m)
        if i not in cited:
            cited.append(i)
    return {"answer": answer, "entry_ids": cited}
