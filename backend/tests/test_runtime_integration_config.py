import json
import sys
import time
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import (
    asker,
    classifier,
    clickup_client,
    flock_client,
    flock_webhook_client,
    gcal_client,
    gitlab_client,
    secret_store,
)


def _runtime_nonsecrets(values):
    return lambda key, fallback: values.get(key, fallback)


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_runtime_config_snapshots_are_immutable(monkeypatch):
    monkeypatch.setattr(
        gitlab_client.app_settings, "integration_disabled", lambda source: False
    )
    monkeypatch.setattr(
        gitlab_client.app_settings,
        "effective_nonsecret",
        lambda key, fallback: fallback,
    )
    monkeypatch.setattr(
        gitlab_client.secret_store,
        "effective_secret",
        lambda name, fallback="": fallback,
    )
    monkeypatch.setattr(
        gcal_client.secret_store, "get_secret", lambda name: "keychain-value"
    )

    configs = (
        gitlab_client.gitlab_config(),
        clickup_client.clickup_config(),
        gcal_client.gcal_config(),
        flock_client.flock_config(),
        classifier.anthropic_config(),
    )
    for config in configs:
        key = next(iter(config))
        with pytest.raises(TypeError):
            config[key] = "mutated"


def test_gitlab_current_user_uses_one_snapshot_for_url_and_header(monkeypatch):
    generations = []

    def changing_config():
        generation = len(generations) + 1
        generations.append(generation)
        return {
            "base_url": f"https://gitlab-{generation}.example.com",
            "username": "",
            "token": f"token-{generation}",
        }

    request = {}

    def get(url, *, headers, timeout):
        request.update(url=url, headers=headers)
        return _Response({"id": 7, "username": "runtime-user"})

    monkeypatch.setattr(gitlab_client, "gitlab_config", changing_config)
    monkeypatch.setattr(gitlab_client.requests, "get", get)

    assert gitlab_client.current_user()["username"] == "runtime-user"
    assert generations == [1]
    assert request == {
        "url": "https://gitlab-1.example.com/api/v4/user",
        "headers": {"PRIVATE-TOKEN": "token-1"},
    }


def test_gitlab_unconfigured_boundaries_issue_zero_requests(conn, monkeypatch):
    blank = {"base_url": "", "username": "", "token": ""}
    monkeypatch.setattr(gitlab_client, "gitlab_config", lambda: blank)

    def unexpected(*args, **kwargs):
        pytest.fail("unconfigured GitLab issued a network request")

    monkeypatch.setattr(gitlab_client.requests, "get", unexpected)
    monkeypatch.setattr(gitlab_client.requests, "put", unexpected)

    for operation in (
        gitlab_client.current_user,
        gitlab_client.current_username,
        lambda: gitlab_client.drop_self_as_reviewer("1!2", 7),
        lambda: gitlab_client.close_mr("1!2"),
        lambda: gitlab_client.fetch_engaged_mr_ids("2026-08-01"),
        gitlab_client.fetch_mrs,
        lambda: gitlab_client.sync(conn),
    ):
        with pytest.raises(RuntimeError, match="GitLab is not configured"):
            operation()

    assert gitlab_client.fetch_mr_by_path("group/project", 2) is None
    assert gitlab_client.ensure_mrs_cached(
        conn, "https://gitlab.example/group/project/-/merge_requests/2"
    ) == []
    assert gitlab_client.token_scopes() == []


