"""Completed-attempt status uses scratch SQLite and never contacts connectors."""
from datetime import datetime
from contextlib import closing
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import db, main
from app.services import sync_pipeline, user_meta


@pytest.fixture
def offline_pipeline(monkeypatch):
    for connector in (sync_pipeline.clickup_client, sync_pipeline.gitlab_client,
                      sync_pipeline.gcal_client, sync_pipeline.flock_client):
        monkeypatch.setattr(connector, "configured", lambda: False)
        monkeypatch.setattr(connector, "sync", lambda _conn: pytest.fail("network sync"))
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(sync_pipeline, "time", SimpleNamespace(monotonic=lambda: clock.value))
    return clock


def test_status_defaults_and_legacy_timestamp(client, conn):
    for timestamp in ("", "2026-09-09T10:00:00"):
        if timestamp:
            user_meta.set_last_sync_at(conn, timestamp)
        response = client.get("/api/sync/status")
        assert response.status_code == 200
        status = response.json()
        assert status["running"] is False
        assert status["last_sync_at"] == timestamp
        assert isinstance(status["sources"], list)
        assert status["last_sync_duration_seconds"] is None
        assert status["last_sync_error_count"] == 0
        assert status["sync_interval_seconds"] == 600


def test_attempt_metrics_persist_and_success_replaces_partial_failure(
    client, conn, monkeypatch, offline_pipeline,
):
    original = sync_pipeline.work_ingestion.ingest_cached_mrs

    def fail_ingestion(_conn):
        offline_pipeline.value = 102.75
        raise RuntimeError("synthetic-private-error-marker")

    monkeypatch.setattr(sync_pipeline.work_ingestion, "ingest_cached_mrs", fail_ingestion)
    result = sync_pipeline.run_sync_pipeline(conn)
    assert "error" in result["ingest_mrs"]
    status = client.get("/api/sync/status").json()
    assert status["last_sync_duration_seconds"] == 2.75
    assert status["last_sync_error_count"] == 1
    assert status["last_sync_at"]
    with closing(db.connect()) as other:
        assert user_meta.get_meta(other, "last_sync_duration_seconds") == "2.75"
        assert user_meta.get_meta(other, "last_sync_error_count") == "1"
        assert "synthetic-private-error-marker" not in " ".join(
            row["value"] for row in other.execute("SELECT value FROM user_meta")
        )

    def succeed_ingestion(connection):
        offline_pipeline.value = 111.25
        return original(connection)

    offline_pipeline.value = 110.0
    monkeypatch.setattr(sync_pipeline.work_ingestion, "ingest_cached_mrs", succeed_ingestion)
    sync_pipeline.run_sync_pipeline(conn)
    status = client.get("/api/sync/status").json()
    assert status["last_sync_duration_seconds"] == 1.25
    assert status["last_sync_error_count"] == 0


def test_running_attempt_and_skipped_request_preserve_completed_metrics(
    client, conn, monkeypatch, offline_pipeline,
):
    sync_pipeline.run_sync_pipeline(conn)
    completed = client.get("/api/sync/status").json()

    def ingest_during_run(_conn):
        status = client.get("/api/sync/status").json()
        assert status == {**completed, "running": True}
        assert sync_pipeline.run_sync_pipeline(conn) == {"skipped": "already_running"}
        assert client.get("/api/sync/status").json() == status
        offline_pipeline.value = 105.0
        return {"created": 0, "updated": 0}

    monkeypatch.setattr(sync_pipeline.work_ingestion, "ingest_cached_mrs", ingest_during_run)
    result = sync_pipeline.run_sync_pipeline(conn)
    assert "error" not in result["ingest_mrs"]
    assert client.get("/api/sync/status").json()["last_sync_duration_seconds"] == 5.0
    assert not sync_pipeline.is_running()


def test_unexpected_pipeline_failure_records_attempt_and_releases_lock(
    client, conn, monkeypatch, offline_pipeline,
):
    def fail_profile(_conn):
        offline_pipeline.value = 104.0
        raise RuntimeError("synthetic-private-error-marker")

    monkeypatch.setattr(sync_pipeline.settings_service, "profile", fail_profile)
    with pytest.raises(RuntimeError, match="synthetic-private-error-marker"):
        sync_pipeline.run_sync_pipeline(conn)
    status = client.get("/api/sync/status").json()
    assert status["running"] is False
    assert status["last_sync_at"]
    assert status["last_sync_duration_seconds"] == 4.0
    assert status["last_sync_error_count"] == 1
    assert "synthetic-private-error-marker" not in str(status)


def test_partial_result_counts_are_reported(client, conn, monkeypatch, offline_pipeline):
    monkeypatch.setattr(sync_pipeline.clickup_client, "configured", lambda: True)
    monkeypatch.setattr(sync_pipeline.clickup_client, "refresh_exact_tasks",
                        lambda _conn, ids: {"updated": 1, "failed": 2, "skipped": 1})
    monkeypatch.setattr(sync_pipeline.review_automation, "run",
                        lambda _conn, **kwargs: {"failed": 1, "uncertain": 1, "deferred": 3})
    sync_pipeline.run_sync_pipeline(conn)
    assert client.get("/api/sync/status").json()["last_sync_error_count"] == 5


def test_scheduler_fires_on_ten_minute_boundaries_before_and_after_timezone_change(monkeypatch):
    # Register real jobs but never start the background thread or execute jobs.
    monkeypatch.setattr(main.BackgroundScheduler, "start", lambda self: None)
    scheduler = main.start_scheduler()
    monkeypatch.setattr(main, "_scheduler", scheduler)
    for timezone in (main.settings.tz, "UTC"):
        if timezone == "UTC":
            assert main.reschedule_scheduler_timezone(timezone)
        trigger = scheduler.get_job("connector_sync").trigger
        now = datetime(2026, 9, 9, 10, 3, tzinfo=ZoneInfo(timezone))
        first = trigger.get_next_fire_time(None, now)
        assert (first.hour, first.minute, first.second) == (10, 10, 0)
        second = trigger.get_next_fire_time(first, first)
        assert (second.hour, second.minute, second.second) == (10, 20, 0)
