import importlib
import json
import sqlite3
import sys
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.config import settings
from app.services import secret_store


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch):
    """Keep every Settings test away from the user's real macOS Keychain."""
    values = {}

    def set_secret(name, value):
        values[name] = value

    def delete_secret(name):
        return values.pop(name, None) is not None

    monkeypatch.setattr(secret_store, "set_secret", set_secret)
    monkeypatch.setattr(secret_store, "delete_secret", delete_secret)
    monkeypatch.setattr(secret_store, "get_secret", lambda name: values.get(name, ""))
    monkeypatch.setattr(secret_store, "has_secret", lambda name: name in values)
    monkeypatch.setattr(
        secret_store,
        "effective_secret",
        lambda name, fallback="": values[name] if name in values else fallback,
    )
    return values


def test_gitlab_settings_never_return_or_persist_token(client, conn, fake_keychain):
    response = client.put(
        "/api/settings/integrations/gitlab",
        json={
            "base_url": "https://gitlab.example.com",
            "token": "top-secret",
            "username": "alex.g",
        },
    )

    assert response.status_code == 200
    assert "top-secret" not in response.text
    assert response.json()["integration"] == {
        "source": "gitlab",
        "base_url": "https://gitlab.example.com",
        "username": "alex.g",
        "credential_present": True,
        "oauth_available": False,
        "oauth_connected": False,
        "oauth_http": False,
        "reauth_required": False,
        "configured": True,
        "health": None,
        "reconnect_required": False,
    }
    assert fake_keychain == {"gitlab.token": "top-secret"}
    assert "top-secret" not in json.dumps([
        tuple(row)
        for row in conn.execute("SELECT key, value_json FROM app_settings").fetchall()
    ])


def test_gitlab_repository_selection_is_resolved_and_persisted_locally(
    client, conn, monkeypatch
):
    from app.services import app_settings, gitlab_client

    client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://gitlab.example.com", "token": "private"},
    )
    available = [
        {"id": 41, "name": "frontend-service", "path": "cm/frontend-service"},
        {"id": 52, "name": "backend-service", "path": "cm/go/backend-service"},
        {"id": 63, "name": "other", "path": "other/repo"},
    ]
    monkeypatch.setattr(
        gitlab_client,
        "list_accessible_projects",
        lambda **kwargs: available,
    )
    by_id = {project["id"]: project for project in available}
    monkeypatch.setattr(
        gitlab_client,
        "get_accessible_project",
        lambda project_id: by_id[project_id],
    )

    listing = client.get("/api/settings/integrations/gitlab/projects?q=serving")
    assert listing.status_code == 200
    assert listing.json() == {"projects": available, "selected": []}

    saved = client.put(
        "/api/settings/integrations/gitlab/projects",
        json={"project_ids": [52, 41]},
    )
    assert saved.status_code == 200
    assert saved.json() == {
        "selected": [available[0], available[1]],
    }
    assert app_settings.get(conn, "integration.gitlab.projects") == [
        available[0], available[1]
    ]


def test_gitlab_repository_selection_rejects_inaccessible_ids(client, monkeypatch):
    from app.services import gitlab_client

    client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://gitlab.example.com", "token": "private"},
    )
    monkeypatch.setattr(
        gitlab_client,
        "get_accessible_project",
        lambda project_id: (_ for _ in ()).throw(LookupError(project_id)),
    )

    response = client.put(
        "/api/settings/integrations/gitlab/projects",
        json={"project_ids": [999]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "One or more GitLab projects are not accessible."


def test_gitlab_repository_picker_loads_known_cache_without_broad_project_scan(
    client, conn, monkeypatch
):
    from app.services import gitlab_client

    client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://gitlab.example.com", "token": "private"},
    )
    conn.executemany(
        "INSERT INTO gitlab_mrs_cache (mr_id,project,title,state,synced_at) "
        "VALUES (?, ?, 'MR', 'opened', 'now')",
        [("41!1", "cm/frontend-service"), ("52!2", "cm/go/backend-service")],
    )
    conn.commit()
    monkeypatch.setattr(
        gitlab_client,
        "list_accessible_projects",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("broad GitLab project scan must not run")
        ),
    )

    response = client.get("/api/settings/integrations/gitlab/projects")

    assert response.status_code == 200
    assert response.json() == {
        "projects": [
            {"id": 41, "name": "frontend-service", "path": "cm/frontend-service"},
            {"id": 52, "name": "backend-service", "path": "cm/go/backend-service"},
        ],
        "selected": [],
    }


def test_anthropic_key_is_write_only_and_nonsecrets_persist(client, fake_keychain):
    response = client.put(
        "/api/settings/integrations/anthropic",
        json={
            "api_key": "fake-anthropic-api-key",
            "base_url": "https://gateway.example.com",
            "model": "claude-sonnet-4-6",
        },
    )

    assert response.status_code == 200
    assert "fake-anthropic-api-key" not in response.text
    assert fake_keychain["anthropic.api_key"] == "fake-anthropic-api-key"
    saved = client.get("/api/settings").json()["integrations"]["anthropic"]
    assert saved["base_url"] == "https://gateway.example.com"
    assert saved["model"] == "claude-sonnet-4-6"
    assert saved["credential_present"] is True


def test_google_client_json_is_write_only_and_flock_is_nonsecret(client, fake_keychain):
    client_json = '{"installed":{"client_secret":"private-google-secret"}}'
    google = client.put(
        "/api/settings/integrations/google-calendar",
        json={"client_config_json": client_json},
    )
    flock = client.put(
        "/api/settings/integrations/flock",
        json={"profile_dir": "/tmp/watson-flock", "user_handle": "Alex S"},
    )

    assert google.status_code == 200
    assert "private-google-secret" not in google.text
    assert fake_keychain["google.client_config"] == client_json
    assert flock.status_code == 200
    assert flock.json()["integration"]["profile_dir"] == "/tmp/watson-flock"
    assert flock.json()["integration"]["user_handle"] == "Alex S"


def test_google_connect_starts_without_blocking_request(client, monkeypatch):
    oauth_sessions = SimpleNamespace(start_google_calendar=lambda: "session-1")
    monkeypatch.setitem(
        sys.modules, "app.services.oauth_sessions", oauth_sessions
    )

    response = client.post(
        "/api/settings/integrations/google-calendar/connect"
    )

    assert response.status_code == 202
    assert response.json() == {"session_id": "session-1", "status": "pending"}


def test_google_connect_status_is_pollable(client, monkeypatch):
    oauth_sessions = SimpleNamespace(
        status=lambda session_id: {
            "session_id": session_id,
            "status": "connected",
        }
    )
    monkeypatch.setitem(
        sys.modules, "app.services.oauth_sessions", oauth_sessions
    )

    response = client.get(
        "/api/settings/integrations/google-calendar/connect/session-1"
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "status": "connected",
    }