def test_clickup_review_creation_uses_one_snapshot_for_list_assignee_and_request(
    monkeypatch,
):
    generations = []

    def changing_config():
        generation = len(generations) + 1
        generations.append(generation)
        return {
            "token": f"token-{generation}",
            "list_ids": [f"list-{generation}"],
            "create_list_id": f"list-{generation}",
        }

    requests_seen = []

    def get(url, *, headers, timeout):
        requests_seen.append(("get", url, headers, None))
        return _Response({"user": {"id": "runtime-user"}})

    def post(url, *, headers, json, timeout):
        requests_seen.append(("post", url, headers, json))
        return _Response({"id": "created-task"})

    monkeypatch.setattr(clickup_client, "clickup_config", changing_config)
    monkeypatch.setattr(clickup_client.requests, "get", get)
    monkeypatch.setattr(clickup_client.requests, "post", post)
    monkeypatch.setattr(clickup_client, "_user_id_cache", None)
    monkeypatch.setattr(clickup_client, "_user_id_cache_key", None)

    created = clickup_client.create_review_task(
        {"draft": {"name": "Runtime task"}}
    )

    assert created["id"] == "created-task"
    assert generations == [1]
    assert requests_seen == [
        (
            "get",
            "https://api.clickup.com/api/v2/user",
            {"Authorization": "token-1", "Content-Type": "application/json"},
            None,
        ),
        (
            "post",
            "https://api.clickup.com/api/v2/list/list-1/task",
            {"Authorization": "token-1", "Content-Type": "application/json"},
            {
                "name": "Runtime task",
                "description": "",
                "assignees": ["runtime-user"],
            },
        ),
    ]


def test_focused_review_creation_never_links_related_task_automatically(monkeypatch):
    config = {
        "token": "runtime-token",
        "list_ids": ["review-list"],
        "create_list_id": "review-list",
    }
    requests_seen = []

    def post(url, *, headers, json=None, timeout):
        requests_seen.append((url, json))
        return _Response({"id": "created-task"})

    monkeypatch.setattr(clickup_client, "current_user_id", lambda _config: "reviewer")
    monkeypatch.setattr(clickup_client.requests, "post", post)

    created = clickup_client.create_review_task(
        {
            "draft": {"name": "Review - Runtime task"},
            "related_task_id": "related-task",
        },
        config=config,
    )

    assert created["id"] == "created-task"
    assert requests_seen == [
        (
            "https://api.clickup.com/api/v2/list/review-list/task",
            {
                "name": "Review - Runtime task",
                "description": "",
                "assignees": ["reviewer"],
            },
        )
    ]


def test_approved_review_creation_may_link_the_created_task(monkeypatch):
    calls = []
    monkeypatch.setattr(clickup_client, "_require_config", lambda: {"token": "token"})
    monkeypatch.setattr(
        clickup_client,
        "create_review_task",
        lambda payload, *, config: calls.append(("create", payload, config))
        or {"id": "created-task"},
    )
    monkeypatch.setattr(
        clickup_client,
        "link_tasks",
        lambda task_id, related_id, config: calls.append(
            ("link", task_id, related_id, config)
        )
        or {},
    )
    payload = {
        "draft": {"name": "Review - Approved task"},
        "related_task_id": "related-task",
    }

    created = clickup_client.execute_action("clickup_create_task", None, payload)

    assert created == {"id": "created-task"}
    assert calls == [
        ("create", payload, {"token": "token"}),
        ("link", "created-task", "related-task", {"token": "token"}),
    ]


def test_clickup_unconfigured_boundaries_issue_zero_requests(conn, monkeypatch):
    blank = {"token": "", "list_ids": [], "create_list_id": ""}
    monkeypatch.setattr(clickup_client, "clickup_config", lambda: blank)
    monkeypatch.setattr(clickup_client, "_user_id_cache", None)
    monkeypatch.setattr(clickup_client, "_user_id_cache_key", None)

    def unexpected(*args, **kwargs):
        pytest.fail("unconfigured ClickUp issued a network request")

    monkeypatch.setattr(clickup_client.requests, "get", unexpected)
    monkeypatch.setattr(clickup_client.requests, "post", unexpected)
    monkeypatch.setattr(clickup_client.requests, "put", unexpected)

    for operation in (
        lambda: clickup_client.get_task("task-1"),
        clickup_client.fetch_tasks,
        clickup_client.current_user_id,
        lambda: clickup_client._closed_status_name("list-1"),
        lambda: clickup_client.close_task("task-1"),
        lambda: clickup_client.link_tasks("task-1", "task-2"),
        lambda: clickup_client.execute_action(
            "clickup_comment", "task-1", {"draft": "hello"}
        ),
        lambda: clickup_client.sync(conn),
    ):
        with pytest.raises(RuntimeError, match="ClickUp is not configured"):
            operation()

    assert clickup_client.refresh_tasks(conn, ["task-1"]) == 0


