import json
import sqlite3
import threading
from datetime import date
from datetime import datetime
from types import MappingProxyType

import pytest

from app.services import exporter, sync_pipeline, user_meta


@pytest.fixture(autouse=True)
def exercise_retained_connector_concurrency(monkeypatch):
    # Keep coverage of the withheld connector's locks for eventual release.
    monkeypatch.setattr(sync_pipeline, "FLOCK_RELEASED", True)


def test_withheld_connector_never_runs_in_normal_sync(conn, monkeypatch):
    monkeypatch.setattr(sync_pipeline, "FLOCK_RELEASED", False)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.flock_client, "sync",
                        lambda _conn: pytest.fail("unreleased connector ran"))
    _quiet_downstream(monkeypatch)
    assert "flock_sync" not in sync_pipeline.run_sync_pipeline(conn)


def _quiet_downstream(monkeypatch):
    monkeypatch.setattr(
        sync_pipeline.settings_service,
        "profile",
        lambda _conn: {
            "auto_create_review_tasks": True,
            "ai_group_review_mrs": False,
        },
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "ingest_cached_mrs",
        lambda _conn: {"created": 0, "updated": 0},
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion, "linked_clickup_ids", lambda _conn: []
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion, "reconcile_clickup_titles", lambda _conn: 0
    )
    monkeypatch.setattr(
        sync_pipeline.work, "backfill_managed_tasks", lambda _conn: {"added": 0}
    )
    monkeypatch.setattr(
        sync_pipeline.work, "backfill_related_clickup_ids", lambda _conn: 0
    )
    monkeypatch.setattr(
        sync_pipeline.completion, "check_completions", lambda _conn: 0
    )
    monkeypatch.setattr(
        sync_pipeline.orphan_detector, "detect_orphans", lambda _conn: 0
    )
    monkeypatch.setattr(
        sync_pipeline.review_automation,
        "run",
        lambda _conn, **_kwargs: {
            "exact_grouped": 0,
            "ai_grouped": 0,
            "attached": 0,
            "created": 0,
            "deferred": 0,
            "ambiguous": 0,
            "failed": 0,
            "uncertain": 0,
        },
    )
    monkeypatch.setattr(
        sync_pipeline.link_proposer, "propose_mr_task_links", lambda _conn: 0
    )


def test_independent_refreshers_overlap_before_ingestion(conn, monkeypatch):
    """A sequential regression makes at least one refresher time out before
    all three have started; the downstream stage must see all three finished."""
    started = set()
    finished = set()
    state_lock = threading.Lock()
    all_started = threading.Event()

    def refresher(name):
        def run(_conn):
            with state_lock:
                started.add(name)
                if len(started) == 3:
                    all_started.set()
            assert all_started.wait(1), "independent refreshers did not overlap"
            with state_lock:
                finished.add(name)
            return 1

        return run

    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "sync", refresher("gitlab"))
    monkeypatch.setattr(sync_pipeline.gcal_client, "sync", refresher("google-calendar"))
    monkeypatch.setattr(sync_pipeline.flock_client, "sync", refresher("flock"))
    _quiet_downstream(monkeypatch)

    def ingest(_conn):
        assert finished == {"gitlab", "google-calendar", "flock"}
        return {"created": 0, "updated": 0}

    monkeypatch.setattr(sync_pipeline.work_ingestion, "ingest_cached_mrs", ingest)

    summary = sync_pipeline.run_sync_pipeline(conn)

    assert summary["gitlab_sync"] == 1
    assert summary["gcal_sync"] == 1
    assert summary["flock_sync"] == 1


def test_one_configuration_probe_failure_does_not_abort_other_sources(
    conn, monkeypatch
):
    calls = []
    secret = "fake-gitlab-config-token"
    monkeypatch.setattr(
        sync_pipeline.gitlab_client,
        "configured",
        lambda: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: False)
    monkeypatch.setattr(
        sync_pipeline.gcal_client, "sync", lambda _conn: calls.append("gcal") or 1
    )
    _quiet_downstream(monkeypatch)

    summary = sync_pipeline.run_sync_pipeline(conn)

    assert calls == ["gcal"]
    assert summary["gitlab_sync"] == {
        "source": "gitlab",
        "error": "GitLab request failed. Check the integration and try again.",
    }
    stored = " ".join(row["value"] or "" for row in conn.execute("SELECT value FROM user_meta"))
    assert secret not in stored