def test_google_connect_start_failure_is_redacted(client, monkeypatch, caplog):
    from app.services import oauth_sessions

    secret = "thread-start-secret"
    monkeypatch.setattr(
        oauth_sessions,
        "start_google_calendar",
        lambda: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    try:
        response = client.post(
            "/api/settings/integrations/google-calendar/connect"
        )
    except RuntimeError:
        pytest.fail("OAuth worker start failure escaped the Settings boundary")

    assert response.status_code == 503
    assert response.json() == {"detail": "Unable to start OAuth session."}
    assert secret not in response.text
    assert secret not in caplog.text


def test_google_oauth_session_starts_exactly_one_daemon_worker(monkeypatch):
    from app.services import oauth_sessions

    oauth_sessions = importlib.reload(oauth_sessions)
    created = []

    class Thread:
        def __init__(self, *, target, args, name, daemon):
            created.append(
                {
                    "target": target,
                    "args": args,
                    "name": name,
                    "daemon": daemon,
                }
            )

        def start(self):
            return None

    monkeypatch.setattr(oauth_sessions.threading, "Thread", Thread)

    session_id = oauth_sessions.start_google_calendar()

    assert len(created) == 1
    assert created[0]["args"] == (session_id,)
    assert created[0]["daemon"] is True
    assert oauth_sessions.status(session_id) == {
        "session_id": session_id,
        "status": "pending",
    }


def test_google_oauth_thread_constructor_failures_release_capacity_and_redact(
    monkeypatch, caplog
):
    from app.services import oauth_sessions

    oauth_sessions = importlib.reload(oauth_sessions)
    secret = "credential-material-in-thread-constructor"
    failed_session_ids = [f"failed-session-{index}" for index in range(20)]
    generated_ids = iter([*failed_session_ids, "successful-session"])
    monkeypatch.setattr(
        oauth_sessions.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(generated_ids)),
    )
    constructor_calls = 0

    class Thread:
        def __init__(self, **_kwargs):
            nonlocal constructor_calls
            constructor_calls += 1
            if constructor_calls <= 20:
                raise RuntimeError(secret)

        def start(self):
            return None

    monkeypatch.setattr(oauth_sessions.threading, "Thread", Thread)

    failures = []
    for _ in failed_session_ids:
        with pytest.raises(RuntimeError) as error:
            oauth_sessions.start_google_calendar()
        failures.append(error.value)

    successful_session = oauth_sessions.start_google_calendar()

    for session_id in failed_session_ids:
        with pytest.raises(KeyError):
            oauth_sessions.status(session_id)

    assert successful_session == "successful-session"
    assert oauth_sessions.status(successful_session) == {
        "session_id": "successful-session",
        "status": "pending",
    }
    assert all(
        isinstance(error, oauth_sessions.OAuthSessionStartError)
        for error in failures
    )
    assert all(
        str(error) == "Unable to start Google OAuth session."
        for error in failures
    )
    assert secret not in " ".join(str(error) for error in failures)
    assert secret not in caplog.text


def test_google_oauth_thread_start_failure_is_removed_and_redacted(
    monkeypatch, caplog
):
    from app.services import oauth_sessions

    oauth_sessions = importlib.reload(oauth_sessions)
    secret = "credential-material-in-thread-start"
    monkeypatch.setattr(
        oauth_sessions.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="failed-start-session"),
    )

    class Thread:
        def __init__(self, **_kwargs):
            return None

        def start(self):
            raise RuntimeError(secret)

    monkeypatch.setattr(oauth_sessions.threading, "Thread", Thread)

    with pytest.raises(oauth_sessions.OAuthSessionStartError) as error:
        oauth_sessions.start_google_calendar()

    with pytest.raises(KeyError):
        oauth_sessions.status("failed-start-session")
    assert str(error.value) == "Unable to start Google OAuth session."
    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_google_oauth_session_records_connected_status(monkeypatch):
    from app.auth import gcal
    from app.services import oauth_sessions

    oauth_sessions = importlib.reload(oauth_sessions)
    monkeypatch.setattr(gcal, "run_oauth_flow", lambda: None)

    class Thread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(oauth_sessions.threading, "Thread", Thread)

    session_id = oauth_sessions.start_google_calendar()

    assert oauth_sessions.status(session_id) == {
        "session_id": session_id,
        "status": "connected",
    }


