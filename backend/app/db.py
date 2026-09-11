import sqlite3
from datetime import datetime
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  raw_text TEXT NOT NULL,
  created_at TIMESTAMP,
  classified_at TIMESTAMP NULL,
  classification_json TEXT NULL
);

CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_id INTEGER NULL REFERENCES captures(id),
  parent_entry_id INTEGER NULL REFERENCES entries(id),  -- follow-up threading
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  people TEXT,
  tags TEXT,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NULL REFERENCES entries(id),
  capture_id INTEGER NULL REFERENCES captures(id),
  text TEXT NOT NULL,
  due_at TIMESTAMP NOT NULL,
  status TEXT DEFAULT 'pending',
  snoozed_until TIMESTAMP NULL,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_id INTEGER REFERENCES captures(id),
  kind TEXT NOT NULL,
  target_id TEXT NULL,
  payload_json TEXT NOT NULL,
  match_confidence REAL NULL,
  status TEXT DEFAULT 'pending',
  created_at TIMESTAMP,
  resolved_at TIMESTAMP NULL,
  error TEXT NULL
);

CREATE TABLE IF NOT EXISTS clickup_tasks_cache (
  task_id TEXT PRIMARY KEY,
  name TEXT, status TEXT, status_type TEXT,
  list_name TEXT, url TEXT,
  assignees TEXT, due_date TIMESTAMP NULL,
  date_closed TIMESTAMP NULL,
  synced_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gitlab_mrs_cache (
  mr_id TEXT PRIMARY KEY,
  project TEXT, title TEXT, state TEXT, url TEXT,
  role TEXT,
  roles TEXT,
  author TEXT,
  author_username TEXT,
  assignee_usernames TEXT,
  reviewer_usernames TEXT,
  source_branch TEXT,
  description TEXT,
  updated_at TIMESTAMP,
  synced_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gcal_events_cache (
  event_id TEXT PRIMARY KEY,
  calendar_id TEXT NOT NULL DEFAULT 'primary',
  title TEXT NOT NULL DEFAULT '',
  description TEXT,
  location TEXT,
  start_at TIMESTAMP NOT NULL,
  end_at TIMESTAMP,
  all_day INTEGER NOT NULL DEFAULT 0,
  organizer TEXT,
  attendees TEXT,          -- JSON array of email strings
  my_response TEXT,        -- accepted | declined | tentative | needsAction
  html_link TEXT,
  synced_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS flock_mentions_cache (
  jid TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  is_group INTEGER NOT NULL DEFAULT 0,
  has_mention INTEGER NOT NULL DEFAULT 0,
  unread_count INTEGER NOT NULL DEFAULT 0,
  last_message_time TIMESTAMP,
  is_muted INTEGER NOT NULL DEFAULT 0,
  notify_on TEXT,
  bucket TEXT,
  synced_at TIMESTAMP
);

-- Registry of Flock Outgoing Webhooks. One row per channel you configured a
-- webhook for at dev.flock.com/webhooks/outgoing. The token identifies which
-- channel the incoming POST belongs to (Flock appends ?token=<token>).
-- channel_jid is auto-populated from the first webhook payload's `to` field.
CREATE TABLE IF NOT EXISTS flock_webhook_channels (
  token TEXT PRIMARY KEY,
  channel_name TEXT NOT NULL,             -- friendly name entered on `add`
  channel_jid TEXT,                       -- filled from first webhook payload
  created_at TIMESTAMP,
  last_seen_at TIMESTAMP                  -- updated on every accepted receipt
);

-- JID → display-name lookup harvested from the sidebar Playwright walk. Used
-- at render time to swap raw sender JIDs (`u:2thc2ap...`) for real names in
-- webhook-sourced mentions. Populated on every flock sync from the buddy
-- rows in the sidebar; upsert semantics keep names fresh as people rename.
CREATE TABLE IF NOT EXISTS flock_contacts (
  jid TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  updated_at TIMESTAMP
);

-- Filtered @-mention messages received via Outgoing Webhooks. Only messages
-- containing the user's Flock handle (or @all/@online) survive the filter.
CREATE TABLE IF NOT EXISTS flock_webhook_mentions (
  id TEXT PRIMARY KEY,                    -- Flock's message id, natural dedup
  token TEXT NOT NULL,                    -- token that received it (→ channel)
  channel_jid TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  sender_jid TEXT NOT NULL,               -- base jid (resource stripped)
  text TEXT NOT NULL,
  received_at TIMESTAMP NOT NULL
);

-- Tasks Watson created in ClickUp, so they can be kept in sync with the work
-- they represent (a linked ClickUp task closing, an MR merging, etc.)
CREATE TABLE IF NOT EXISTS managed_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clickup_task_id TEXT NOT NULL,          -- the task Watson created (mine)
  capture_id INTEGER NULL REFERENCES captures(id),
  related_clickup_task_id TEXT NULL,      -- existing task it links to / tracks
  related_mr_id TEXT NULL,                -- MR it tracks (Phase 2)
  category TEXT NULL,                     -- Discussion / Review / Work / ...
  status TEXT DEFAULT 'open',             -- Watson's view: 'open' | 'closed'
  created_at TIMESTAMP
);

-- Audit trail of things Watson (or the user via Watson) did: sync ticks,
-- drafts created, approvals/rejections, detected state changes. Populates
-- the Log tab alongside classified entries.
CREATE TABLE IF NOT EXISTS system_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,        -- sync | proposal | approval | rejection | state_change | backfill
  subject TEXT NOT NULL,     -- short human-readable line
  details_json TEXT NULL,    -- optional context (counts, ids, urls)
  related_action_id INTEGER NULL,
  related_mr_id TEXT NULL,
  related_task_id TEXT NULL,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name TEXT NOT NULL,
  is_self INTEGER NOT NULL DEFAULT 0,
  is_tracked INTEGER NOT NULL DEFAULT 0,
  lane_position INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_people_single_self
  ON people(is_self) WHERE is_self = 1;

CREATE TABLE IF NOT EXISTS person_identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  display_value TEXT,
  UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS work_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  title_is_manual INTEGER NOT NULL DEFAULT 1,
  description TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'next'
    CHECK(state IN ('today','next','waiting','done')),
  owner_person_id INTEGER NULL REFERENCES people(id),
  owner_display TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 1000,
  priority_position INTEGER NOT NULL DEFAULT 1000,
  origin TEXT NOT NULL DEFAULT 'manual',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  completed_at TIMESTAMP NULL
);

CREATE TABLE IF NOT EXISTS review_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  author_username TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  provenance TEXT NOT NULL CHECK(provenance IN ('exact','ai','user','singleton')),
  confidence REAL NOT NULL,
  fingerprint TEXT NOT NULL UNIQUE,
  work_item_id INTEGER NULL REFERENCES work_items(id),
  clickup_task_id TEXT NULL,
  related_clickup_task_id TEXT NULL,
  creation_state TEXT NOT NULL DEFAULT 'pending'
    CHECK(creation_state IN ('pending','creating','created','disabled','deferred','failed','uncertain')),
  last_error TEXT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS review_group_mrs (
  group_id INTEGER NOT NULL REFERENCES review_groups(id) ON DELETE CASCADE,
  mr_id TEXT NOT NULL UNIQUE,
  created_at TIMESTAMP NOT NULL,
  PRIMARY KEY(group_id, mr_id)
);

CREATE TABLE IF NOT EXISTS review_group_decisions (
  fingerprint TEXT PRIMARY KEY,
  decision TEXT NOT NULL CHECK(decision='keep_separate'),
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS work_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  source_type TEXT NOT NULL,
  external_id TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL,
  UNIQUE(source_type, external_id)
);

CREATE TABLE IF NOT EXISTS work_activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id INTEGER NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  activity_type TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  capture_id INTEGER NULL REFERENCES captures(id),
  reminder_id INTEGER NULL REFERENCES reminders(id),
  pending_action_id INTEGER NULL REFERENCES pending_actions(id),
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS work_inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_id INTEGER NOT NULL UNIQUE REFERENCES captures(id) ON DELETE CASCADE,
  suggested_work_item_id INTEGER NULL REFERENCES work_items(id),
  confidence REAL NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','linked','dismissed')),
  created_at TIMESTAMP NOT NULL,
  resolved_at TIMESTAMP NULL
);