def test_flock_fetch_failure_preserves_today_cache_and_marks_health_degraded(
    conn, monkeypatch
):
    conn.execute(
        "INSERT INTO flock_mentions_cache (jid, name, synced_at) VALUES (?, ?, ?)",
        ("cached@go.to", "Cached conversation", "2026-08-27T09:30:00"),
    )
    conn.commit()
    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.flock_client, "_fetch_inbox", lambda *_args: None)
    _quiet_downstream(monkeypatch)

    summary = sync_pipeline.run_sync_pipeline(conn)

    assert summary["flock_sync"] == {
        "source": "flock",
        "error": "Flock request failed. Reconnect the integration and try again.",
    }
    assert conn.execute(
        "SELECT name FROM flock_mentions_cache WHERE jid='cached@go.to'"
    ).fetchone()["name"] == "Cached conversation"
    flock = next(
        item for item in user_meta.integration_health(conn) if item["source"] == "flock"
    )
    assert flock["status"] == "degraded"
    audit = conn.execute(
        "SELECT subject, details_json FROM system_events WHERE kind='sync' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "error_type" not in audit["subject"]
    assert "error_type" not in audit["details_json"]


def test_downstream_failure_stays_isolated_and_public_audit_is_generic(
    conn, monkeypatch, caplog
):
    secret = "pk_proposer_secret_sentinel"
    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "sync", lambda _conn: 1)
    _quiet_downstream(monkeypatch)
    calls = []
    monkeypatch.setattr(
        sync_pipeline.review_automation,
        "run",
        lambda _conn, **_kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    monkeypatch.setattr(
        sync_pipeline.link_proposer,
        "propose_mr_task_links",
        lambda _conn: calls.append("links") or 0,
    )

    summary = sync_pipeline.run_sync_pipeline(conn)

    assert calls == ["links"]
    assert summary["review_automation"] == {
        "source": "review-automation",
        "error": "External service request failed. Check the integration and try again.",
    }
    audit = conn.execute(
        "SELECT subject, details_json FROM system_events WHERE kind='sync' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert secret not in audit["subject"]
    assert secret not in audit["details_json"]
    assert "error_type" not in audit["subject"]
    assert "error_type" not in audit["details_json"]
    assert secret not in caplog.text


def test_pipeline_uses_strict_ingest_exact_refresh_title_order(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: False)
    monkeypatch.setattr(
        sync_pipeline.gitlab_client,
        "sync",
        lambda _conn: calls.append("gitlab") or 2,
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "ingest_cached_mrs",
        lambda _conn: calls.append("ingest") or {"created": 1, "updated": 0},
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "linked_clickup_ids",
        lambda _conn: calls.append("ids") or ["86abc1234"],
    )
    monkeypatch.setattr(
        sync_pipeline.clickup_client,
        "refresh_exact_tasks",
        lambda _conn, ids: calls.append(("clickup", tuple(ids)))
        or {"updated": 1, "failed": 0, "skipped": 0},
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "reconcile_clickup_titles",
        lambda _conn: calls.append("titles") or 1,
    )
    monkeypatch.setattr(
        sync_pipeline.settings_service,
        "profile",
        lambda _conn: {
            "auto_create_review_tasks": True,
            "ai_group_review_mrs": False,
        },
    )
    monkeypatch.setattr(
        sync_pipeline.review_automation,
        "run",
        lambda _conn, **_kwargs: calls.append("review") or {"created": 0},
    )
    monkeypatch.setattr(
        sync_pipeline.clickup_client,
        "sync",
        lambda _conn: pytest.fail("scheduled sync must not scan ClickUp lists"),
    )
    monkeypatch.setattr(
        sync_pipeline.work, "backfill_managed_tasks", lambda _conn: calls.append("backfill") or {"added": 0}
    )
    monkeypatch.setattr(
        sync_pipeline.work, "backfill_related_clickup_ids", lambda _conn: calls.append("relink") or 0
    )
    monkeypatch.setattr(
        sync_pipeline.completion, "check_completions", lambda _conn: calls.append("completion") or 0
    )
    monkeypatch.setattr(
        sync_pipeline.orphan_detector, "detect_orphans", lambda _conn: calls.append("orphan") or 0
    )
    monkeypatch.setattr(
        sync_pipeline.link_proposer, "propose_mr_task_links", lambda _conn: calls.append("links") or 0
    )

    sync_pipeline.run_sync_pipeline(conn)

    assert calls == [
        "gitlab",
        "ingest",
        "ids",
        ("clickup", ("86abc1234",)),
        "titles",
        "review",
        "backfill",
        "relink",
        "completion",
        "orphan",
        "links",
    ]


def test_sync_runs_review_automation_once_after_ingestion_with_one_profile_snapshot(
    conn, monkeypatch
):
    calls = []
    profile = {"auto_create_review_tasks": True, "ai_group_review_mrs": True}
    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: False)
    monkeypatch.setattr(
        sync_pipeline,
        "settings_service",
        type("Settings", (), {"profile": staticmethod(lambda _c: calls.append("profile") or profile)}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_pipeline,
        "classifier",
        type("Classifier", (), {"anthropic_config": staticmethod(lambda: {"api_key": "test-key"})}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_pipeline,
        "review_automation",
        type("ReviewAutomation", (), {
            "run": staticmethod(
                lambda _c, **kwargs: calls.append(("review", kwargs["profile"]))
                or {"created": 0}
            )
        }),
        raising=False,
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "ingest_cached_mrs",
        lambda _c: calls.append("ingest") or {},
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "reconcile_clickup_titles",
        lambda _c: calls.append("reconcile") or 0,
    )

    summary = sync_pipeline.run_sync_pipeline(conn)

    assert calls.count("profile") == 1
    assert calls.index("ingest") < calls.index("reconcile")
    assert calls.index("reconcile") < calls.index(("review", profile))
    assert summary["review_automation"] == {"created": 0}


def test_anthropic_is_conditional_nonrefreshing_capability_gate(conn, monkeypatch):
    calls = []
    profile = {"auto_create_review_tasks": True, "ai_group_review_mrs": False}
    assert "anthropic" not in user_meta.INTEGRATION_SOURCES
    for client in (
        sync_pipeline.clickup_client,
        sync_pipeline.gitlab_client,
        sync_pipeline.gcal_client,
        sync_pipeline.flock_client,
    ):
        monkeypatch.setattr(client, "configured", lambda: False)
    monkeypatch.setattr(
        sync_pipeline,
        "settings_service",
        type("Settings", (), {"profile": staticmethod(lambda _c: profile)}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_pipeline,
        "classifier",
        type("Classifier", (), {
            "anthropic_config": staticmethod(lambda: calls.append("anthropic") or {"api_key": "test-key"})
        }),
        raising=False,
    )
    monkeypatch.setattr(
        sync_pipeline,
        "review_automation",
        type("ReviewAutomation", (), {
            "run": staticmethod(lambda _c, **kwargs: calls.append(("review", kwargs["ai_ready"])) or {})
        }),
        raising=False,
    )

    summary = sync_pipeline.run_sync_pipeline(conn)

    assert "anthropic" not in calls
    assert ("review", False) in calls
    assert "review_grouping_ready" not in summary


def test_review_automation_health_and_audit_store_only_safe_nested_counts(
    client, conn, monkeypatch, caplog
):
    secret = "fake-anthropic-review-secret"
    result = {
        "exact_grouped": 1,
        "ai_grouped": 0,
        "attached": 0,
        "created": 0,
        "deferred": 1,
        "ambiguous": 2,
        "failed": 0,
        "uncertain": 1,
        "status": f"deferred {secret}",
    }
    for integration_client in (
        sync_pipeline.clickup_client,
        sync_pipeline.gitlab_client,
        sync_pipeline.gcal_client,
        sync_pipeline.flock_client,
    ):
        monkeypatch.setattr(integration_client, "configured", lambda: False)
    monkeypatch.setattr(
        sync_pipeline,
        "settings_service",
        type("Settings", (), {"profile": staticmethod(lambda _c: {
            "auto_create_review_tasks": True,
            "ai_group_review_mrs": False,
        })}),
        raising=False,
    )
    monkeypatch.setattr(
        sync_pipeline,
        "review_automation",
        type("ReviewAutomation", (), {"run": staticmethod(lambda _c, **_kwargs: result)}),
        raising=False,
    )

    summary = sync_pipeline.run_sync_pipeline(conn)
    status = client.get("/api/sync/status")
    stored = " ".join(row["value"] or "" for row in conn.execute("SELECT value FROM user_meta"))
    audit = conn.execute(
        "SELECT subject, details_json FROM system_events WHERE kind='sync' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert summary["review_automation"]["status"] == "deferred"
    review_health = next(
        item for item in status.json()["sources"] if item["source"] == "review-automation"
    )
    assert review_health["status"] == "degraded"
    assert review_health["counts"] == {
        "exact_grouped": 1,
        "ai_grouped": 0,
        "attached": 0,
        "created": 0,
        "deferred": 1,
        "ambiguous": 2,
        "failed": 0,
        "uncertain": 1,
    }
    assert secret not in status.text
    assert secret not in stored
    assert secret not in audit["subject"]
    assert secret not in audit["details_json"]
    assert secret not in caplog.text
    assert "review automation" in audit["subject"].lower()


def test_health_api_and_today_hide_raw_technical_error(client, conn, monkeypatch):
    secret = "Authorization: Bearer pk_secret_health_sentinel"
    monkeypatch.setattr(
        user_meta, "_now", lambda: datetime(2026, 8, 27, 10, 5, 0)
    )
    user_meta.set_integration_health(
        conn,
        "clickup",
        {
            "status": "degraded",
            "last_success_at": "2026-08-27T10:00:00",
            "retry_at": "2026-08-27T10:02:00",
            "message": "Timed out while refreshing linked tasks",
            "technical_error": secret,
        },
    )

    status = client.get("/api/sync/status")
    today = client.get("/api/today")
    stored = " ".join(
        row["value"] or ""
        for row in conn.execute(
            "SELECT value FROM user_meta WHERE key LIKE 'integration_health:%'"
        )
    )

    assert status.status_code == 200
    assert today.status_code == 200
    assert secret not in status.text
    assert secret not in today.text
    assert secret not in stored
    clickup = next(
        item for item in status.json()["sources"] if item["source"] == "clickup"
    )
    assert clickup == {
        "source": "clickup",
        "status": "degraded",
        "last_success_at": "2026-08-27T10:00:00",
        "cached_age_seconds": 300,
        "retry_at": "2026-08-27T10:02:00",
        "message": "Timed out while refreshing linked tasks",
    }
    assert today.json()["integration_health"] == status.json()["sources"]


def test_health_technical_metadata_rejects_arbitrary_labels(conn):
    sentinel = "pk_secret_arbitrary_technical_label"

    user_meta.set_integration_health(
        conn,
        "gitlab",
        {
            "status": "degraded",
            "message": "GitLab request failed. Check the integration and try again.",
            "technical": {
                "error_type": f"RuntimeError_{sentinel}",
                "operation": f"gitlab_sync_{sentinel}",
            },
        },
    )

    row = conn.execute(
        "SELECT value FROM user_meta WHERE key=?",
        ("integration_health_technical:gitlab",),
    ).fetchone()
    assert row is None


def test_exception_class_tokens_never_reach_db_audit_log_or_backup(
    conn, monkeypatch, caplog
):
    sentinel = "pk_secret_exception_class_sentinel"
    CredentialNamedError = type(
        f"RuntimeError_{sentinel}",
        (RuntimeError,),
        {},
    )
    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.gcal_client, "configured", lambda: False)
    monkeypatch.setattr(sync_pipeline.flock_client, "configured", lambda: False)
    monkeypatch.setattr(
        sync_pipeline.gitlab_client,
        "sync",
        lambda _conn: (_ for _ in ()).throw(CredentialNamedError("redacted")),
    )
    _quiet_downstream(monkeypatch)

    summary = sync_pipeline.run_sync_pipeline(conn)
    backup_path = exporter.backup_db(conn, date(2099, 1, 17))
    backup_conn = sqlite3.connect(backup_path)
    try:
        backup_text = " ".join(
            str(value or "")
            for table, column in (
                ("user_meta", "value"),
                ("system_events", "details_json"),
                ("system_events", "subject"),
            )
            for (value,) in backup_conn.execute(f"SELECT {column} FROM {table}")
        )
    finally:
        backup_conn.close()

    live_text = " ".join(
        row["value"] or "" for row in conn.execute("SELECT value FROM user_meta")
    ) + " " + " ".join(
        (row["subject"] or "") + " " + (row["details_json"] or "")
        for row in conn.execute("SELECT subject, details_json FROM system_events")
    )
    technical = json.loads(
        conn.execute(
            "SELECT value FROM user_meta WHERE key=?",
            ("integration_health_technical:gitlab",),
        ).fetchone()["value"]
    )

    assert summary["gitlab_sync"] == {
        "source": "gitlab",
        "error": "GitLab request failed. Check the integration and try again.",
    }
    assert technical == {
        "error_type": "external",
        "operation": "gitlab_sync",
    }
    assert sentinel not in live_text
    assert sentinel not in backup_text
    assert sentinel not in caplog.text


def test_health_cached_age_is_derived_from_preserved_cache(conn, monkeypatch):
    conn.execute(
        "INSERT INTO clickup_tasks_cache (task_id, name, synced_at) VALUES (?, ?, ?)",
        ("86abc1234", "Cached linked task", "2026-08-27T09:30:00"),
    )
    conn.commit()
    user_meta.set_integration_health(
        conn,
        "clickup",
        {
            "status": "degraded",
            "last_success_at": "2026-08-27T09:30:00",
            "retry_at": "",
            "message": "ClickUp request failed. Check the integration and try again.",
        },
    )
    monkeypatch.setattr(user_meta, "_now", lambda: datetime(2026, 8, 27, 10, 0, 0))

    clickup = next(
        item for item in user_meta.integration_health(conn) if item["source"] == "clickup"
    )

    assert clickup["cached_age_seconds"] == 1800


def test_concurrent_degraded_health_preserves_just_recorded_success(
    conn, monkeypatch
):
    """The degraded writer starts first but commits last. Its merge must occur
    under the health lock so it sees the intervening successful timestamp."""
    from app import db

    degraded_lock_started = threading.Event()
    healthy_written = threading.Event()
    real_lock = threading.RLock()

    class ControlledLock:
        def __enter__(self):
            if threading.current_thread().name == "degraded-health":
                degraded_lock_started.set()
                assert healthy_written.wait(1)
            real_lock.acquire()

        def __exit__(self, *_args):
            real_lock.release()

    monkeypatch.setattr(user_meta, "_HEALTH_LOCK", ControlledLock())
    errors = []

    def write_degraded():
        connection = db.connect()
        try:
            user_meta.set_integration_health(
                connection,
                "gitlab",
                {"status": "degraded", "message": "GitLab request failed."},
            )
        except Exception as exc:  # surfaced below without leaking into the thread
            errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=write_degraded, name="degraded-health")
    worker.start()
    assert degraded_lock_started.wait(1)
    user_meta.set_integration_health(
        conn,
        "gitlab",
        {
            "status": "healthy",
            "last_success_at": "2026-08-27T10:00:00",
            "message": "",
        },
    )
    healthy_written.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert errors == []
    stored = json.loads(
        conn.execute(
            "SELECT value FROM user_meta WHERE key='integration_health:gitlab'"
        ).fetchone()["value"]
    )
    assert stored["status"] == "degraded"
    assert stored["last_success_at"] == "2026-08-27T10:00:00"


def test_disabled_and_never_attempted_sources_have_explicit_semantics(conn):
    conn.execute(
        "INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)",
        ("integration.flock.disabled", "true", "2026-08-27T10:00:00"),
    )
    conn.commit()

    health = {item["source"]: item for item in user_meta.integration_health(conn)}

    assert health["flock"]["status"] == "disabled"
    assert health["flock"]["message"] == "Integration is disabled in Settings."
    assert health["gitlab"]["status"] == "unconfigured"
    assert health["gitlab"]["message"] == "Integration is not configured. Open Settings to connect it."


def test_clickup_retry_refreshes_exact_linked_ids_only(client, monkeypatch):
    captured = []
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "linked_clickup_ids",
        lambda _conn: ["86abc1234", "86def5678"],
    )
    monkeypatch.setattr(
        sync_pipeline.clickup_client,
        "refresh_exact_tasks",
        lambda _conn, ids: captured.append(tuple(ids))
        or {"updated": 2, "failed": 0, "skipped": 0},
    )
    monkeypatch.setattr(
        sync_pipeline.clickup_client,
        "sync",
        lambda _conn: pytest.fail("retry must not run broad ClickUp sync"),
    )

    response = client.post("/api/sync/retry/clickup")

    assert response.status_code == 200
    assert captured == [("86abc1234", "86def5678")]
    assert response.json()["result"] == {"updated": 2, "failed": 0, "skipped": 0}


def test_legacy_clickup_sync_route_uses_exact_shared_retry_and_keeps_shape(
    client, monkeypatch
):
    captured = []
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "linked_clickup_ids",
        lambda _conn: ["86abc1234", "86def5678"],
    )
    monkeypatch.setattr(
        sync_pipeline.clickup_client,
        "refresh_exact_tasks",
        lambda _conn, ids: captured.append(tuple(ids))
        or {"updated": 2, "failed": 0, "skipped": 0},
    )
    monkeypatch.setattr(
        sync_pipeline.clickup_client,
        "sync",
        lambda _conn: (_ for _ in ()).throw(
            AssertionError("legacy route must never scan ClickUp lists")
        ),
    )

    response = client.post("/api/sync/clickup")

    assert response.status_code == 200
    assert response.json() == {"synced": 2}
    assert captured == [("86abc1234", "86def5678")]


def test_legacy_clickup_sync_route_shares_retry_concurrency_guard(client):
    lock = sync_pipeline._retry_locks["clickup"]
    assert lock.acquire(blocking=False)
    try:
        response = client.post("/api/sync/clickup")
    finally:
        lock.release()

    assert response.status_code == 409


def test_clickup_retry_uses_one_immutable_config_snapshot(client, monkeypatch):
    from app.services import clickup_client

    config_reads = []
    authorizations = []

    def rotating_config():
        token = f"token-v{len(config_reads) + 1}"
        config_reads.append(token)
        return MappingProxyType(
            {"token": token, "list_ids": ("list-1",), "create_list_id": "list-1"}
        )

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, task_id):
            self.task_id = task_id

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": self.task_id, "name": f"Task {self.task_id}"}

        def close(self):
            return None

    def request(url, *, headers, timeout):
        assert timeout == clickup_client.TIMEOUT
        authorizations.append(headers["Authorization"])
        return Response(url.rsplit("/", 1)[-1])

    monkeypatch.setattr(clickup_client, "clickup_config", rotating_config)
    monkeypatch.setattr(clickup_client.requests, "get", request)
    monkeypatch.setattr(
        clickup_client,
        "_TASK_CIRCUIT_BREAKER",
        clickup_client.CircuitBreaker(),
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "linked_clickup_ids",
        lambda _conn: ["86abc1234", "86def5678"],
    )

    response = client.post("/api/sync/retry/clickup")

    assert response.status_code == 200
    assert config_reads == ["token-v1"]
    assert authorizations == ["token-v1", "token-v1"]