def test_google_oauth_failure_status_and_logs_are_redacted(monkeypatch, caplog):
    from app.auth import gcal
    from app.services import oauth_sessions

    oauth_sessions = importlib.reload(oauth_sessions)
    secret = "oauth-client-secret-from-exception"
    monkeypatch.setattr(
        gcal,
        "run_oauth_flow",
        lambda: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    class Thread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(oauth_sessions.threading, "Thread", Thread)

    session_id = oauth_sessions.start_google_calendar()
    result = oauth_sessions.status(session_id)

    assert result == {
        "session_id": session_id,
        "status": "failed",
        "error": "Google connection failed.",
    }
    assert secret not in json.dumps(result)
    assert secret not in caplog.text


def test_google_oauth_sessions_are_bounded_to_twenty_active_workers(monkeypatch):
    from app.services import oauth_sessions

    oauth_sessions = importlib.reload(oauth_sessions)

    class Thread:
        def __init__(self, **_kwargs):
            return None

        def start(self):
            return None

    monkeypatch.setattr(oauth_sessions.threading, "Thread", Thread)

    session_ids = [oauth_sessions.start_google_calendar() for _ in range(20)]
    with pytest.raises(oauth_sessions.OAuthSessionCapacityError):
        oauth_sessions.start_google_calendar()

    assert all(oauth_sessions.status(session_id) for session_id in session_ids)


def test_completed_google_oauth_session_expires_after_thirty_minutes(monkeypatch):
    from app.auth import gcal
    from app.services import oauth_sessions

    oauth_sessions = importlib.reload(oauth_sessions)
    clock = {"now": 100.0}
    monkeypatch.setattr(oauth_sessions.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(gcal, "run_oauth_flow", lambda: None)

    class Thread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(oauth_sessions.threading, "Thread", Thread)
    session_id = oauth_sessions.start_google_calendar()
    assert oauth_sessions.status(session_id)["status"] == "connected"

    clock["now"] += 1800.001

    with pytest.raises(KeyError):
        oauth_sessions.status(session_id)


def test_unknown_google_oauth_session_returns_404(client):
    from app.services import oauth_sessions

    importlib.reload(oauth_sessions)

    response = client.get(
        "/api/settings/integrations/google-calendar/connect/not-a-session"
    )

    assert response.status_code == 404
    assert "not-a-session" not in response.text


def test_google_oauth_flow_reads_client_config_and_writes_authorized_user(
    monkeypatch, fake_keychain
):
    from app.auth import gcal
    from google_auth_oauthlib import flow as google_flow
    from requests_oauthlib import OAuth2Session

    client_config = {
        "installed": {
            "client_id": "desktop-client-id",
            "client_secret": "desktop-client-secret",
            "redirect_uris": ["http://localhost"],
        }
    }
    fake_keychain["google.client_config"] = json.dumps(client_config)
    calls = {}

    class Credentials:
        granted_scopes = ['https://www.googleapis.com/auth/calendar.events']
        def to_json(self):
            return '{"refresh_token":"authorized-user-secret"}'

    class Flow:
        oauth2session = OAuth2Session('synthetic')
        def run_local_server(self, **kwargs):
            calls["run_local_server"] = kwargs
            return Credentials()

    def from_client_config(value, scopes):
        calls["client_config"] = value
        calls["scopes"] = scopes
        return Flow()

    monkeypatch.setattr(
        google_flow.InstalledAppFlow,
        "from_client_config",
        from_client_config,
    )

    gcal.run_oauth_flow()

    assert calls["client_config"] == client_config
    assert calls["scopes"] == [
        "https://www.googleapis.com/auth/calendar.events",
    ]
    assert calls["run_local_server"] == {
        "port": 0,
        "prompt": "consent",
        "open_browser": True,
    }
    assert json.loads(fake_keychain["google.authorized_user"]) == {
        'refresh_token': 'authorized-user-secret',
        'scopes': ['https://www.googleapis.com/auth/calendar.events'],
    }


def test_google_oauth_flow_errors_never_echo_client_config(
    monkeypatch, fake_keychain
):
    from app.auth import gcal

    secret = "private-client-json-material"
    fake_keychain["google.client_config"] = f"not-json-{secret}"

    with pytest.raises(gcal.GoogleOAuthError) as error:
        gcal.run_oauth_flow()

    assert secret not in str(error.value)


def test_google_oauth_cli_calls_the_same_flow_synchronously(monkeypatch):
    from app.auth import gcal

    calls = []
    monkeypatch.setattr(gcal, "run_oauth_flow", lambda: calls.append("run"))

    result = gcal.main()

    assert result is None
    assert calls == ["run"]


def test_clickup_uses_its_explicit_schema_and_rejects_unknown_fields(
    client, fake_keychain
):
    rejected = client.put(
        "/api/settings/integrations/clickup",
        json={"token": "clickup-private", "create_list_id": "list-1", "raw_key": "x"},
    )
    unknown = client.put("/api/settings/integrations/unknown", json={"token": "x"})

    assert rejected.status_code == 422
    assert unknown.status_code == 404
    assert "clickup.token" not in fake_keychain

    accepted = client.put(
        "/api/settings/integrations/clickup",
        json={"token": "clickup-private", "create_list_id": "list-1"},
    )
    assert accepted.status_code == 200
    assert "clickup-private" not in accepted.text
    assert accepted.json()["integration"]["create_list_id"] == "list-1"


def test_settings_remain_editable_after_onboarding_and_my_work_cannot_rename_self(
    client, conn
):
    completed = client.patch("/api/onboarding", json={"completed": True, "step": 4})
    updated = client.patch(
        "/api/settings/profile", json={"display_name": "Alex", "timezone": "UTC"}
    )

    assert completed.status_code == 200
    assert updated.status_code == 200
    assert client.get("/api/settings").json()["profile"] == {
        "display_name": "Alex",
        "timezone": "UTC",
        "email_domain": "example.com",
        "separate_work_by_status": False,
        "auto_create_review_tasks": True,
        "ai_group_review_mrs": True,
    }

    self_id = conn.execute("SELECT id FROM people WHERE is_self=1").fetchone()["id"]
    assert client.get("/api/work-items/my").status_code == 200
    assert client.post("/api/work-items", json={"title": "A local card"}).status_code == 201
    assert conn.execute(
        "SELECT display_name FROM people WHERE id=?", (self_id,)
    ).fetchone()["display_name"] == "Alex"


def test_profile_persists_board_segregation_choice(client):
    assert (
        client.get("/api/settings").json()["profile"]["separate_work_by_status"]
        is False
    )

    saved = client.patch(
        "/api/settings/profile", json={"separate_work_by_status": True}
    )
    assert saved.status_code == 200
    assert saved.json()["profile"]["separate_work_by_status"] is True

    preserved = client.patch(
        "/api/settings/profile", json={"display_name": "Taylor"}
    )
    assert preserved.status_code == 200
    assert preserved.json()["profile"]["separate_work_by_status"] is True

    restored = client.patch(
        "/api/settings/profile", json={"separate_work_by_status": False}
    )
    assert restored.status_code == 200
    assert restored.json()["profile"]["separate_work_by_status"] is False


def test_review_automation_profile_defaults_enabled(client):
    profile = client.get("/api/settings").json()["profile"]
    assert profile["auto_create_review_tasks"] is True
    assert profile["ai_group_review_mrs"] is True


def test_profile_email_domain_defaults_neutral_and_drives_generated_identities(
    client, conn
):
    assert client.get("/api/settings").json()["profile"]["email_domain"] == "example.com"

    saved = client.patch(
        "/api/settings/profile", json={"email_domain": " Engineering.Example "}
    )
    assert saved.status_code == 200
    assert saved.json()["profile"]["email_domain"] == "engineering.example"

    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]
    identities = conn.execute(
        "SELECT source, external_id FROM person_identities "
        "WHERE person_id=? ORDER BY source",
        (person["id"],),
    ).fetchall()
    assert [tuple(identity) for identity in identities] == [
        ("clickup", "morgan.dev@engineering.example"),
        ("flock", "morgan.dev@engineering.example"),
        ("gitlab", "morgan.dev"),
        ("google-calendar", "morgan.dev@engineering.example"),
    ]


def test_profile_email_domain_infers_legacy_identity_before_first_save(client, conn):
    person_id = conn.execute(
        "INSERT INTO people "
        "(display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
        "VALUES ('Legacy teammate', 0, 1, 1, datetime('now'), datetime('now'))"
    ).lastrowid
    conn.execute(
        "INSERT INTO person_identities "
        "(person_id, source, external_id, display_value) VALUES (?, ?, ?, ?)",
        (person_id, "clickup", "legacy.user@legacy.example", "legacy.user@legacy.example"),
    )
    conn.commit()

    assert client.get("/api/settings").json()["profile"]["email_domain"] == "legacy.example"


def test_changing_profile_email_domain_migrates_existing_generated_identities(
    client, conn
):
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]

    response = client.patch(
        "/api/settings/profile", json={"email_domain": "engineering.example"}
    )

    assert response.status_code == 200
    identities = conn.execute(
        "SELECT source, external_id FROM person_identities "
        "WHERE person_id=? ORDER BY source",
        (person["id"],),
    ).fetchall()
    assert [tuple(identity) for identity in identities] == [
        ("clickup", "morgan.dev@engineering.example"),
        ("flock", "morgan.dev@engineering.example"),
        ("gitlab", "morgan.dev"),
        ("google-calendar", "morgan.dev@engineering.example"),
    ]


