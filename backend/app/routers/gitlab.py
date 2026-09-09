from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import mr_dict
from ..services import sync_pipeline

router = APIRouter(prefix="/api", tags=["gitlab"])


@router.get("/gitlab/mrs")
def list_mrs(conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT * FROM gitlab_mrs_cache ORDER BY updated_at"
    ).fetchall()
    return {"mrs": [mr_dict(r) for r in rows]}


@router.post("/sync/gitlab")
def sync_now(conn=Depends(get_db)):
    try:
        count = sync_pipeline.retry_source(conn, "gitlab")
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
    return {"synced": count, "proposed_reviews": 0, "proposed_links": 0}