@pytest.mark.parametrize(
    ("source", "attribute"),
    (("gitlab", "gitlab_client"), ("google-calendar", "gcal_client"), ("flock", "flock_client")),
)
def test_retry_runs_only_the_requested_source(client, monkeypatch, source, attribute):
    calls = []
    client_module = getattr(sync_pipeline, attribute)
    monkeypatch.setattr(client_module, "configured", lambda: True)
    monkeypatch.setattr(client_module, "sync", lambda _conn: calls.append(source) or 3)

    response = client.post(f"/api/sync/retry/{source}")

    assert response.status_code == 200
    assert calls == [source]
    assert response.json()["result"] == 3


def test_retry_rejects_unknown_source_and_same_source_concurrency(client):
    unknown = client.post("/api/sync/retry/dropbox")
    assert unknown.status_code == 404

    lock = sync_pipeline._retry_locks["gitlab"]
    assert lock.acquire(blocking=False)
    try:
        busy = client.post("/api/sync/retry/gitlab")
    finally:
        lock.release()
    assert busy.status_code == 409


_SOURCE_OPERATION = {
    "clickup": "clickup_exact",
    "gitlab": "gitlab_sync",
    "google-calendar": "gcal_sync",
    "flock": "flock_sync",
}
_COMPAT_SYNC_ROUTE = {
    "gitlab": (
        "/api/sync/gitlab",
        {"synced": 1, "proposed_reviews": 0, "proposed_links": 0},
    ),
    "google-calendar": ("/api/sync/gcal", {"stored": 1}),
    "flock": ("/api/sync/flock", {"stored": 1}),
}