def test_email_domain_identity_collision_rolls_back_profile_and_people(client, conn):
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]
    before = conn.execute(
        "SELECT source, external_id FROM person_identities "
        "WHERE person_id=? ORDER BY source",
        (person["id"],),
    ).fetchall()
    blocker_id = conn.execute(
        "INSERT INTO people "
        "(display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
        "VALUES ('Existing account', 0, 0, 0, datetime('now'), datetime('now'))"
    ).lastrowid
    conn.execute(
        "INSERT INTO person_identities "
        "(person_id, source, external_id, display_value) VALUES (?, ?, ?, ?)",
        (
            blocker_id,
            "clickup",
            "morgan.dev@engineering.example",
            "morgan.dev@engineering.example",
        ),
    )
    conn.commit()

    response = client.patch(
        "/api/settings/profile", json={"email_domain": "engineering.example"}
    )

    assert response.status_code == 503
    assert client.get("/api/settings").json()["profile"]["email_domain"] == "example.com"
    after = conn.execute(
        "SELECT source, external_id FROM person_identities "
        "WHERE person_id=? ORDER BY source",
        (person["id"],),
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


@pytest.mark.parametrize("email_domain", ["invalid", "@example.com", "example..com"])
def test_profile_email_domain_rejects_invalid_values(client, email_domain):
    response = client.patch(
        "/api/settings/profile", json={"email_domain": email_domain}
    )
    assert response.status_code in (400, 422)


def test_review_automation_profile_persists_without_resetting_other_fields(client):
    saved = client.patch("/api/settings/profile", json={
        "auto_create_review_tasks": False,
        "ai_group_review_mrs": False,
    }).json()["profile"]
    assert saved["auto_create_review_tasks"] is False
    assert saved["ai_group_review_mrs"] is False

    renamed = client.patch(
        "/api/settings/profile", json={"display_name": "Taylor"}
    ).json()["profile"]
    assert renamed["auto_create_review_tasks"] is False
    assert renamed["ai_group_review_mrs"] is False


def test_invalid_profile_name_cannot_partially_write_a_valid_timezone(client, conn):
    self_before = conn.execute(
        "SELECT id, display_name FROM people WHERE is_self=1"
    ).fetchone()
    response = client.patch(
        "/api/settings/profile",
        json={"display_name": "   ", "timezone": "UTC"},
    )

    assert response.status_code in (400, 422)
    assert conn.execute(
        "SELECT value_json FROM app_settings WHERE key='profile.timezone'"
    ).fetchone() is None
    self_after = conn.execute(
        "SELECT id, display_name FROM people WHERE is_self=1"
    ).fetchone()
    assert tuple(self_after) == tuple(self_before)


def test_timezone_update_reschedules_running_jobs_without_restart(
    client, monkeypatch
):
    from app.services import settings_service

    rescheduled = []
    monkeypatch.setattr(
        settings_service,
        "_reschedule_timezone",
        lambda timezone: rescheduled.append(timezone),
        raising=False,
    )

    response = client.patch(
        "/api/settings/profile", json={"timezone": " Europe/London "}
    )

    assert response.status_code == 200
    assert response.json()["profile"]["timezone"] == "Europe/London"
    assert rescheduled == ["Europe/London"]


@pytest.mark.parametrize(
    "persisted_review_preferences",
    [
        None,
        {"auto_create_review_tasks": False, "ai_group_review_mrs": False},
    ],
    ids=["absent-default-on", "persisted-off"],
)
def test_timezone_reschedule_failure_restores_profile_self_runtime_and_preferences(
    client, conn, monkeypatch, persisted_review_preferences
):
    from app.services import settings_service

    if persisted_review_preferences is not None:
        saved = client.patch(
            "/api/settings/profile", json=persisted_review_preferences
        )
        assert saved.status_code == 200

    teammate = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]
    before_email_identities = conn.execute(
        "SELECT id, person_id, source, external_id, display_value "
        "FROM person_identities WHERE person_id=? ORDER BY id",
        (teammate["id"],),
    ).fetchall()

    before_profile = client.get("/api/settings").json()["profile"]
    before_review_preference_rows = conn.execute(
        "SELECT key, value_json FROM app_settings"
        " WHERE key IN (?, ?) ORDER BY key",
        ("profile.ai_group_review_mrs", "profile.auto_create_review_tasks"),
    ).fetchall()
    before_runtime_timezone = settings.tz
    before_self = conn.execute(
        "SELECT id, display_name, updated_at FROM people WHERE is_self=1"
    ).fetchone()
    requested_timezone = (
        "UTC" if before_profile["timezone"] != "UTC" else "Asia/Kolkata"
    )

    def fail_reschedule(_timezone):
        raise RuntimeError("scheduler-secret-must-not-leak")

    monkeypatch.setattr(settings_service, "_reschedule_timezone", fail_reschedule)
    monkeypatch.setattr(settings_service, "now_iso", lambda: "2099-01-01T00:00:00")

    response = client.patch(
        "/api/settings/profile",
        json={
            "display_name": "Changed name",
            "timezone": requested_timezone,
            "email_domain": "engineering.example",
            "auto_create_review_tasks": not before_profile[
                "auto_create_review_tasks"
            ],
            "ai_group_review_mrs": not before_profile["ai_group_review_mrs"],
        },
    )

    assert response.status_code == 503
    assert "scheduler-secret-must-not-leak" not in response.text
    assert client.get("/api/settings").json()["profile"] == before_profile
    after_review_preference_rows = conn.execute(
        "SELECT key, value_json FROM app_settings"
        " WHERE key IN (?, ?) ORDER BY key",
        ("profile.ai_group_review_mrs", "profile.auto_create_review_tasks"),
    ).fetchall()
    assert [tuple(row) for row in after_review_preference_rows] == [
        tuple(row) for row in before_review_preference_rows
    ]
    after_self = conn.execute(
        "SELECT id, display_name, updated_at FROM people WHERE is_self=1"
    ).fetchone()
    assert tuple(after_self) == tuple(before_self)
    assert settings.tz == before_runtime_timezone
    after_email_identities = conn.execute(
        "SELECT id, person_id, source, external_id, display_value "
        "FROM person_identities WHERE person_id=? ORDER BY id",
        (teammate["id"],),
    ).fetchall()
    assert [tuple(row) for row in after_email_identities] == [
        tuple(row) for row in before_email_identities
    ]


def test_scheduler_timezone_helper_rebuilds_all_cron_triggers(monkeypatch):
    from app import main

    calls = []

    class Job:
        trigger = "old"

    class Scheduler:
        def get_job(self, _job_id):
            return Job()

        def reschedule_job(self, job_id, **kwargs):
            calls.append((job_id, kwargs))

    monkeypatch.setattr(main, "_scheduler", Scheduler(), raising=False)
    main.reschedule_scheduler_timezone("UTC")

    assert [job_id for job_id, _ in calls] == [
        "connector_sync",
        "morning_digest",
        "daily_export",
    ]
    assert all(call["trigger"] == "cron" for _, call in calls)
    assert all(str(call["timezone"]) == "UTC" for _, call in calls)


def test_scheduler_timezone_helper_restores_every_trigger_after_partial_failure(
    monkeypatch,
):
    from app import main

    old_triggers = {
        "connector_sync": object(),
        "morning_digest": object(),
        "daily_export": object(),
    }
    jobs = {
        job_id: SimpleNamespace(trigger=trigger)
        for job_id, trigger in old_triggers.items()
    }
    restored = []

    class Scheduler:
        def get_job(self, job_id):
            return jobs[job_id]

        def reschedule_job(self, job_id, **kwargs):
            trigger = kwargs["trigger"]
            if trigger == "cron":
                if job_id == "morning_digest":
                    raise RuntimeError("second reschedule failed")
                jobs[job_id].trigger = ("new", kwargs["timezone"])
            else:
                jobs[job_id].trigger = trigger
                restored.append(job_id)

    monkeypatch.setattr(main, "_scheduler", Scheduler(), raising=False)

    with pytest.raises(RuntimeError, match="second reschedule failed"):
        main.reschedule_scheduler_timezone("UTC")

    assert {
        job_id: job.trigger for job_id, job in jobs.items()
    } == old_triggers
    assert restored == ["connector_sync", "morning_digest", "daily_export"]


def test_onboarding_state_persists_but_does_not_gate_settings(client):
    assert client.get("/api/onboarding").json() == {"completed": False, "step": 1}
    assert client.patch("/api/onboarding", json={"step": 3}).json() == {
        "completed": False,
        "step": 3,
    }
    assert client.get("/api/onboarding").json()["step"] == 3
    assert client.patch(
        "/api/settings/profile", json={"display_name": "Still editable"}
    ).status_code == 200


def test_person_identifier_generates_all_connector_identities(client, conn):
    response = client.post(
        "/api/settings/people",
        json={"display_name": "Taylor", "identifier": "taylor.dev"},
    )

    assert response.status_code == 201
    person = response.json()["person"]
    assert person["identifier"] == "taylor.dev"
    identities = conn.execute(
        "SELECT source, external_id FROM person_identities "
        "WHERE person_id=? ORDER BY source",
        (person["id"],),
    ).fetchall()
    assert [tuple(identity) for identity in identities] == [
        ("clickup", "taylor.dev@example.com"),
        ("flock", "taylor.dev@example.com"),
        ("gitlab", "taylor.dev"),
        ("google-calendar", "taylor.dev@example.com"),
    ]


