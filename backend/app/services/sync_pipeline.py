"""Single source of truth for the periodic sync pipeline.

`job_sync` (scheduler) and `POST /api/sync` (the UI's "Sync now" button) both
call this, so the button produces the exact same outcome as the :00 / :30 tick.

Ordering matters: independent caches are refreshed FIRST. Cached MRs are then
ingested into local work, linked ClickUp IDs are derived from that result, only
those exact tasks are refreshed, and generated titles are reconciled before
detectors and proposers run.

GitLab, Google Calendar, and Flock refresh in parallel. ClickUp is deliberately
absent from that group: its broad list scan has been retired, and its exact
refresh depends on links created by GitLab ingestion.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..models import now_iso
from . import (
    classifier, clickup_client, completion, events, flock_client, gcal_client,
    gitlab_client, link_proposer, mr_review_tracker, orphan_detector,
    review_automation, settings_service, work, work_ingestion,
)
from . import external_errors, user_meta

log = logging.getLogger("watson.sync")

# Withheld from the product until the integration is ready; not a user setting.
FLOCK_RELEASED = False

# Serialise concurrent pipeline runs — the boot-sync thread and a user's Sync
# Now click (or two overlapping scheduler ticks) would otherwise thrash on
# ClickUp/GitLab API rate limits and, worse, contend for Chrome CPU during
# the Flock deep-read (measured: two overlapping runs pushed Flock from
# 20s → 50s+). Skip-if-running is the right call: if a sync is already in
# flight, the caller either doesn't need a fresh one right now (scheduler
# tick landed on a manual sync) or should be blocked by the UI (which it is).
_sync_lock = threading.Lock()
_retry_locks = {
    source: threading.Lock() for source in user_meta.INTEGRATION_SOURCES
}


class SourceRetryBusy(RuntimeError):
    """A retry for this connector is already in progress."""


class SourceRetryUnconfigured(RuntimeError):
    """A retry was requested for a connector not configured in Settings."""


class SourceRetryFailed(RuntimeError):
    """A connector retry failed; ``details`` is safe for an API response."""

    def __init__(self, details: dict):
        super().__init__("integration retry failed")
        self.details = details


def _public_error_details(details: dict) -> dict:
    return {
        key: details[key]
        for key in ("source", "error")
        if key in details
    }


def _public_summary(summary: dict) -> dict:
    return {
        name: (
            _public_review_result(result)
            if name == "review_automation" and isinstance(result, dict)
            else
            _public_error_details(result)
            if isinstance(result, dict) and "error" in result
            else result
        )
        for name, result in summary.items()
    }


def _public_review_result(result: dict) -> dict:
    if "error" in result:
        return _public_error_details(result)
    public = {
        key: max(0, value)
        for key in user_meta.REVIEW_AUTOMATION_COUNT_KEYS
        if isinstance((value := result.get(key)), int)
    }
    status = str(result.get("status") or "").split(" ", 1)[0]
    if status in ("deferred", "healthy", "degraded"):
        public["status"] = status
    return public


def _numeric_result(summary: dict, name: str) -> int:
    value = summary.get(name, 0)
    return value if isinstance(value, int) else 0


def _source_busy(source: str) -> dict[str, str]:
    return {"source": source, "skipped": "already_running"}


def _is_source_busy(result) -> bool:
    return isinstance(result, dict) and result.get("skipped") == "already_running"


def _step(summary: dict, name: str, fn, conn, *, source: str | None = None):
    """Run one pipeline step; a failure logs + records but doesn't abort the run.
    Logs elapsed time so we can see per-step cost at a glance in server.log."""
    t0 = time.monotonic()
    try:
        summary[name] = fn(conn)
    except Exception as exc:  # noqa: BLE001 — the whole pipeline should keep going
        error_source = source or external_errors.source_for_step(name)
        external_errors.log_failure(log, f"sync step {name}", error_source, exc)
        summary[name] = external_errors.details(error_source, exc)
    log.info("sync step %s took %.1fs", name, time.monotonic() - t0)


def _parallel_step(summary: dict, source: str, name: str, fn):
    """Same as _step but opens its own DB connection — safe to run in a thread.
    SQLite's per-connection `busy_timeout` (5000ms) handles write contention."""
    from ..db import connect
    t0 = time.monotonic()
    conn = None
    source_lock = _retry_locks[source]
    if not source_lock.acquire(blocking=False):
        summary[name] = _source_busy(source)
        log.info("sync step %s skipped: source already running", name)
        return
    try:
        conn = connect()
        summary[name] = fn(conn)
    except Exception as exc:  # noqa: BLE001
        source = external_errors.source_for_step(name)
        external_errors.log_failure(log, f"sync step {name}", source, exc)
        summary[name] = external_errors.details(source, exc)
    finally:
        if conn is not None:
            conn.close()
        source_lock.release()
    log.info("sync step %s took %.1fs", name, time.monotonic() - t0)


