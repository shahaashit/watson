#!/usr/bin/env bash
# Prove the additive Work OS migration against a scratch legacy database.
# This script never reads, writes, or deletes the user's live Watson data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${WATSON_PYTHON:-$ROOT/backend/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || { echo "error: Python environment missing; run ./scripts/install.sh first." >&2; exit 1; }

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/watson-migration.XXXXXXXX")"
TMP_ROOT="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
cleanup() {
  # Never permit a broad cleanup target, even if the shell state is damaged.
  if [[ -n "${SCRATCH:-}" && -d "$SCRATCH" && "$SCRATCH" == "$TMP_ROOT"/watson-migration.* && "$SCRATCH" != "$TMP_ROOT" ]]; then
    rm -rf "$SCRATCH"
  fi
}
trap cleanup EXIT

export WATSON_DATA_DIR="$SCRATCH/data"
export WATSON_BACKUP_DIR="$SCRATCH/backup"
export TESTING=1
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"

"$PYTHON_BIN" - <<'PY'
import os
import sqlite3
from pathlib import Path

scratch = Path(os.environ["WATSON_DATA_DIR"])
scratch.mkdir(parents=True, exist_ok=True)
path = scratch / "watson.db"
conn = sqlite3.connect(path)
conn.executescript("""
CREATE TABLE captures (id INTEGER PRIMARY KEY AUTOINCREMENT, raw_text TEXT NOT NULL, created_at TIMESTAMP, classified_at TIMESTAMP NULL, classification_json TEXT NULL);
CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, capture_id INTEGER, type TEXT NOT NULL, title TEXT NOT NULL, body TEXT, people TEXT, tags TEXT, created_at TIMESTAMP);
CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER, text TEXT NOT NULL, due_at TIMESTAMP NOT NULL, status TEXT DEFAULT 'pending', snoozed_until TIMESTAMP NULL, created_at TIMESTAMP);
CREATE TABLE pending_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, capture_id INTEGER, kind TEXT NOT NULL, target_id TEXT NULL, payload_json TEXT NOT NULL, match_confidence REAL NULL, status TEXT DEFAULT 'pending', created_at TIMESTAMP, resolved_at TIMESTAMP NULL, error TEXT NULL);
CREATE TABLE clickup_tasks_cache (task_id TEXT PRIMARY KEY, name TEXT, status TEXT, list_name TEXT, url TEXT, assignees TEXT, due_date TIMESTAMP NULL, synced_at TIMESTAMP);
CREATE TABLE gitlab_mrs_cache (mr_id TEXT PRIMARY KEY, project TEXT, title TEXT, state TEXT, url TEXT, role TEXT, updated_at TIMESTAMP, synced_at TIMESTAMP);
CREATE TABLE managed_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, clickup_task_id TEXT NOT NULL, capture_id INTEGER, related_clickup_task_id TEXT, related_mr_id TEXT, category TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP);
""")
conn.execute("INSERT INTO captures VALUES (41, 'legacy capture stays intact', '2026-08-01T09:00:00', NULL, NULL)")
conn.execute("INSERT INTO entries VALUES (42, 41, 'discussion', 'Legacy entry', 'context remains', '[]', '[\"legacy\"]', '2026-08-01T09:01:00')")
conn.execute("INSERT INTO reminders VALUES (43, 42, 'Legacy follow-up', '2026-08-02T09:00:00', 'pending', NULL, '2026-08-01T09:02:00')")
conn.execute("INSERT INTO pending_actions VALUES (44, 41, 'clickup_comment', 'legacy-task', '{\"draft\":\"keep approval\"}', 0.9, 'pending', '2026-08-01T09:03:00', NULL, NULL)")
conn.execute("INSERT INTO clickup_tasks_cache VALUES ('legacy-task', 'Legacy task', 'open', 'Legacy', 'https://clickup.example/tasks/legacy-task', '[]', NULL, '2026-08-01T09:04:00')")
conn.execute("INSERT INTO gitlab_mrs_cache VALUES ('1!2', 'example/project', 'Legacy MR', 'opened', 'https://gitlab.example/mr/2', 'author', '2026-08-01T09:05:00', '2026-08-01T09:05:00')")
conn.execute("INSERT INTO managed_tasks VALUES (45, 'watson-task', 41, 'legacy-task', '1!2', 'Discussion', 'open', '2026-08-01T09:06:00')")
conn.commit(); conn.close()

from app import db
db.init_db()
db.init_db()  # Must be safe after the first migration.

conn = db.connect()
try:
    assert conn.execute("SELECT raw_text FROM captures WHERE id=41").fetchone()[0] == "legacy capture stays intact"
    entry = conn.execute("SELECT title, body FROM entries WHERE id=42").fetchone()
    assert (entry["title"], entry["body"]) == ("Legacy entry", "context remains")
    assert conn.execute("SELECT text FROM reminders WHERE id=43").fetchone()[0] == "Legacy follow-up"
    assert conn.execute("SELECT payload_json FROM pending_actions WHERE id=44").fetchone()[0] == '{"draft":"keep approval"}'
    assert conn.execute("SELECT name FROM clickup_tasks_cache WHERE task_id='legacy-task'").fetchone()[0] == "Legacy task"
    assert conn.execute("SELECT COUNT(*) FROM gitlab_mrs_cache").fetchone()[0] == 1
    mr = conn.execute(
        "SELECT project, title, state, url FROM gitlab_mrs_cache WHERE mr_id='1!2'"
    ).fetchone()
    assert (mr["project"], mr["title"], mr["state"], mr["url"]) == (
        "example/project", "Legacy MR", "opened", "https://gitlab.example/mr/2"
    )
    assert conn.execute("SELECT clickup_task_id FROM managed_tasks WHERE id=45").fetchone()[0] == "watson-task"
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    assert "work_item_id" in {row[1] for row in conn.execute("PRAGMA table_info(captures)")}
    assert "work_item_id" in {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    assert "additional_mr_ids" in {row[1] for row in conn.execute("PRAGMA table_info(managed_tasks)")}
finally:
    conn.close()

backups = list((Path(os.environ["WATSON_BACKUP_DIR"]) / "backups").glob("watson-pre-work-os-*.db"))
assert len(backups) == 1, backups
print("migration verification passed: preserved legacy ids 41-45; additive migration is idempotent")
PY