def test_editing_identifier_replaces_every_generated_identity(client, conn):
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Taylor", "identifier": "taylor.old"},
    ).json()["person"]

    response = client.patch(
        f"/api/settings/people/{person['id']}",
        json={"display_name": "Taylor S", "identifier": "taylor.dev"},
    )

    assert response.status_code == 200
    assert response.json()["person"]["identifier"] == "taylor.dev"
    assert {
        row["external_id"]
        for row in conn.execute(
            "SELECT external_id FROM person_identities WHERE person_id=?",
            (person["id"],),
        )
    } == {"taylor.dev", "taylor.dev@example.com"}


def test_person_identifier_rejects_an_email_address(client):
    response = client.post(
        "/api/settings/people",
        json={"display_name": "Taylor", "identifier": "taylor.dev@example.com"},
    )

    assert response.status_code == 422


def test_people_api_rejects_per_app_identity_and_tracking_fields(client):
    response = client.post(
        "/api/settings/people",
        json={
            "display_name": "Morgan",
            "identifier": "morgan.dev",
            "is_tracked": False,
            "identities": [{"source": "gitlab", "external_id": "morgan.dev"}],
        },
    )

    assert response.status_code == 422


def test_people_settings_lists_only_self_and_explicitly_tracked_people(client, conn):
    cursor = conn.execute(
        "INSERT INTO people "
        "(display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
        "VALUES ('Auto-discovered GitLab user', 0, 0, 0, datetime('now'), datetime('now'))"
    )
    hidden_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO person_identities (person_id, source, external_id) "
        "VALUES (?, 'gitlab', 'discovered.user')",
        (hidden_id,),
    )
    conn.commit()
    tracked = client.post(
        "/api/settings/people",
        json={"display_name": "Added person", "identifier": "added.person"},
    ).json()["person"]

    listed = client.get("/api/settings/people").json()["people"]

    assert hidden_id not in {person["id"] for person in listed}
    assert tracked["id"] in {person["id"] for person in listed}
    assert all(person["is_self"] or person["is_tracked"] for person in listed)


def test_adding_a_hidden_discovered_identity_restores_it_as_tracked(client, conn):
    cursor = conn.execute(
        "INSERT INTO people "
        "(display_name, is_self, is_tracked, lane_position, created_at, updated_at) "
        "VALUES ('Discovered name', 0, 0, 0, datetime('now'), datetime('now'))"
    )
    hidden_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO person_identities (person_id, source, external_id) "
        "VALUES (?, 'gitlab', 'morgan.dev')",
        (hidden_id,),
    )
    conn.commit()

    response = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    )

    assert response.status_code == 201
    restored = response.json()["person"]
    assert restored["id"] == hidden_id
    assert restored["display_name"] == "Morgan"
    assert restored["is_tracked"] is True
    assert restored["lane_position"] > 0
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM person_identities "
        "WHERE source='gitlab' AND external_id='morgan.dev'"
    ).fetchone()["count"] == 1


def test_people_display_name_can_be_edited_without_changing_identifier(client):
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]
    updated = client.patch(
        f"/api/settings/people/{person['id']}",
        json={"display_name": "Morgan G"},
    )

    assert updated.status_code == 200
    assert updated.json()["person"]["display_name"] == "Morgan G"
    assert updated.json()["person"]["is_tracked"] is True
    assert updated.json()["person"]["identifier"] == "morgan.dev"


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": "   ", "identifier": "morgan.dev"},
        {"display_name": "Morgan", "identifier": "   "},
        {"display_name": "Morgan", "identifier": "morgan g"},
    ],
)
def test_blank_or_invalid_person_values_are_rejected_before_persistence(
    client, conn, payload
):
    response = client.post("/api/settings/people", json=payload)

    assert response.status_code in (400, 422)
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM people WHERE is_self=0"
    ).fetchone()["count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM person_identities"
    ).fetchone()["count"] == 0


def test_person_values_are_stripped_and_identifier_is_normalized(client):
    response = client.post(
        "/api/settings/people",
        json={
            "display_name": "  Morgan  ",
            "identifier": " Morgan.Dev ",
        },
    )

    assert response.status_code == 201
    person = response.json()["person"]
    assert person["display_name"] == "Morgan"
    assert person["identifier"] == "morgan.dev"


def test_blank_person_patch_is_rejected_without_changing_existing_name(client):
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Morgan", "identifier": "morgan.dev"},
    ).json()["person"]

    response = client.patch(
        f"/api/settings/people/{person['id']}", json={"display_name": "   "}
    )

    assert response.status_code in (400, 422)
    saved = next(
        item
        for item in client.get("/api/settings/people").json()["people"]
        if item["id"] == person["id"]
    )
    assert saved["display_name"] == "Morgan"


def test_deleting_a_linked_person_demotes_them_and_preserves_cards(client, conn):
    person = client.post(
        "/api/settings/people",
        json={"display_name": "Priya", "identifier": "priya.s"},
    ).json()["person"]
    item = client.post(
        "/api/work-items",
        json={"title": "Review rollout", "owner_person_id": person["id"]},
    ).json()["work_item"]

    removed = client.delete(f"/api/settings/people/{person['id']}")

    assert removed.status_code == 200
    assert removed.json()["deleted"] is False
    assert removed.json()["demoted"] is True
    retained = conn.execute("SELECT is_tracked FROM people WHERE id=?", (person["id"],)).fetchone()
    assert retained is not None and retained["is_tracked"] == 0
    others = next(
        lane for lane in client.get("/api/work-items/team").json()["lanes"]
        if lane["name"] == "Others"
    )
    assert [work["id"] for work in others["items"]] == [item["id"]]
    assert person["id"] not in {
        listed["id"]
        for listed in client.get("/api/settings/people").json()["people"]
    }


def test_deleting_an_unlinked_person_removes_only_that_person(client, conn):
    first = client.post(
        "/api/settings/people",
        json={"display_name": "Delete me", "identifier": "delete.me"},
    ).json()["person"]
    second = client.post(
        "/api/settings/people",
        json={"display_name": "Keep me", "identifier": "keep.me"},
    ).json()["person"]

    response = client.delete(f"/api/settings/people/{first['id']}")

    assert response.json() == {"deleted": True, "demoted": False}
    assert conn.execute("SELECT 1 FROM people WHERE id=?", (first["id"],)).fetchone() is None
    assert conn.execute("SELECT 1 FROM people WHERE id=?", (second["id"],)).fetchone()


def test_disconnect_requires_confirmation_and_preserves_cache_and_local_work(
    client, conn, fake_keychain
):
    client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://gitlab.example.com", "token": "private"},
    )
    conn.execute(
        "INSERT INTO gitlab_mrs_cache (mr_id, title, state) VALUES ('1!2', 'Cached MR', 'opened')"
    )
    conn.commit()
    work_id = client.post("/api/work-items", json={"title": "Local work"}).json()[
        "work_item"
    ]["id"]

    assert client.delete("/api/settings/integrations/gitlab").status_code == 400
    disconnected = client.delete("/api/settings/integrations/gitlab?confirm=true")

    assert disconnected.status_code == 200
    assert disconnected.json()["integration"]["credential_present"] is False
    assert disconnected.json()["integration"]["configured"] is False
    assert "gitlab.token" not in fake_keychain
    assert conn.execute("SELECT title FROM gitlab_mrs_cache WHERE mr_id='1!2'").fetchone()[
        "title"
    ] == "Cached MR"
    assert conn.execute("SELECT title FROM work_items WHERE id=?", (work_id,)).fetchone()[
        "title"
    ] == "Local work"