def _configure_one_blocking_source(monkeypatch, source, started, release, calls):
    call_lock = threading.Lock()

    def operation(*_args):
        with call_lock:
            calls.append(source)
            first = len(calls) == 1
        if first:
            started.set()
            assert release.wait(2)
        if source == "clickup":
            return {"updated": 1, "failed": 0, "skipped": 0}
        return 1

    monkeypatch.setattr(
        sync_pipeline.clickup_client, "configured", lambda: source == "clickup"
    )
    monkeypatch.setattr(
        sync_pipeline.gitlab_client, "configured", lambda: source == "gitlab"
    )
    monkeypatch.setattr(
        sync_pipeline.gcal_client,
        "configured",
        lambda: source == "google-calendar",
    )
    monkeypatch.setattr(
        sync_pipeline.flock_client, "configured", lambda: source == "flock"
    )
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "linked_clickup_ids",
        lambda _conn: ["86abc1234"],
    )
    if source == "clickup":
        monkeypatch.setattr(
            sync_pipeline.clickup_client, "refresh_exact_tasks", operation
        )
    else:
        client_module = {
            "gitlab": sync_pipeline.gitlab_client,
            "google-calendar": sync_pipeline.gcal_client,
            "flock": sync_pipeline.flock_client,
        }[source]
        monkeypatch.setattr(client_module, "sync", operation)
    _quiet_downstream(monkeypatch)


