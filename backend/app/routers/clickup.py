from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import task_dict
from ..services import sync_pipeline

router = APIRouter(prefix="/api", tags=["clickup"])


@router.get("/clickup/tasks")
def list_tasks(conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM clickup_tasks_cache ORDER BY name").fetchall()
    return {"tasks": [task_dict(r) for r in rows]}


@router.post("/sync/clickup")
def sync_now(conn=Depends(get_db)):
    try:
        result = sync_pipeline.retry_source(conn, "clickup")
    except sync_pipeline.SourceRetryBusy:
        raise HTTPException(
            status_code=409,
            detail="A retry for this integration is already running.",
        ) from None
    except sync_pipeline.SourceRetryFailed as exc:
        raise HTTPException(status_code=502, detail=exc.details) from None
    return {"synced": int(result.get("updated", 0) or 0)}