def test_flock_sync_uses_one_snapshot_for_worker_and_chat_filter(conn, monkeypatch):
    generations = []

    def changing_config():
        generation = len(generations) + 1
        generations.append(generation)
        return {
            "disabled": False,
            "profile_path": f"/runtime/profile-{generation}",
            "user_handle": f"Runtime User {generation}",
            "url": f"https://flock-{generation}.example.com/",
            "lookback_hours": 24 if generation == 1 else 0,
        }

    worker = {}

    def fetch_inbox(profile, url):
        worker.update(profile=profile, url=url)
        return [
            {
                "jid": "runtime@go.to",
                "name": "Runtime",
                "type": "buddy",
                "isGroup": False,
                "hasMention": False,
                "unreadCount": 1,
                "lastMessageTime": int((time.time() - 60) * 1000),
                "isMuted": False,
                "notifyOn": "",
                "bucket": "open",
            }
        ]

    monkeypatch.setattr(flock_client, "flock_config", changing_config)
    monkeypatch.setattr(flock_client, "_fetch_inbox", fetch_inbox)

    assert flock_client.sync(conn) == 1
    assert generations == [1]
    assert worker == {
        "profile": "/runtime/profile-1",
        "url": "https://flock-1.example.com/",
    }


def test_flock_webhook_receive_uses_current_handle_snapshot(conn, monkeypatch):
    flock_webhook_client.register_channel(conn, "runtime-token", "Runtime")
    monkeypatch.setattr(settings, "flock_user_handle", "Stale User")
    calls = []

    def current_config():
        calls.append(1)
        return {
            "disabled": False,
            "profile_path": "unused",
            "user_handle": "Runtime User",
            "url": "https://web.flock.com/",
            "lookback_hours": 24,
        }

    monkeypatch.setattr(flock_client, "flock_config", current_config)

    result = flock_webhook_client.receive(
        conn,
        "runtime-token",
        _flock_payload("hello @Runtime User"),
    )

    assert result["matched"] is True
    assert calls == [1]


def test_flock_webhook_receive_ignores_messages_while_disabled(conn, monkeypatch):
    flock_webhook_client.register_channel(conn, "runtime-token", "Runtime")
    monkeypatch.setattr(
        flock_client,
        "flock_config",
        lambda: {
            "disabled": True,
            "profile_path": "",
            "user_handle": "Runtime User",
            "url": "",
            "lookback_hours": 24,
        },
    )

    result = flock_webhook_client.receive(
        conn,
        "runtime-token",
        _flock_payload("@all should not land"),
    )

    assert result == {"ok": False, "reason": "disabled"}
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM flock_webhook_mentions"
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT last_seen_at FROM flock_webhook_channels"
    ).fetchone()["last_seen_at"] is None