@pytest.mark.parametrize("source", tuple(_SOURCE_OPERATION))
def test_retry_returns_409_while_full_pipeline_owns_same_source(
    client, monkeypatch, source
):
    from app import db

    started = threading.Event()
    release = threading.Event()
    calls = []
    errors = []
    _configure_one_blocking_source(
        monkeypatch, source, started, release, calls
    )

    def run_full():
        connection = db.connect()
        try:
            sync_pipeline.run_sync_pipeline(connection)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_full, name=f"full-{source}")
    worker.start()
    assert started.wait(2)
    try:
        response = client.post(f"/api/sync/retry/{source}")
    finally:
        release.set()
        worker.join(timeout=2)

    assert response.status_code == 409
    assert calls == [source]
    assert errors == []
    assert not worker.is_alive()


@pytest.mark.parametrize("source", tuple(_SOURCE_OPERATION))
def test_full_pipeline_skips_source_owned_by_active_retry(
    conn, monkeypatch, source
):
    from app import db

    started = threading.Event()
    release = threading.Event()
    calls = []
    errors = []
    _configure_one_blocking_source(
        monkeypatch, source, started, release, calls
    )

    def run_retry():
        connection = db.connect()
        try:
            sync_pipeline.retry_source(connection, source)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_retry, name=f"retry-{source}")
    worker.start()
    assert started.wait(2)
    try:
        summary = sync_pipeline.run_sync_pipeline(conn)
    finally:
        release.set()
        worker.join(timeout=2)

    assert summary[_SOURCE_OPERATION[source]] == {
        "source": source,
        "skipped": "already_running",
    }
    assert calls == [source]
    assert errors == []
    assert not worker.is_alive()