-- Scratch notes taken from the My Work and Team boards. Deliberately outside
-- the capture pipeline: these are quick jottings, never classified by the LLM.
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  body TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_capture ON entries(capture_id);
CREATE INDEX IF NOT EXISTS idx_managed_status ON managed_tasks(status);
CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_at);
CREATE INDEX IF NOT EXISTS idx_actions_status ON pending_actions(status);
CREATE INDEX IF NOT EXISTS idx_events_created ON system_events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_kind ON system_events(kind);
CREATE INDEX IF NOT EXISTS idx_gcal_start ON gcal_events_cache(start_at);
CREATE INDEX IF NOT EXISTS idx_flock_last_msg ON flock_mentions_cache(last_message_time);
CREATE INDEX IF NOT EXISTS idx_flock_webhook_received ON flock_webhook_mentions(received_at);
CREATE INDEX IF NOT EXISTS idx_flock_webhook_token ON flock_webhook_mentions(token);
CREATE INDEX IF NOT EXISTS idx_review_groups_creation_state ON review_groups(creation_state);
CREATE INDEX IF NOT EXISTS idx_review_groups_author_username ON review_groups(author_username);
CREATE INDEX IF NOT EXISTS idx_review_group_mrs_group_id ON review_group_mrs(group_id);

-- Small key/value store for cross-integration user preferences learned at
-- runtime (e.g. google_user_email, so every integration that surfaces a
-- Google URL can force the correct account via ?authuser=).
CREATE TABLE IF NOT EXISTS user_meta (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TIMESTAMP
);

