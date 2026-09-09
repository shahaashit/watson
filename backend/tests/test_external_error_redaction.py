import subprocess
from types import SimpleNamespace

import requests

from app.config import settings
from app.services import clickup_client, flock_client, work


def test_clickup_recoverable_fetch_error_never_logs_exception_text(
    conn, monkeypatch, caplog
):
    secret = "clickup-recoverable-credential-sentinel"
    monkeypatch.setattr(settings, "clickup_api_token", "configured")
    monkeypatch.setattr(settings, "clickup_list_ids", "list-1")
    monkeypatch.setattr(
        clickup_client,
        "get_task",
        lambda _task_id, config=None: (_ for _ in ()).throw(
            requests.RequestException(secret)
        ),
    )

    assert clickup_client.refresh_tasks(conn, ["task-1"]) == 0
    assert secret not in caplog.text


def test_backfill_recoverable_identity_error_is_generic_in_sync_summary(
    conn, monkeypatch
):
    secret = "clickup-identity-credential-sentinel"
    monkeypatch.setattr(settings, "clickup_create_list_id", "list-1")
    monkeypatch.setattr(
        clickup_client,
        "current_user_id",
        lambda: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    result = work.backfill_managed_tasks(conn)

    assert result == {
        "added": 0,
        "skipped": 0,
        "reason": "ClickUp request failed. Check the integration and try again.",
    }
    assert secret not in str(result)


def test_flock_worker_diagnostics_never_log_captured_third_party_output(
    tmp_path, monkeypatch, caplog
):
    secret = "flock-worker-credential-sentinel"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr=secret
        ),
    )

    assert flock_client._fetch_inbox(tmp_path, "https://web.flock.com/") is None
    assert secret not in caplog.text