@pytest.mark.parametrize("source", tuple(_COMPAT_SYNC_ROUTE))
def test_compat_sync_returns_409_while_full_pipeline_owns_source(
    client, monkeypatch, source
):
    from app import db

    started = threading.Event()
    release = threading.Event()
    calls = []
    errors = []
    _configure_one_blocking_source(monkeypatch, source, started, release, calls)

    def run_full():
        connection = db.connect()
        try:
            sync_pipeline.run_sync_pipeline(connection)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_full, name=f"full-compat-{source}")
    worker.start()
    assert started.wait(2)
    try:
        response = client.post(_COMPAT_SYNC_ROUTE[source][0])
    finally:
        release.set()
        worker.join(timeout=2)

    assert response.status_code == 409
    assert calls == [source]
    assert errors == []
    assert not worker.is_alive()


@pytest.mark.parametrize("source", tuple(_COMPAT_SYNC_ROUTE))
def test_full_pipeline_skips_source_owned_by_compat_sync(
    client, conn, monkeypatch, source
):
    started = threading.Event()
    release = threading.Event()
    calls = []
    responses = []
    _configure_one_blocking_source(monkeypatch, source, started, release, calls)

    def run_compat():
        responses.append(client.post(_COMPAT_SYNC_ROUTE[source][0]))

    worker = threading.Thread(target=run_compat, name=f"compat-{source}")
    worker.start()
    assert started.wait(2)
    try:
        summary = sync_pipeline.run_sync_pipeline(conn)
    finally:
        release.set()
        worker.join(timeout=2)

    assert summary[_SOURCE_OPERATION[source]] == {
        "source": source,
        "skipped": "already_running",
    }
    assert len(responses) == 1
    assert responses[0].status_code == 200
    assert responses[0].json() == _COMPAT_SYNC_ROUTE[source][1]
    assert calls == [source]
    assert not worker.is_alive()