def test_google_and_flock_real_configured_gates_honor_disconnect_tombstones(
    client, conn, tmp_path, monkeypatch, fake_keychain
):
    from app.services import flock_client, gcal_client

    credentials = tmp_path / "gcal-client.json"
    token = tmp_path / "gcal-token.json"
    credentials.write_text("client")
    token.write_text("token")
    profile = tmp_path / "flock-profile"
    profile.mkdir()
    cookie_db = profile / "Default" / "Network" / "Cookies"
    cookie_db.parent.mkdir(parents=True)
    cookies = sqlite3.connect(cookie_db)
    cookies.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, "
        "expires_utc INTEGER)"
    )
    cookies.execute(
        "INSERT INTO cookies (host_key, name, encrypted_value, expires_utc) "
        "VALUES (?, ?, ?, ?)",
        (".flock.com", "flock-login", b"session", 0),
    )
    cookies.commit()
    cookies.close()
    monkeypatch.setattr(settings, "gcal_credentials_path", str(credentials))
    monkeypatch.setattr(settings, "gcal_token_path", str(token))
    monkeypatch.setattr(settings, "flock_profile_dir", str(profile))
    fake_keychain["google.client_config"] = "client"
    fake_keychain["google.authorized_user"] = "token"

    conn.execute(
        "INSERT INTO gcal_events_cache (event_id, title, start_at) "
        "VALUES ('event-1', 'Cached meeting', '2026-08-27T10:00:00')"
    )
    conn.execute(
        "INSERT INTO flock_mentions_cache (jid, name) VALUES ('u:1', 'Cached mention')"
    )
    conn.commit()
    work_id = client.post("/api/work-items", json={"title": "Local work"}).json()[
        "work_item"
    ]["id"]

    assert gcal_client.configured() is True
    assert flock_client.configured() is True
    assert client.delete(
        "/api/settings/integrations/google-calendar?confirm=true"
    ).status_code == 200
    assert client.delete(
        "/api/settings/integrations/flock?confirm=true"
    ).status_code == 200

    assert gcal_client.configured() is False
    assert flock_client.configured() is False
    assert conn.execute(
        "SELECT title FROM gcal_events_cache WHERE event_id='event-1'"
    ).fetchone()["title"] == "Cached meeting"
    assert conn.execute(
        "SELECT name FROM flock_mentions_cache WHERE jid='u:1'"
    ).fetchone()["name"] == "Cached mention"
    assert conn.execute("SELECT 1 FROM work_items WHERE id=?", (work_id,)).fetchone()


def test_flock_disconnect_quarantines_only_the_managed_profile_and_preserves_local_data(
    client, conn
):
    from app.config import settings

    profile = settings.data_dir / "chrome-profile-flock"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Cookies").write_text("authenticated-session")
    unrelated_cache = settings.data_dir / "connector-cache"
    unrelated_cache.mkdir(exist_ok=True)
    (unrelated_cache / "cached.json").write_text("keep")
    conn.execute(
        "INSERT INTO flock_mentions_cache (jid, name) VALUES ('u:managed', 'Keep mention')"
    )
    conn.commit()
    work_id = client.post("/api/work-items", json={"title": "Keep local work"}).json()[
        "work_item"
    ]["id"]
    assert client.put(
        "/api/settings/integrations/flock",
        json={"profile_dir": str(profile), "user_handle": "Taylor"},
    ).status_code == 200

    response = client.delete("/api/settings/integrations/flock?confirm=true")

    assert response.status_code == 200
    assert response.json()["integration"]["disconnect_state"] == "quarantined"
    assert response.json()["integration"]["configured"] is False
    assert not profile.exists()
    quarantined = list((settings.data_dir / "disconnected-profiles").iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / "Cookies").read_text() == "authenticated-session"
    assert (unrelated_cache / "cached.json").read_text() == "keep"
    assert conn.execute(
        "SELECT name FROM flock_mentions_cache WHERE jid='u:managed'"
    ).fetchone()["name"] == "Keep mention"
    assert conn.execute("SELECT title FROM work_items WHERE id=?", (work_id,)).fetchone()[
        "title"
    ] == "Keep local work"


def test_flock_disconnect_refuses_an_outside_profile_but_stays_disabled(
    client, conn, tmp_path
):
    from app.services import app_settings, flock_client

    outside = tmp_path / "outside-flock-profile"
    outside.mkdir()
    (outside / "Cookies").write_text("outside-session")
    client.put(
        "/api/settings/integrations/flock",
        json={"profile_dir": str(outside), "user_handle": "Taylor"},
    )

    response = client.delete("/api/settings/integrations/flock?confirm=true")

    integration = response.json()["integration"]
    assert response.status_code == 200
    assert integration["disconnect_state"] == "unsafe_profile_left_in_place"
    assert integration["disconnect_guidance"] == (
        "The configured profile was not Watson-managed; it was left untouched. "
        "Review it manually before removing anything."
    )
    assert (outside / "Cookies").read_text() == "outside-session"
    assert app_settings.get(conn, "integration.flock.disabled") is True
    assert flock_client.configured() is False
    assert str(outside) not in integration["disconnect_guidance"]


def test_flock_quarantine_failure_leaves_profile_and_integration_disabled(
    client, conn, tmp_path, monkeypatch
):
    from app.config import settings
    from app.services import app_settings, flock_client

    profile = settings.data_dir / "chrome-profile-flock"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Cookies").write_text("authenticated-session")
    client.put(
        "/api/settings/integrations/flock",
        json={"profile_dir": str(profile), "user_handle": "Taylor"},
    )
    original_rename = Path.rename

    def fail_profile_rename(path, target):
        if path == profile:
            raise OSError("credential-bearing filesystem sentinel")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_profile_rename)

    response = client.delete("/api/settings/integrations/flock?confirm=true")

    assert response.status_code == 200
    assert response.json()["integration"]["disconnect_state"] == "quarantine_failed"
    assert (profile / "Cookies").read_text() == "authenticated-session"
    assert app_settings.get(conn, "integration.flock.disabled") is True
    assert flock_client.configured() is False
    assert "credential-bearing" not in response.text


def test_failed_keychain_update_keeps_old_nonsecrets_disabled(
    client, conn, monkeypatch, fake_keychain
):
    from app.services import app_settings

    assert client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://old.gitlab.example.com", "token": "old-token"},
    ).status_code == 200

    def fail_set(name, value):
        raise secret_store.SecretStoreError(f"Unable to access Keychain secret '{name}'.")

    monkeypatch.setattr(secret_store, "set_secret", fail_set)
    response = client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://new.gitlab.example.com", "token": "new-token"},
    )

    assert response.status_code == 503
    assert app_settings.get(conn, "integration.gitlab.base_url") == (
        "https://old.gitlab.example.com"
    )
    assert app_settings.get(conn, "integration.gitlab.disabled") is True
    assert fake_keychain["gitlab.token"] == "old-token"
    assert settings.gitlab_token == ""
    view = client.get("/api/settings").json()["integrations"]["gitlab"]
    assert view["base_url"] == "https://old.gitlab.example.com"
    assert view["configured"] is False
    assert "new-token" not in response.text