def test_gcal_approved_write_uses_one_snapshot_through_refresh_and_service(
    monkeypatch,
):
    from google.oauth2 import credentials as google_credentials
    from googleapiclient import discovery

    generations = []

    def changing_config():
        generation = len(generations) + 1
        generations.append(generation)
        return {
            "client_config": f"client-{generation}",
            "authorized_user": json.dumps(
                {
                    "generation": generation,
                    "refresh_token": f"refresh-{generation}",
                }
            ),
        }

    persisted = []
    built_with = []
    inserted = []

    class Credentials:
        expired = True

        def __init__(self, info):
            self.generation = info["generation"]
            self.refresh_token = info["refresh_token"]
            self.refreshed = False

        def refresh(self, request):
            self.refreshed = True

        def to_json(self):
            return json.dumps(
                {"generation": self.generation, "refreshed": self.refreshed}
            )

    class Insert:
        def execute(self):
            return {"id": "runtime-event"}

    class Events:
        def insert(self, **kwargs):
            inserted.append(kwargs)
            return Insert()

    class Service:
        def events(self):
            return Events()

    monkeypatch.setattr(gcal_client, "gcal_config", changing_config)
    monkeypatch.setattr(
        google_credentials.Credentials,
        "from_authorized_user_info",
        lambda info, scopes: Credentials(info),
    )
    monkeypatch.setattr(
        discovery,
        "build",
        lambda service, version, *, credentials, cache_discovery: (
            built_with.append(credentials.generation) or Service()
        ),
    )
    monkeypatch.setattr(
        gcal_client.secret_store,
        "set_secret",
        lambda name, value: persisted.append((name, json.loads(value))),
    )

    result = gcal_client.execute_action(
        "gcal_create_event",
        None,
        {
            "draft": {
                "title": "Runtime review",
                "start_at": "2026-08-28T15:00:00+05:30",
                "end_at": "2026-08-28T15:30:00+05:30",
                "add_meet": False,
            }
        },
    )

    assert result == {"id": "runtime-event"}
    assert generations == [1]
    assert built_with == [1]
    assert persisted == [
        ("google.authorized_user", {"generation": 1, "refreshed": True})
    ]
    assert inserted[0]["body"]["summary"] == "Runtime review"


def test_representative_connector_config_never_reaches_security_subprocess(
    monkeypatch,
):
    calls = []
    original_run = secret_store.subprocess.run

    def guarded_run(command, **kwargs):
        if command and command[0] == secret_store.SECURITY:
            calls.append(command)
            return SimpleNamespace(returncode=44, stdout="", stderr="not found")
        return original_run(command, **kwargs)

    monkeypatch.setattr(secret_store.subprocess, "run", guarded_run)

    gitlab_client.gitlab_config()
    clickup_client.clickup_config()
    gcal_client.gcal_config()
    classifier.anthropic_config()

    assert calls == []


def _flock_payload(text):
    return {
        "id": "runtime-message",
        "from": "runtime-sender@go.to/resource",
        "to": "runtime-channel@groups.go.to",
        "text": text,
    }


def test_gitlab_operations_resolve_runtime_config_on_each_call(monkeypatch):
    runtime = {
        "integration.gitlab.base_url": "https://first.gitlab.example.com/api/v4",
        "integration.gitlab.username": "first-user",
    }
    token = {"value": "first-token"}
    monkeypatch.setattr(
        gitlab_client.app_settings,
        "effective_nonsecret",
        _runtime_nonsecrets(runtime),
    )
    monkeypatch.setattr(
        gitlab_client.app_settings, "integration_disabled", lambda source: False
    )
    monkeypatch.setattr(
        gitlab_client.secret_store,
        "effective_secret",
        lambda name, fallback="": token["value"],
    )

    assert gitlab_client.configured() is True
    assert gitlab_client._api("/user") == (
        "https://first.gitlab.example.com/api/v4/user"
    )
    assert gitlab_client._headers() == {"PRIVATE-TOKEN": "first-token"}
    assert gitlab_client.current_username() == "first-user"

    runtime["integration.gitlab.base_url"] = "https://second.gitlab.example.com"
    runtime["integration.gitlab.username"] = "second-user"
    token["value"] = "second-token"

    assert gitlab_client._api("/user") == (
        "https://second.gitlab.example.com/api/v4/user"
    )
    assert gitlab_client._headers() == {"PRIVATE-TOKEN": "second-token"}
    assert gitlab_client.current_username() == "second-user"