@pytest.mark.parametrize("source", tuple(_COMPAT_SYNC_ROUTE))
def test_compat_sync_preserves_response_shape_and_updates_health(
    client, conn, monkeypatch, source
):
    _configure_one_blocking_source(
        monkeypatch, source, threading.Event(), threading.Event(), []
    )
    client_module = {
        "gitlab": sync_pipeline.gitlab_client,
        "google-calendar": sync_pipeline.gcal_client,
        "flock": sync_pipeline.flock_client,
    }[source]
    monkeypatch.setattr(client_module, "sync", lambda _conn: 1)

    response = client.post(_COMPAT_SYNC_ROUTE[source][0])
    row = conn.execute(
        "SELECT value FROM user_meta WHERE key=?",
        (f"integration_health:{source}",),
    ).fetchone()

    assert response.status_code == 200
    assert response.json() == _COMPAT_SYNC_ROUTE[source][1]
    assert row is not None
    health = json.loads(row["value"])
    assert health["status"] == "healthy"


@pytest.mark.parametrize("source", tuple(_COMPAT_SYNC_ROUTE))
def test_compat_sync_records_unconfigured_health_without_calling_source(
    client, conn, monkeypatch, source
):
    calls = []
    client_module = {
        "gitlab": sync_pipeline.gitlab_client,
        "google-calendar": sync_pipeline.gcal_client,
        "flock": sync_pipeline.flock_client,
    }[source]
    monkeypatch.setattr(client_module, "configured", lambda: False)
    monkeypatch.setattr(
        client_module,
        "sync",
        lambda _conn: calls.append(source) or 1,
    )

    response = client.post(_COMPAT_SYNC_ROUTE[source][0])
    row = conn.execute(
        "SELECT value FROM user_meta WHERE key=?",
        (f"integration_health:{source}",),
    ).fetchone()

    assert row is not None
    health = json.loads(row["value"])

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Integration is not configured. Open Settings to connect it."
    )
    assert calls == []
    assert health["status"] == "unconfigured"