def _mark_unconfigured(conn, source: str) -> None:
    user_meta.set_integration_health(conn, source, {
        "status": "unconfigured",
        "message": "Integration is not configured. Open Settings to connect it.",
    })


def _mark_source_result(conn, source: str, operation: str, result) -> None:
    if _is_source_busy(result):
        return
    if isinstance(result, dict) and "error" in result:
        user_meta.set_integration_health(conn, source, {
            "status": "degraded",
            "message": external_errors.message(source),
            "technical": user_meta.technical_metadata(
                error_type=result.get("error_type", "ExternalError"),
                operation=operation,
            ),
        })
        return
    if source == "clickup" and isinstance(result, dict):
        unavailable = int(result.get("failed", 0) or 0) + int(result.get("skipped", 0) or 0)
        if unavailable:
            user_meta.set_integration_health(conn, source, {
                "status": "degraded",
                "message": "Some linked ClickUp tasks could not be refreshed. Cached data remains available.",
                "technical": user_meta.technical_metadata(operation=operation),
            })
            return
    user_meta.set_integration_health(conn, source, {
        "status": "healthy",
        "last_success_at": now_iso(),
        "message": "",
    })


def _connector_gate(summary: dict, source: str, operation: str, configured) -> bool:
    try:
        return bool(configured())
    except Exception as exc:
        external_errors.log_failure(log, f"sync gate {operation}", source, exc)
        summary[operation] = external_errors.details(source, exc)
        return False


def is_running() -> bool:
    """True while a pipeline run is in flight. Exposed via /api/sync/status
    so the UI can keep its Sync-now button honest when a request lost the
    lock race (scheduler tick already running)."""
    return _sync_lock.locked()


def run_sync_pipeline(conn) -> dict:
    """Full sync in one shot. Returns per-step results (counts or {"error": ...}).

    Returns {"skipped": "already_running"} if another sync is in flight — see
    the _sync_lock comment above. Callers (route + scheduler) should treat
    that as a benign no-op, not an error.
    """
    if not _sync_lock.acquire(blocking=False):
        log.info("sync: skipped, another pipeline is already in flight")
        return {"skipped": "already_running"}
    try:
        result = _run_sync_pipeline_locked(conn)
        # Stamp completion time regardless of per-step success — the frontend
        # uses this to detect "have we tried recently" (auto-sync on focus
        # after Mac wake). Individual step failures still show up in the
        # returned summary and in server.log.
        from . import user_meta
        user_meta.set_last_sync_at(conn)
        return result
    finally:
        _sync_lock.release()