def test_gitlab_disabled_or_keychain_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "gitlab_base_url", "https://stale.example.com")
    monkeypatch.setattr(settings, "gitlab_token", "stale-token")
    monkeypatch.setattr(
        gitlab_client.app_settings, "integration_disabled", lambda source: True
    )
    monkeypatch.setattr(
        gitlab_client.secret_store,
        "effective_secret",
        lambda name, fallback="": (_ for _ in ()).throw(
            AssertionError("disabled connector must not read Keychain")
        ),
    )
    assert gitlab_client.configured() is False

    monkeypatch.setattr(
        gitlab_client.app_settings, "integration_disabled", lambda source: False
    )

    def unavailable(name, fallback=""):
        raise secret_store.SecretStoreError("redacted")

    monkeypatch.setattr(gitlab_client.secret_store, "effective_secret", unavailable)
    assert gitlab_client.configured() is False
    monkeypatch.setattr(
        gitlab_client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail(
            "GitLab requested after Keychain failure"
        ),
    )
    with pytest.raises(RuntimeError, match="GitLab is not configured"):
        gitlab_client.current_user()


def test_clickup_operations_resolve_keychain_and_ui_list_on_each_call(monkeypatch):
    runtime = {"integration.clickup.create_list_id": "first-list"}
    token = {"value": "first-token"}
    monkeypatch.setattr(
        clickup_client.app_settings,
        "effective_nonsecret",
        _runtime_nonsecrets(runtime),
    )
    monkeypatch.setattr(
        clickup_client.app_settings, "integration_disabled", lambda source: False
    )
    monkeypatch.setattr(
        clickup_client.secret_store,
        "effective_secret",
        lambda name, fallback="": token["value"],
    )

    assert clickup_client.configured() is True
    assert clickup_client._headers()["Authorization"] == "first-token"
    assert clickup_client.clickup_config()["list_ids"] == ("first-list",)

    runtime["integration.clickup.create_list_id"] = "second-list"
    token["value"] = "second-token"

    assert clickup_client._headers()["Authorization"] == "second-token"
    assert clickup_client.clickup_config()["list_ids"] == ("second-list",)


def test_clickup_configured_uses_current_keychain_token_presence(monkeypatch):
    monkeypatch.setattr(
        clickup_client.app_settings, "integration_disabled", lambda source: False
    )
    monkeypatch.setattr(
        clickup_client.app_settings,
        "effective_nonsecret",
        lambda key, fallback: fallback,
    )
    monkeypatch.setattr(
        clickup_client.secret_store,
        "effective_secret",
        lambda name, fallback="": "pk_ui",
    )
    monkeypatch.setattr(settings, "clickup_list_ids", "")
    monkeypatch.setattr(settings, "clickup_create_list_id", "")

    assert clickup_client.configured() is True


def test_clickup_disabled_or_keychain_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "clickup_api_token", "stale-token")
    monkeypatch.setattr(settings, "clickup_list_ids", "stale-list")
    monkeypatch.setattr(
        clickup_client.app_settings, "integration_disabled", lambda source: True
    )
    assert clickup_client.configured() is False

    monkeypatch.setattr(
        clickup_client.app_settings, "integration_disabled", lambda source: False
    )

    def unavailable(name, fallback=""):
        raise secret_store.SecretStoreError("redacted")

    monkeypatch.setattr(clickup_client.secret_store, "effective_secret", unavailable)
    assert clickup_client.configured() is False
    monkeypatch.setattr(
        clickup_client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail(
            "ClickUp requested after Keychain failure"
        ),
    )
    with pytest.raises(RuntimeError, match="ClickUp is not configured"):
        clickup_client.get_task("task-1")


def test_gcal_runtime_config_is_keychain_only_and_fails_closed(monkeypatch):
    secrets = {
        "google.client_config": "client-json",
        "google.authorized_user": "authorized-user-json",
    }
    monkeypatch.setattr(
        gcal_client.app_settings, "integration_disabled", lambda source: False
    )
    monkeypatch.setattr(
        gcal_client.secret_store,
        "get_secret",
        lambda name: secrets.get(name, ""),
    )

    assert gcal_client.gcal_config() == {
        "client_config": "client-json",
        "authorized_user": "authorized-user-json",
    }
    assert gcal_client.configured() is True

    secrets.clear()
    assert gcal_client.configured() is False

    def unavailable(name):
        raise secret_store.SecretStoreError("redacted")

    monkeypatch.setattr(gcal_client.secret_store, "get_secret", unavailable)
    assert gcal_client.configured() is False