-- User-configurable, non-secret settings. Values are JSON so runtime values
-- retain their native Python types instead of becoming string-only metadata.
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT,
  updated_at TIMESTAMP
);
"""


def _migrate(conn):
    """Idempotent column adds for databases created before a feature landed.
    Runs AFTER the SCHEMA script, so any index touching a migrated column lives
    here (not in SCHEMA) to avoid referencing a column the old table lacks."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
    if "parent_entry_id" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN parent_entry_id INTEGER NULL")
    if "work_item_id" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN work_item_id INTEGER NULL REFERENCES work_items(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_entry_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_work_item ON entries(work_item_id)")

    capture_cols = {r["name"] for r in conn.execute("PRAGMA table_info(captures)")}
    if "work_item_id" not in capture_cols:
        conn.execute("ALTER TABLE captures ADD COLUMN work_item_id INTEGER NULL REFERENCES work_items(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_captures_work_item ON captures(work_item_id)")

    reminder_cols = {r["name"] for r in conn.execute("PRAGMA table_info(reminders)")}
    if "work_item_id" not in reminder_cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN work_item_id INTEGER NULL REFERENCES work_items(id)")
    if "capture_id" not in reminder_cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN capture_id INTEGER NULL REFERENCES captures(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_work_item ON reminders(work_item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_capture ON reminders(capture_id)")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_person_identities_person ON person_identities(person_id)")
    work_item_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(work_items)")
    }
    if "priority_position" not in work_item_cols:
        conn.execute(
            "ALTER TABLE work_items ADD COLUMN priority_position INTEGER NOT NULL DEFAULT 1000"
        )
        rows = conn.execute(
            "SELECT id, owner_person_id, owner_display FROM work_items "
            "ORDER BY owner_person_id, owner_display, "
            "CASE state WHEN 'today' THEN 1 WHEN 'next' THEN 2 "
            "WHEN 'waiting' THEN 3 WHEN 'done' THEN 4 END, position, created_at, id"
        ).fetchall()
        owner_positions = {}
        for row in rows:
            owner_key = (row["owner_person_id"], row["owner_display"] or "")
            owner_positions[owner_key] = owner_positions.get(owner_key, 0) + 1000
            conn.execute(
                "UPDATE work_items SET priority_position=? WHERE id=?",
                (owner_positions[owner_key], row["id"]),
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_lane ON work_items(owner_person_id, owner_display, state, position)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_priority ON work_items(owner_person_id, owner_display, priority_position)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_links_item ON work_links(work_item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_activity_item_created ON work_activity(work_item_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_inbox_status_created ON work_inbox(status, created_at)")

    # These statements also migrate databases whose original SCHEMA predates
    # canonical review groups. Tables must exist before their indexes.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_groups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          author_username TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          provenance TEXT NOT NULL CHECK(provenance IN ('exact','ai','user','singleton')),
          confidence REAL NOT NULL,
          fingerprint TEXT NOT NULL UNIQUE,
          work_item_id INTEGER NULL REFERENCES work_items(id),
          clickup_task_id TEXT NULL,
          related_clickup_task_id TEXT NULL,
          creation_state TEXT NOT NULL DEFAULT 'pending'
            CHECK(creation_state IN ('pending','creating','created','disabled','deferred','failed','uncertain')),
          last_error TEXT NULL,
          created_at TIMESTAMP NOT NULL,
          updated_at TIMESTAMP NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_group_mrs (
          group_id INTEGER NOT NULL REFERENCES review_groups(id) ON DELETE CASCADE,
          mr_id TEXT NOT NULL UNIQUE,
          created_at TIMESTAMP NOT NULL,
          PRIMARY KEY(group_id, mr_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_group_decisions (
          fingerprint TEXT PRIMARY KEY,
          decision TEXT NOT NULL CHECK(decision='keep_separate'),
          created_at TIMESTAMP NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_groups_creation_state "
        "ON review_groups(creation_state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_groups_author_username "
        "ON review_groups(author_username)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_group_mrs_group_id "
        "ON review_group_mrs(group_id)"
    )

    mrcols = {r["name"] for r in conn.execute("PRAGMA table_info(gitlab_mrs_cache)")}
    if "author" not in mrcols:
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN author TEXT")
    if "source_branch" not in mrcols:
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN source_branch TEXT")
    if "description" not in mrcols:
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN description TEXT")
    if "author_username" not in mrcols:
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN author_username TEXT")
    if "assignee_usernames" not in mrcols:
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN assignee_usernames TEXT")
    if "reviewer_usernames" not in mrcols:
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN reviewer_usernames TEXT")
    if "roles" not in mrcols:
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN roles TEXT")

    cucols = {r["name"] for r in conn.execute("PRAGMA table_info(clickup_tasks_cache)")}
    if "status_type" not in cucols:
        conn.execute("ALTER TABLE clickup_tasks_cache ADD COLUMN status_type TEXT")
    if "date_closed" not in cucols:
        conn.execute("ALTER TABLE clickup_tasks_cache ADD COLUMN date_closed TIMESTAMP NULL")

    mtcols = {r["name"] for r in conn.execute("PRAGMA table_info(managed_tasks)")}
    if "additional_mr_ids" not in mtcols:
        # JSON array of MR ids linked to this task by user-approved title match,
        # beyond related_mr_id (one explicit) and branch-tag matching (any).
        conn.execute("ALTER TABLE managed_tasks ADD COLUMN additional_mr_ids TEXT")

    # Contacts harvested from Google Calendar attendee lists — feeds the
    # classifier so it can include real email addresses in drafted gcal
    # events (otherwise Watson has no way to know "Morgan" == morgan.lee@…).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gcal_contacts (
          email TEXT PRIMARY KEY,
          name TEXT,
          last_seen_at TIMESTAMP
        )
    """)

    grcols = {r["name"] for r in conn.execute("PRAGMA table_info(gitlab_mrs_cache)")}
    if "labels" not in grcols:
        # JSON array of label strings from GitLab. Feeds the MR-stage badge
        # (e.g. "QA in progress" when labels contain "Ready for QA").
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN labels TEXT")
    if "stages" not in grcols:
        # JSON array of lifecycle tags: any subset of
        #   {merged, qa, reviewed_by_me, review_pending, closed}
        # An MR can be both "reviewed_by_me" AND "qa" concurrently
        # (I signed off, then it moved to QA). Computed during gitlab sync
        # from state + labels + comment scan so the Today card can render
        # badges without per-render API calls. Read via _json_list().
        conn.execute("ALTER TABLE gitlab_mrs_cache ADD COLUMN stages TEXT")

    fmcols = {r["name"] for r in conn.execute("PRAGMA table_info(flock_mentions_cache)")}
    if "mentions_json" not in fmcols:
        # JSON array of {id, sender, sender_name, text, chip} — deep-read
        # extraction of @-mention messages (surfaces last-24h mentions of
        # the user even after they've been read, which the sidebar-level
        # hasMention rollup would have cleared).
        conn.execute("ALTER TABLE flock_mentions_cache ADD COLUMN mentions_json TEXT")

    gccols = {r["name"] for r in conn.execute("PRAGMA table_info(gcal_events_cache)")}
    if "meet_link" not in gccols:
        # Direct video-conference URL (Google Meet from hangoutLink or
        # conferenceData, else scraped from description for Zoom/Teams/etc).
        # Lets the Today UI offer a one-click Join without an intermediate
        # trip through the Google Calendar event page.
        conn.execute("ALTER TABLE gcal_events_cache ADD COLUMN meet_link TEXT")


def db_path():
    return settings.data_dir / "watson.db"


def connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # wait up to 5s for a write lock instead of erroring immediately — the
    # scheduler (sync/digest/export) and a capture request can collide.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def backup_before_work_migration(conn: sqlite3.Connection) -> Path | None:
    """Snapshot a legacy database once, before adding the work-domain tables."""
    has_captures = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='captures'"
    ).fetchone()
    has_work_items = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'"
    ).fetchone()
    if not has_captures or has_work_items:
        return None

    backup_root = settings.backup_dir / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup_path = backup_root / f"watson-pre-work-os-{timestamp}.db"
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return backup_path


def init_db():
    conn = connect()
    try:
        backup_before_work_migration(conn)
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def get_db():
    """FastAPI dependency: one connection per request, committed on success."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