def _run_sync_pipeline_locked(conn) -> dict:
    summary: dict = {}
    pipeline_t0 = time.monotonic()

    # Snapshot connector gates once for this run. Downstream stages use the
    # same decision even if Settings changes while network work is in flight.
    specs = (
        ("clickup", "clickup_exact", clickup_client.configured, None),
        ("gitlab", "gitlab_sync", gitlab_client.configured, gitlab_client.sync),
        ("google-calendar", "gcal_sync", gcal_client.configured, gcal_client.sync),
    )
    if FLOCK_RELEASED:
        specs += (("flock", "flock_sync", flock_client.configured, flock_client.sync),)
    gates = {
        source: _connector_gate(summary, source, operation, configured)
        for source, operation, configured, _fn in specs
    }
    profile = settings_service.profile(conn)
    ai_ready = False
    if profile["ai_group_review_mrs"]:
        ai_ready = _connector_gate(
            summary,
            "anthropic",
            "review_grouping_ready",
            lambda: bool(classifier.anthropic_config().get("api_key")),
        )

    # 1) Independent network refreshes overlap and each uses its own SQLite
    # connection. Broad ClickUp discovery is intentionally not a sync stage.
    api_parallel = [
        (source, name, fn)
        for source, name, _configured, fn in specs
        if fn is not None and gates[source]
    ]
    operations = {source: operation for source, operation, _configured, _fn in specs}
    for source in gates:
        if not gates[source]:
            operation = operations[source]
            if operation in summary:
                _mark_source_result(conn, source, operation, summary[operation])
            else:
                _mark_unconfigured(conn, source)

    if api_parallel:
        parallel_t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(api_parallel)) as pool:
            futs = [
                pool.submit(_parallel_step, summary, source, name, fn)
                for source, name, fn in api_parallel
            ]
            for f in futs:
                f.result()
        log.info("sync: %d API cache refreshers ran in parallel, %.1fs wall",
                 len(api_parallel), time.monotonic() - parallel_t0)
        for source, name, _fn in api_parallel:
            _mark_source_result(conn, source, name, summary[name])

    # 2) Strict dependency chain: MR ingestion can create ClickUp links, so
    # exact IDs cannot be computed before it. Reconciliation reads the exact
    # cache results and therefore cannot move ahead of the refresh.
    _step(summary, "ingest_mrs", work_ingestion.ingest_cached_mrs, conn)
    _step(
        summary,
        "retire_terminal_mr_work",
        work_ingestion.retire_terminal_mr_work,
        conn,
    )
    if isinstance(summary.get("gitlab_sync"), int):
        _step(
            summary,
            "retire_missing_gitlab_work",
            work_ingestion.retire_missing_gitlab_work,
            conn,
        )
        _step(
            summary,
            "retire_unselected_gitlab_work",
            work_ingestion.retire_unselected_gitlab_work,
            conn,
        )
        _step(
            summary,
            "deactivate_unselected_proposals",
            mr_review_tracker.deactivate_unselected_proposals,
            conn,
        )
    _step(summary, "linked_clickup_ids", work_ingestion.linked_clickup_ids, conn)
    if gates["clickup"] and isinstance(summary.get("linked_clickup_ids"), list):
        ids = summary["linked_clickup_ids"]
        clickup_lock = _retry_locks["clickup"]
        if clickup_lock.acquire(blocking=False):
            try:
                _step(
                    summary,
                    "clickup_exact",
                    lambda c: clickup_client.refresh_exact_tasks(c, ids),
                    conn,
                    source="clickup",
                )
            finally:
                clickup_lock.release()
        else:
            summary["clickup_exact"] = _source_busy("clickup")
        _mark_source_result(conn, "clickup", "clickup_exact", summary["clickup_exact"])
    _step(summary, "reconcile_titles", work_ingestion.reconcile_clickup_titles, conn)
    _step(
        summary,
        "review_automation",
        lambda c: review_automation.run(
            c,
            profile=profile,
            clickup_ready=gates["clickup"],
            ai_ready=ai_ready,
        ),
        conn,
        source="review-automation",
    )
    user_meta.set_review_automation_health(conn, summary["review_automation"])

    # 3) Applicable legacy detectors remain cache-only and sequential.
    if gates["clickup"]:
        # adopt any pre-existing Watson-shaped ClickUp tasks that aren't yet
        # in managed_tasks — must run before completions so newly-adopted
        # tasks get their close-drafts drafted in the same tick.
        _step(summary, "backfill", work.backfill_managed_tasks, conn)
        # recover related_clickup_task_id for rows whose branch-tag failed at
        # draft time; uses the MR description URL as fallback. Needs BOTH caches
        # fresh, so it runs after gitlab_sync + clickup_sync.
        _step(summary, "relink", work.backfill_related_clickup_ids, conn)
        _step(summary, "completions", completion.check_completions, conn)
        _step(summary, "orphans", orphan_detector.detect_orphans, conn)
    # 4) Remaining proposer uses the refreshed-or-preserved GitLab cache.
    if gates["gitlab"]:
        _step(summary, "propose_links", link_proposer.propose_mr_task_links, conn)

    # Audit line: one event per sync tick summarizing what changed. Skip
    # writing an event if EVERY numeric step returned 0 AND no errors — a
    # quiet tick doesn't need a row.
    numeric = {k: v for k, v in summary.items() if isinstance(v, int)}
    review_counts = _public_review_result(summary.get("review_automation", {}))
    review_numeric = {
        key: value for key, value in review_counts.items() if isinstance(value, int)
    }
    errors = [k for k, v in summary.items() if isinstance(v, dict) and "error" in v]
    if any(v != 0 for v in numeric.values()) or any(review_numeric.values()) or errors:
        parts = []
        exact = summary.get("clickup_exact")
        if isinstance(exact, dict) and exact.get("updated"):
            parts.append(f"{exact['updated']} ClickUp tasks")
        gitlab_count = _numeric_result(summary, "gitlab_sync")
        gcal_count = _numeric_result(summary, "gcal_sync")
        flock_count = _numeric_result(summary, "flock_sync")
        if gitlab_count: parts.append(f"{gitlab_count} MRs")
        if gcal_count: parts.append(f"{gcal_count} calendar events")
        if flock_count: parts.append(f"{flock_count} Flock threads")
        proposed = sum(
            _numeric_result(summary, name)
            for name in ("propose_links",)
        )
        drafted = _numeric_result(summary, "completions") + _numeric_result(
            summary, "orphans"
        )
        if proposed: parts.append(f"{proposed} new proposal{'s' if proposed != 1 else ''}")
        if drafted: parts.append(f"{drafted} close draft{'s' if drafted != 1 else ''}")
        review_changed = sum(
            review_numeric.get(name, 0)
            for name in ("exact_grouped", "ai_grouped", "attached", "created")
        )
        review_attention = sum(
            review_numeric.get(name, 0)
            for name in ("deferred", "ambiguous", "failed", "uncertain")
        )
        if review_changed:
            parts.append(f"{review_changed} review automation update{'s' if review_changed != 1 else ''}")
        if review_attention:
            parts.append(f"{review_attention} review automation item{'s' if review_attention != 1 else ''} need attention")
        if errors: parts.append(f"errors in {', '.join(errors)}")
        subject = "Sync — " + (", ".join(parts) if parts else "no changes")
        events.record(conn, "sync", subject, details=_public_summary(summary))
        conn.commit()
    log.info("sync: total pipeline %.1fs", time.monotonic() - pipeline_t0)
    return _public_summary(summary)