def test_failed_retry_keeps_raw_exception_out_of_response_log_db_and_audit(
    client, conn, monkeypatch, caplog
):
    secret = "fake-gitlab-health-token"
    monkeypatch.setattr(sync_pipeline.gitlab_client, "configured", lambda: True)
    monkeypatch.setattr(
        sync_pipeline.gitlab_client,
        "sync",
        lambda _conn: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    response = client.post("/api/sync/retry/gitlab")
    database_text = " ".join(
        row["value"] or "" for row in conn.execute("SELECT value FROM user_meta")
    )
    audit_text = " ".join(
        row["details_json"] or ""
        for row in conn.execute("SELECT details_json FROM system_events")
    )

    assert response.status_code == 502
    assert secret not in response.text
    assert secret not in caplog.text
    assert secret not in database_text
    assert secret not in audit_text
    assert response.json()["detail"] == {
        "source": "gitlab",
        "error": "GitLab request failed. Check the integration and try again.",
    }
    assert "error_type" not in response.text


def test_failed_clickup_retry_records_exact_operation_enum(client, conn, monkeypatch):
    monkeypatch.setattr(
        sync_pipeline.work_ingestion,
        "linked_clickup_ids",
        lambda _conn: ["86abc1234"],
    )
    monkeypatch.setattr(
        sync_pipeline.clickup_client,
        "refresh_exact_tasks",
        lambda _conn, _ids: (_ for _ in ()).throw(RuntimeError("redacted")),
    )

    response = client.post("/api/sync/retry/clickup")
    technical = json.loads(
        conn.execute(
            "SELECT value FROM user_meta WHERE key=?",
            ("integration_health_technical:clickup",),
        ).fetchone()["value"]
    )

    assert response.status_code == 502
    assert technical == {
        "error_type": "external",
        "operation": "clickup_exact_retry",
    }
