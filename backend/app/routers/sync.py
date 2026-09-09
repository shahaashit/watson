from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..services import sync_pipeline, user_meta

router = APIRouter(prefix="/api", tags=["sync"])


@router.post("/sync")
def sync_all(conn=Depends(get_db)):
    """Full sync pipeline — the same one `job_sync` runs every ten clock-aligned minutes.

    Blocks until the pipeline finishes so the UI's Sync-now button can hold
    its "Syncing…" state for the whole run and show results in one refresh.
    GitLab, Google Calendar, and Flock refresh independently in parallel;
    linked ClickUp tasks refresh afterward because their IDs come from MR
    ingestion.

    Response shape:
      { clickup_sync: N, gitlab_sync: N, ... } — normal completion
      { skipped: "already_running" } — a scheduler tick was already in flight;
        caller should poll /api/sync/status until it returns { running: false }
        before considering their sync request satisfied.
    """
    return sync_pipeline.run_sync_pipeline(conn)


@router.get("/sync/status")
def sync_status(conn=Depends(get_db)):
    """Whether any pipeline (route- or scheduler-driven) is currently running.
    Includes persisted metrics for the last completed attempt, even if some
    steps failed. This local-only read is safe to poll every three seconds."""
    return {
        "running": sync_pipeline.is_running(),
        **user_meta.last_sync_status(conn),
        "sync_interval_seconds": 600,
        "sources": user_meta.integration_health(conn),
    }


@router.post("/sync/retry/{source}")
def retry_source(source: str, conn=Depends(get_db)):
    """Retry one connector. External writes remain approval-gated elsewhere."""
    if source not in user_meta.INTEGRATION_SOURCES:
        raise HTTPException(status_code=404, detail="Unknown integration source.")
    try:
        result = sync_pipeline.retry_source(conn, source)
    except sync_pipeline.SourceRetryBusy:
        raise HTTPException(
            status_code=409,
            detail="A retry for this integration is already running.",
        ) from None
    except sync_pipeline.SourceRetryUnconfigured:
        raise HTTPException(
            status_code=400,
            detail="Integration is not configured. Open Settings to connect it.",
        ) from None
    except sync_pipeline.SourceRetryFailed as exc:
        raise HTTPException(status_code=502, detail=exc.details) from None
    return {"source": source, "result": result}


@router.post("/sync/flock")
def sync_flock_only(conn=Depends(get_db)):
    """Run ONLY the Flock cache refresh. Fast-feedback debug endpoint — skips
    ClickUp, GitLab, GCal, and all downstream detectors/proposers. Failures use
    the same redacted external-error boundary as the full pipeline."""
    try:
        stored = sync_pipeline.retry_source(conn, "flock")
    except sync_pipeline.SourceRetryBusy:
        raise HTTPException(
            status_code=409,
            detail="A retry for this integration is already running.",
        ) from None
    except sync_pipeline.SourceRetryUnconfigured:
        raise HTTPException(
            status_code=400,
            detail="Integration is not configured. Open Settings to connect it.",
        ) from None
    except sync_pipeline.SourceRetryFailed as exc:
        raise HTTPException(status_code=502, detail=exc.details) from None
    return {"stored": stored}


@router.post("/sync/gcal")
def sync_gcal_only(conn=Depends(get_db)):
    """Same shape as /sync/flock — GCal-only for fast debug iteration."""
    try:
        stored = sync_pipeline.retry_source(conn, "google-calendar")
    except sync_pipeline.SourceRetryBusy:
        raise HTTPException(
            status_code=409,
            detail="A retry for this integration is already running.",
        ) from None
    except sync_pipeline.SourceRetryUnconfigured:
        raise HTTPException(
            status_code=400,
            detail="Integration is not configured. Open Settings to connect it.",
        ) from None
    except sync_pipeline.SourceRetryFailed as exc:
        raise HTTPException(status_code=502, detail=exc.details) from None
    return {"stored": stored}