def retry_source(conn, source: str):
    """Retry one connector without broadening the operation's write scope."""
    if source not in _retry_locks:
        raise KeyError(source)
    operations = {
        "clickup": "clickup_exact_retry",
        "gitlab": "gitlab_retry",
        "google-calendar": "google-calendar_retry",
        "flock": "flock_retry",
    }
    operation = operations[source]
    lock = _retry_locks[source]
    if not lock.acquire(blocking=False):
        raise SourceRetryBusy(source)
    try:
        if source == "clickup":
            ids = work_ingestion.linked_clickup_ids(conn)
            result = clickup_client.refresh_exact_tasks(conn, ids)
        else:
            clients = {
                "gitlab": gitlab_client,
                "google-calendar": gcal_client,
                "flock": flock_client,
            }
            client = clients[source]
            if not client.configured():
                _mark_unconfigured(conn, source)
                raise SourceRetryUnconfigured(source)
            result = client.sync(conn)
        _mark_source_result(conn, source, operation, result)
        return result
    except SourceRetryUnconfigured:
        raise
    except Exception as exc:
        external_errors.log_failure(log, f"sync retry {source}", source, exc)
        details = external_errors.details(source, exc)
        user_meta.set_integration_health(conn, source, {
            "status": "degraded",
            "message": external_errors.message(source),
            "technical": user_meta.technical_metadata(
                error_type=details["error_type"],
                operation=operation,
            ),
        })
        raise SourceRetryFailed(_public_error_details(details)) from None
    finally:
        lock.release()
