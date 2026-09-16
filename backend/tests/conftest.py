import os
import tempfile
from pathlib import Path

# Must be set before the app (and its module-level Settings) is imported.
TEST_SCRATCH_ROOT = Path(tempfile.mkdtemp(prefix="watson-test-"))
os.environ["WATSON_DATA_DIR"] = str(TEST_SCRATCH_ROOT / "data")
os.environ["WATSON_BACKUP_DIR"] = str(TEST_SCRATCH_ROOT / "backup")
os.environ["TESTING"] = "1"
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["CLICKUP_API_TOKEN"] = ""
os.environ["GITLAB_TOKEN"] = ""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import secret_store


@pytest.fixture(autouse=True)
def isolated_keychain(monkeypatch, request):
    """Keep all non-secret-store tests away from the macOS Keychain."""
    if request.node.path.name == "test_secret_store.py":
        yield None
        return

    values = {}
    original_run = secret_store.subprocess.run

    def guarded_run(command, *args, **kwargs):
        if command and command[0] == secret_store.SECURITY:
            pytest.fail("test crossed the real macOS Keychain boundary")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(secret_store.subprocess, "run", guarded_run)
    monkeypatch.setattr(
        secret_store, "get_secret", lambda name: values.get(name, "")
    )
    monkeypatch.setattr(secret_store, "has_secret", lambda name: name in values)
    monkeypatch.setattr(
        secret_store,
        "effective_secret",
        lambda name, fallback="": values[name] if name in values else fallback,
    )
    monkeypatch.setattr(
        secret_store, "set_secret", lambda name, value: values.__setitem__(name, value)
    )
    monkeypatch.setattr(
        secret_store,
        "delete_secret",
        lambda name: values.pop(name, None) is not None,
    )
    yield values


@pytest.fixture
def client():
    with TestClient(app) as c:  # context manager runs lifespan -> init_db
        yield c


@pytest.fixture
def conn():
    db.init_db()
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def clean_db():
    db.init_db()
    connection = db.connect()
    # order matters: child/referencing tables before the tables they reference
    for table in ("meet_link_requests", "work_link_removals", "work_removals", "review_group_mrs", "review_group_decisions", "review_groups",
                  "work_activity", "work_inbox", "managed_tasks", "pending_actions",
                  "reminders", "entries", "captures", "work_links", "work_items",
                  "person_identities", "people", "clickup_tasks_cache", "gitlab_mrs_cache",
                  "gcal_events_cache", "flock_mentions_cache",
                  "flock_webhook_mentions", "flock_webhook_channels",
                  "flock_contacts",
                  "notes", "system_events", "user_meta", "app_settings"):
        connection.execute(f"DELETE FROM {table}")
    connection.commit()
    connection.close()
    yield


def seed_task(connection, task_id="abc123", name="Fix iframe z-index on Playback player"):
    connection.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, status, list_name, url,"
        " assignees, due_date, synced_at) VALUES (?, ?, 'in progress', 'Playback', '',"
        " '[]', NULL, '2026-06-12T09:00:00')",
        (task_id, name),
    )
    connection.commit()
