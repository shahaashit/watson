"""Watson — FastAPI app + scheduler + static UI mount.

Single-user localhost work assistant. Read CLAUDE.md at the repo root before
making structural changes. Key invariants this module enforces:

  * Scheduler is `cron minute='*/10'` (every ten clock-aligned minutes) — user explicitly
    asked for clock-aligned times. Don't switch to `interval` without asking.
  * Boot sync runs in a daemon thread so startup never blocks on the network
    (matters for the always-on launchd agent + KeepAlive restarts).
  * The built UI is served from the same FastAPI app at /, so one process on
    :8000 serves both API and app. Dev-mode Vite (:5173) is for HMR only.
  * Binds 127.0.0.1 — never expose externally.
"""
import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from . import db
from .config import settings
from .routers import actions, ask, capture, clickup, entries, flock_webhook, gitlab, log as log_router, notes, reminders, review_automation, search, settings as settings_router, sync, today, work_import, work_inbox, work_items
from .services import (
    clickup_client, digest, exporter, external_errors, gitlab_client,
    settings_service, sync_pipeline, work_backfill,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("watson")
_scheduler = None
_scheduler_lock = threading.Lock()


def _with_conn(fn):
    """Run a scheduler job with its own connection; never let it crash the scheduler."""
    conn = db.connect()
    try:
        settings_service.apply_runtime_settings(conn)
        fn(conn)
    except Exception as exc:
        external_errors.log_failure(
            log, f"scheduled job {fn.__name__}", "external-service", exc
        )
    finally:
        conn.close()


def job_sync():
    _with_conn(sync_pipeline.run_sync_pipeline)


def job_digest():
    _with_conn(digest.run_digest)


def job_export():
    _with_conn(exporter.export_day)
    _with_conn(exporter.backup_db)


def _backfill_work_at_startup() -> None:
    """Adopt legacy managed work without preventing the local app from starting."""
    conn = db.connect()
    try:
        work_backfill.backfill_managed_work(conn)
    except Exception as exc:
        external_errors.log_failure(
            log, "startup managed-work backfill", "external-service", exc
        )
    finally:
        conn.close()


def start_scheduler() -> BackgroundScheduler:
    tz = ZoneInfo(settings.tz)
    scheduler = BackgroundScheduler(timezone=tz)
    # cron with minute="*/10" so it fires every ten minutes of every
    # hour regardless of restart time — easier to predict than interval.
    scheduler.add_job(job_sync, "cron", minute="*/10", id="connector_sync")
    hour, minute = settings.digest_time.split(":")
    scheduler.add_job(job_digest, "cron", hour=int(hour), minute=int(minute), id="morning_digest")
    scheduler.add_job(job_export, "cron", hour=23, minute=55, id="daily_export")
    scheduler.start()
    return scheduler


def reschedule_scheduler_timezone(
    timezone: str, *, timeout_seconds: float = 2.0
) -> bool:
    """Rebuild timezone-sensitive cron triggers on the running scheduler."""
    tz = ZoneInfo(timezone)
    if not _scheduler_lock.acquire(timeout=timeout_seconds):
        raise TimeoutError("scheduler reschedule lock timed out")
    try:
        if _scheduler is None:
            return False
        job_ids = ("connector_sync", "morning_digest", "daily_export")
        previous_triggers = {
            job_id: _scheduler.get_job(job_id).trigger for job_id in job_ids
        }
        try:
            _scheduler.reschedule_job(
                "connector_sync", trigger="cron", minute="*/10", timezone=tz
            )
            hour, minute = settings.digest_time.split(":")
            _scheduler.reschedule_job(
                "morning_digest",
                trigger="cron",
                hour=int(hour),
                minute=int(minute),
                timezone=tz,
            )
            _scheduler.reschedule_job(
                "daily_export", trigger="cron", hour=23, minute=55, timezone=tz
            )
        except Exception:
            restore_failed = False
            for job_id, trigger in previous_triggers.items():
                try:
                    _scheduler.reschedule_job(job_id, trigger=trigger)
                except Exception:
                    restore_failed = True
            if restore_failed:
                raise RuntimeError("scheduler trigger restoration failed") from None
            raise
        return True
    finally:
        _scheduler_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    db.init_db()
    runtime_conn = db.connect()
    try:
        settings_service.apply_runtime_settings(runtime_conn)
    finally:
        runtime_conn.close()
    _backfill_work_at_startup()
    scheduler = None
    if not settings.testing:
        scheduler = start_scheduler()
        with _scheduler_lock:
            _scheduler = scheduler
        # warm the caches in the background so startup isn't blocked on the
        # network (matters for an always-on service + KeepAlive restarts)
        threading.Thread(target=job_sync, name="boot-sync", daemon=True).start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)
        with _scheduler_lock:
            if _scheduler is scheduler:
                _scheduler = None


app = FastAPI(title="Watson", lifespan=lifespan)

app.include_router(capture.router)
app.include_router(entries.router)
app.include_router(reminders.router)
app.include_router(actions.router)
app.include_router(clickup.router)
app.include_router(gitlab.router)
app.include_router(today.router)
app.include_router(ask.router)
app.include_router(search.router)
app.include_router(sync.router)
app.include_router(log_router.router)
app.include_router(flock_webhook.router)
app.include_router(notes.router)
app.include_router(work_items.router)
app.include_router(work_import.router)
app.include_router(work_inbox.router)
app.include_router(settings_router.router)
app.include_router(review_automation.router)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "db": str(db.db_path()),
        "clickup_configured": clickup_client.configured(),
        "gitlab_configured": gitlab_client.configured(),
        "llm_configured": bool(settings.anthropic_api_key),
    }


# Serve built assets and only the known browser entry routes. The deliberately
# narrow fallback keeps bad API URLs and missing static files as real 404s.
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_ASSETS = _DIST / "assets"
if _ASSETS.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")


_SPA_PATHS = {
    "",
    "my-work",
    "team",
    "log",
    "settings",
    "onboarding",
}
_SETTINGS_SECTIONS = {"profile", "integrations", "people", "sync", "data"}
_WORK_ID_PATTERN = re.compile(r"[1-9][0-9]{0,14}")


def _is_spa_path(path: str) -> bool:
    path = path.rstrip("/")
    if path in _SPA_PATHS:
        return True
    parts = path.split("/")
    if len(parts) == 2 and parts[0] == "work":
        return _WORK_ID_PATTERN.fullmatch(parts[1]) is not None
    return len(parts) == 2 and parts[0] == "settings" and parts[1] in _SETTINGS_SECTIONS


@app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
def favicon():
    icon = _DIST / "favicon.svg"
    if not icon.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(icon, media_type="image/svg+xml")


@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
def spa_entry(path: str, request: Request):
    # A browser document navigation explicitly accepts HTML. Fetch/XHR callers
    # that happen to target a UI URL receive 404 instead of app markup.
    if path.startswith("api/") or not _is_spa_path(path):
        raise HTTPException(404, "Not found")
    if "text/html" not in request.headers.get("accept", "").lower():
        raise HTTPException(404, "Not found")
    index = _DIST / "index.html"
    if not index.is_file():
        # Do not hide a missing production build behind a synthetic response.
        raise HTTPException(404, "Frontend build not found")
    return FileResponse(index, media_type="text/html")