def test_failed_keychain_delete_leaves_config_disabled_and_preserved(
    client, conn, monkeypatch
):
    from app.services import app_settings

    client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://gitlab.example.com", "token": "private"},
    )

    def fail_delete(name):
        raise secret_store.SecretStoreError(f"Unable to access Keychain secret '{name}'.")

    monkeypatch.setattr(secret_store, "delete_secret", fail_delete)
    response = client.delete("/api/settings/integrations/gitlab?confirm=true")

    assert response.status_code == 503
    assert app_settings.get(conn, "integration.gitlab.base_url") == (
        "https://gitlab.example.com"
    )
    assert app_settings.get(conn, "integration.gitlab.disabled") is True
    assert settings.gitlab_token == ""
    assert client.get("/api/settings").json()["integrations"]["gitlab"][
        "configured"
    ] is False


def test_keychain_access_error_fails_closed_instead_of_using_stale_env(
    client, conn, monkeypatch
):
    from app.services import app_settings, gitlab_client, settings_service

    monkeypatch.setenv("GITLAB_TOKEN", "stale-env-token")
    app_settings.set_value(
        conn, "integration.gitlab.base_url", "https://gitlab.example.com"
    )

    def unavailable(*args, **kwargs):
        raise secret_store.SecretStoreError("redacted keychain failure")

    monkeypatch.setattr(secret_store, "effective_secret", unavailable)
    monkeypatch.setattr(secret_store, "has_secret", unavailable)
    settings_service.apply_runtime_settings(conn)

    assert settings.gitlab_token == ""
    assert gitlab_client.configured() is False
    response = client.get("/api/settings")
    view = response.json()["integrations"]["gitlab"]
    assert view["configured"] is False
    assert view["credential_present"] is False
    assert view["health"] == {
        "status": "unavailable",
        "error": "Credential store unavailable.",
    }
    assert "stale-env-token" not in response.text


def test_import_env_is_explicit_and_does_not_modify_dotenv(client, monkeypatch, fake_keychain):
    dotenv = Path(".env")
    before = dotenv.read_bytes() if dotenv.exists() else None
    monkeypatch.setenv("GITLAB_BASE_URL", "https://env.gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "env-private")
    monkeypatch.setenv("GITLAB_USERNAME", "env-user")

    assert "gitlab.token" not in fake_keychain
    imported = client.post("/api/settings/integrations/gitlab/import-env")

    assert imported.status_code == 200
    assert "env-private" not in imported.text
    assert fake_keychain["gitlab.token"] == "env-private"
    assert imported.json()["integration"]["base_url"] == "https://env.gitlab.example.com"
    assert (dotenv.read_bytes() if dotenv.exists() else None) == before


def test_successful_gitlab_test_upserts_only_self_identity(client, conn, monkeypatch):
    from app.services import gitlab_client

    client.patch("/api/settings/profile", json={"display_name": "Alex"})
    client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://gitlab.example.com", "token": "private"},
    )
    monkeypatch.setattr(
        gitlab_client,
        "current_user",
        lambda: {"id": 41, "username": "alex.gitlab", "name": "Alex"},
    )

    response = client.post("/api/settings/integrations/gitlab/test")

    assert response.status_code == 200
    assert response.json()["health"] == {"status": "connected"}
    identities = conn.execute(
        "SELECT pi.source, pi.external_id, p.is_self, p.is_tracked "
        "FROM person_identities pi JOIN people p ON p.id=pi.person_id"
    ).fetchall()
    assert [tuple(row) for row in identities] == [("gitlab", "alex.gitlab", 1, 1)]
    assert conn.execute("SELECT COUNT(*) AS count FROM people").fetchone()["count"] == 1


def test_connection_failure_is_redacted_from_response_and_logs(
    client, monkeypatch, caplog
):
    from app.services import gitlab_client

    secret = "private-token-in-exception"
    client.put(
        "/api/settings/integrations/gitlab",
        json={"base_url": "https://gitlab.example.com", "token": secret},
    )
    monkeypatch.setattr(
        gitlab_client,
        "current_user",
        lambda: (_ for _ in ()).throw(RuntimeError(f"connection failed with {secret}")),
    )

    response = client.post("/api/settings/integrations/gitlab/test")

    assert response.status_code == 200
    assert response.json()["health"] == {
        "status": "failed",
        "error": "Connection test failed.",
    }
    assert secret not in response.text
    assert secret not in caplog.text


def test_anthropic_connection_client_has_explicit_deadline_and_no_retries(monkeypatch):
    from app.services import settings_service

    constructor_calls = []

    class Models:
        def list(self, **kwargs):
            return []

    def anthropic_client(**kwargs):
        constructor_calls.append(kwargs)
        return SimpleNamespace(models=Models())

    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=anthropic_client)
    )
    monkeypatch.setattr(settings, "anthropic_api_key", "private")

    settings_service._test_anthropic()

    assert constructor_calls[0]["timeout"] <= 20
    assert constructor_calls[0]["max_retries"] == 0


def test_connection_test_runner_is_single_flight_per_source_and_cleans_up():
    from app.services import settings_service

    release = threading.Event()
    started = threading.Event()
    starts = []

    def hung_test():
        starts.append("gitlab")
        started.set()
        release.wait()

    try:
        with pytest.raises(TimeoutError):
            settings_service._run_bounded(
                "gitlab", hung_test, timeout_seconds=0.01
            )
        assert started.wait(0.2)
        run = settings_service._connection_test_runs["gitlab"]

        for _ in range(2):
            with pytest.raises(settings_service.ConnectionTestBusy):
                settings_service._run_bounded(
                    "gitlab", hung_test, timeout_seconds=0.01
                )

        assert settings_service._run_bounded(
            "clickup", lambda: "independent", timeout_seconds=0.1
        ) == "independent"
        assert starts == ["gitlab"]
    finally:
        release.set()
        if "run" in locals():
            run.worker.join(0.5)

    assert "gitlab" not in settings_service._connection_test_runs


def test_connection_test_busy_is_a_controlled_redacted_response(client, monkeypatch):
    from app.services import settings_service

    monkeypatch.setattr(
        settings_service,
        "_run_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            settings_service.ConnectionTestBusy("busy-secret")
        ),
    )

    response = client.post("/api/settings/integrations/gitlab/test")

    assert response.status_code in (409, 429, 503)
    assert "busy-secret" not in response.text


def test_google_service_uses_explicit_transport_deadline(monkeypatch):
    from app.services import gcal_client
    import google_auth_httplib2
    import httplib2
    from googleapiclient import discovery

    calls = {}
    credentials = object()
    transport = object()
    authorized_transport = object()

    def make_http(**kwargs):
        calls["http"] = kwargs
        return transport

    def authorize(creds, *, http):
        calls["authorized"] = (creds, http)
        return authorized_transport

    def build(*args, **kwargs):
        calls["build"] = (args, kwargs)
        return object()

    def load_credentials(config=None, *, timeout_seconds):
        calls["credential_config"] = config
        calls["credential_timeout"] = timeout_seconds
        return credentials

    monkeypatch.setattr(gcal_client, "_load_credentials", load_credentials)
    monkeypatch.setattr(httplib2, "Http", make_http)
    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", authorize)
    monkeypatch.setattr(discovery, "build", build)

    gcal_client._service(timeout_seconds=20)

    assert 0 < calls["credential_timeout"] <= 20
    assert calls["credential_config"] == {
        "client_config": "",
        "authorized_user": "",
    }
    assert 0 < calls["http"]["timeout"] <= 20
    assert calls["authorized"] == (credentials, transport)
    assert calls["build"][1]["http"] is authorized_transport
    assert "credentials" not in calls["build"][1]


