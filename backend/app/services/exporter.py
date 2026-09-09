"""Daily human-readable markdown export + nightly db backup into the
cloud-synced backup dir (WATSON_BACKUP_DIR)."""
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from ..config import settings
from ..models import _json_list
from . import app_settings

log = logging.getLogger("watson.exporter")


def _backup_dir(conn) -> Path:
    """Resolve the UI-managed destination for every individual export."""
    configured = app_settings.get(conn, "data.backup_dir")
    if configured:
        return Path(configured).expanduser().resolve()
    return settings.backup_dir.resolve()


def backup_db(conn, day: date = None) -> Path:
    """Write a consistent copy of the live db into the backup dir using
    SQLite's online-backup API (safe even while the db is being written)."""
    day = day or date.today()
    backup_root = _backup_dir(conn) / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    out_path = backup_root / f"watson-{day.isoformat()}.db"
    dest = sqlite3.connect(out_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    log.info("backed up db to %s", out_path)
    return out_path


def export_day(conn, day: date = None) -> Path:
    day = day or date.today()
    prefix = day.isoformat()
    journal_dir = _backup_dir(conn) / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    out_path = journal_dir / f"{prefix}.md"

    like = f"{prefix}%"
    captures = conn.execute(
        "SELECT * FROM captures WHERE created_at LIKE ? ORDER BY id", (like,)
    ).fetchall()
    entries = conn.execute(
        "SELECT * FROM entries WHERE created_at LIKE ? ORDER BY id", (like,)
    ).fetchall()
    reminders_done = conn.execute(
        "SELECT * FROM reminders WHERE status = 'done' AND due_at LIKE ? ORDER BY due_at", (like,)
    ).fetchall()
    actions_executed = conn.execute(
        "SELECT * FROM pending_actions WHERE status = 'executed' AND resolved_at LIKE ?"
        " ORDER BY resolved_at",
        (like,),
    ).fetchall()

    lines = [f"# Watson journal — {day.strftime('%A, %d %B %Y')}", ""]

    lines.append(f"## Captures ({len(captures)})")
    for c in captures:
        lines.append(f"- `{c['created_at'][11:16]}` {c['raw_text']}")
    lines.append("")

    lines.append(f"## Entries ({len(entries)})")
    for e in entries:
        people = ", ".join(_json_list(e["people"]))
        tags = " ".join(f"#{t}" for t in _json_list(e["tags"]))
        meta = " — ".join(x for x in (people, tags) if x)
        lines.append(f"### [{e['type']}] {e['title']}")
        if meta:
            lines.append(f"*{meta}*")
        if e["body"]:
            lines.append(e["body"])
        lines.append("")

    lines.append(f"## Reminders completed ({len(reminders_done)})")
    for r in reminders_done:
        lines.append(f"- [x] {r['text']} (was due {r['due_at'][:16]})")
    lines.append("")

    lines.append(f"## ClickUp actions executed ({len(actions_executed)})")
    for a in actions_executed:
        lines.append(f"- {a['kind']} → task {a['target_id'] or '(new)'} at {a['resolved_at'][11:16]}")
    lines.append("")

    out_path.write_text("\n".join(lines))
    log.info("exported journal to %s", out_path)
    return out_path