def test_flock_operations_resolve_runtime_profile_and_handle(monkeypatch, tmp_path):
    first = tmp_path / "first-profile"
    second = tmp_path / "second-profile"
    runtime = {
        "integration.flock.profile_dir": str(first),
        "integration.flock.user_handle": "First User",
    }
    probed = []
    monkeypatch.setattr(
        flock_client.app_settings,
        "effective_nonsecret",
        _runtime_nonsecrets(runtime),
    )
    monkeypatch.setattr(
        flock_client.app_settings, "integration_disabled", lambda source: False
    )
    monkeypatch.setattr(
        flock_client,
        "probe_authenticated_session",
        lambda profile, timeout_seconds: probed.append(profile) or True,
    )

    assert flock_client.configured() is True
    assert flock_client.flock_config()["user_handle"] == "First User"
    assert probed[-1] == first

    runtime["integration.flock.profile_dir"] = str(second)
    runtime["integration.flock.user_handle"] = "Second User"

    assert flock_client.configured() is True
    assert flock_client.flock_config()["user_handle"] == "Second User"
    assert probed[-1] == second


def test_flock_disabled_tombstone_prevents_direct_sync(conn, monkeypatch):
    monkeypatch.setattr(
        flock_client.app_settings, "integration_disabled", lambda source: True
    )
    monkeypatch.setattr(
        flock_client,
        "_fetch_inbox",
        lambda *args, **kwargs: pytest.fail("disabled Flock launched its worker"),
    )

    with pytest.raises(RuntimeError, match="Flock is not configured"):
        flock_client.sync(conn)


def test_anthropic_clients_resolve_runtime_config_on_each_call(monkeypatch):
    runtime = {
        "integration.anthropic.base_url": "https://first.gateway.example.com",
        "integration.anthropic.model": "first-model",
    }
    key = {"value": "first-key"}
    constructor_calls = []
    create_calls = []

    class Messages:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(text="answer")])

    def anthropic_client(**kwargs):
        constructor_calls.append(kwargs)
        return SimpleNamespace(messages=Messages())

    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=anthropic_client)
    )
    monkeypatch.setattr(
        classifier.app_settings,
        "effective_nonsecret",
        _runtime_nonsecrets(runtime),
    )
    monkeypatch.setattr(
        classifier.app_settings, "integration_disabled", lambda source: False
    )
    monkeypatch.setattr(
        classifier.secret_store,
        "effective_secret",
        lambda name, fallback="": key["value"],
    )
    monkeypatch.setattr(asker, "build_prompt", lambda conn, question: "prompt")

    assert classifier.call_llm("classify") == "answer"

    runtime["integration.anthropic.base_url"] = "https://second.gateway.example.com"
    runtime["integration.anthropic.model"] = "second-model"
    key["value"] = "second-key"

    assert asker.answer_question(None, "question") == {
        "answer": "answer",
        "entry_ids": [],
    }
    assert constructor_calls == [
        {
            "api_key": "first-key",
            "base_url": "https://first.gateway.example.com",
        },
        {
            "api_key": "second-key",
            "base_url": "https://second.gateway.example.com",
        },
    ]
    assert [call["model"] for call in create_calls] == [
        "first-model",
        "second-model",
    ]


def test_anthropic_disabled_or_keychain_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "stale-key")
    monkeypatch.setattr(
        classifier.app_settings, "integration_disabled", lambda source: True
    )
    assert classifier.anthropic_config()["api_key"] == ""

    monkeypatch.setattr(
        classifier.app_settings, "integration_disabled", lambda source: False
    )

    def unavailable(name, fallback=""):
        raise secret_store.SecretStoreError("redacted")

    monkeypatch.setattr(classifier.secret_store, "effective_secret", unavailable)
    assert classifier.anthropic_config()["api_key"] == ""