def test_expired_google_credentials_refresh_with_explicit_deadline(
    monkeypatch, fake_keychain
):
    from app.services import gcal_client
    from google.auth.transport import requests as google_requests
    from google.oauth2 import credentials as google_credentials

    fake_keychain["google.authorized_user"] = "{}"
    calls = {}

    class Request:
        def __call__(self, **kwargs):
            calls["refresh_timeout"] = kwargs.get("timeout")
            return object()

    class Credentials:
        expired = True
        refresh_token = "refresh-token"

        def refresh(self, request):
            request(method="POST", url="https://oauth.example/token")

        def to_json(self):
            return "{}"

    monkeypatch.setattr(google_requests, "Request", Request)
    monkeypatch.setattr(
        google_credentials.Credentials,
        "from_authorized_user_info",
        lambda *_args, **_kwargs: Credentials(),
    )

    gcal_client._load_credentials(timeout_seconds=20)

    assert 0 < calls["refresh_timeout"] <= 20


def test_google_connection_timeout_returns_only_generic_health(client, monkeypatch):
    from app.services import settings_service

    monkeypatch.setattr(
        settings_service,
        "_run_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("client-json-secret")),
        raising=False,
    )

    response = client.post("/api/settings/integrations/google-calendar/test")

    assert response.status_code == 200
    assert response.json()["health"] == {
        "status": "failed",
        "error": "Connection test failed.",
    }
    assert "client-json-secret" not in response.text


def test_flock_configured_requires_a_real_flock_cookie_database(
    conn, tmp_path, monkeypatch
):
    from app.services import flock_client

    profile = tmp_path / "flock-profile"
    profile.mkdir()
    (profile / "unrelated-cache-file").write_text("not authentication")
    monkeypatch.setattr(settings, "flock_profile_dir", str(profile))

    assert flock_client.configured() is False

    cookie_db = profile / "Default" / "Network" / "Cookies"
    cookie_db.parent.mkdir(parents=True)
    cookies = sqlite3.connect(cookie_db)
    cookies.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, "
        "expires_utc INTEGER)"
    )
    cookies.execute(
        "INSERT INTO cookies (host_key, name, encrypted_value, expires_utc) "
        "VALUES (?, ?, ?, ?)",
        (".flock.com", "flock-login", b"encrypted-session", 0),
    )
    cookies.commit()
    cookies.close()

    assert flock_client.configured() is True


def test_flock_authenticated_session_probe_is_read_only_and_domain_specific(
    tmp_path
):
    from app.services import flock_client

    profile = tmp_path / "flock-profile"
    cookie_db = profile / "Default" / "Cookies"
    cookie_db.parent.mkdir(parents=True)
    cookies = sqlite3.connect(cookie_db)
    cookies.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, "
        "expires_utc INTEGER)"
    )
    cookies.execute(
        "INSERT INTO cookies (host_key, name, encrypted_value, expires_utc) "
        "VALUES (?, ?, ?, ?)",
        ("example.com", "flock-login", b"encrypted-other-session", 0),
    )
    cookies.commit()
    cookies.close()
    before = cookie_db.read_bytes()
    before_mtime = cookie_db.stat().st_mtime_ns

    assert flock_client.probe_authenticated_session(profile, timeout_seconds=1) is False

    cookies = sqlite3.connect(cookie_db)
    cookies.execute(
        "INSERT INTO cookies (host_key, name, encrypted_value, expires_utc) "
        "VALUES (?, ?, ?, ?)",
        (".flock.com", "flock-login", b"encrypted-flock-session", 0),
    )
    cookies.commit()
    cookies.close()
    expected = cookie_db.read_bytes()
    expected_mtime = cookie_db.stat().st_mtime_ns

    assert flock_client.probe_authenticated_session(profile, timeout_seconds=1) is True
    assert cookie_db.read_bytes() == expected
    assert cookie_db.stat().st_mtime_ns == expected_mtime
    assert before != expected or before_mtime != expected_mtime


def test_flock_settings_test_uses_bounded_probe_and_redacts_failure(
    client, conn, tmp_path, monkeypatch, caplog
):
    from app.services import app_settings, flock_client

    profile = tmp_path / "flock-profile"
    profile.mkdir()
    client.put(
        "/api/settings/integrations/flock",
        json={"profile_dir": str(profile), "user_handle": "Taylor"},
    )
    timeouts = []

    def fail_probe(_profile, *, timeout_seconds):
        timeouts.append(timeout_seconds)
        raise RuntimeError("flock-cookie-credential-sentinel")

    monkeypatch.setattr(
        flock_client, "probe_authenticated_session", fail_probe, raising=False
    )

    response = client.post("/api/settings/integrations/flock/test")

    assert response.status_code == 200
    assert response.json()["health"] == {
        "status": "failed",
        "error": "Connection test failed.",
    }
    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 20
    assert app_settings.get(conn, "integration.flock.health") == response.json()["health"]
    assert "flock-cookie-credential-sentinel" not in response.text
    assert "flock-cookie-credential-sentinel" not in caplog.text


def test_clickup_connection_test_rechecks_rotated_credentials(client, monkeypatch):
    from app.services import clickup_client

    client.put(
        "/api/settings/integrations/clickup",
        json={"token": "rotated-token", "create_list_id": "list-1"},
    )
    clickup_client._user_id_cache = "stale-user"
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"user": {"id": "fresh-user"}}

    monkeypatch.setattr(
        clickup_client.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    )

    response = client.post("/api/settings/integrations/clickup/test")

    assert response.status_code == 200
    assert response.json()["health"] == {"status": "connected"}
    assert len(calls) == 1


def test_backup_path_cannot_equal_live_data_dir_and_is_resolved_each_export(
    client, conn, tmp_path
):
    from app.services import exporter

    live_db = db.db_path().resolve()
    rejected = client.patch(
        "/api/settings/data", json={"backup_dir": str(settings.data_dir)}
    )
    assert rejected.status_code == 400
    assert db.db_path().resolve() == live_db

    first_root = tmp_path / "first"
    accepted = client.patch(
        "/api/settings/data", json={"backup_dir": str(first_root)}
    )
    first_export = exporter.export_day(conn, date.today())
    second_root = tmp_path / "second"
    client.patch("/api/settings/data", json={"backup_dir": str(second_root)})
    second_export = exporter.export_day(conn, date.today())

    assert accepted.status_code == 200
    assert accepted.json()["data"]["backup_dir"] == str(first_root.resolve())
    assert first_export.parent == first_root.resolve() / "journal"
    assert second_export.parent == second_root.resolve() / "journal"
    assert db.db_path().resolve() == live_db


def test_backup_path_rejects_live_database_file_and_existing_regular_file(
    client, tmp_path
):
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("content")

    live_response = client.patch(
        "/api/settings/data", json={"backup_dir": str(db.db_path())}
    )
    file_response = client.patch(
        "/api/settings/data", json={"backup_dir": str(regular_file)}
    )

    assert live_response.status_code == 400
    assert file_response.status_code == 400


def test_backup_path_resolution_errors_are_mapped_to_bad_request(client, tmp_path):
    symlink_loop = tmp_path / "loop"
    symlink_loop.symlink_to(symlink_loop)

    response = client.patch(
        "/api/settings/data", json={"backup_dir": str(symlink_loop)}
    )

    assert response.status_code == 400
