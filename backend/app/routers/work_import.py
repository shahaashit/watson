"""Manual, exact URL imports into the local work board."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from ..db import get_db
from ..schemas import WorkImportIn
from ..services import external_errors, work_ingestion


router = APIRouter(prefix="/api", tags=["work-import"])


@router.post("/work-import")
def import_work_url(payload: WorkImportIn, conn=Depends(get_db)):
    try:
        result = work_ingestion.import_external_url(conn, payload.url)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except Exception as error:
        source = "clickup" if "clickup.com" in payload.url.lower() else "gitlab"
        raise HTTPException(502, external_errors.details(source, error)) from None
    return JSONResponse(
        {"work_item": result["work_item"]},
        status_code=201 if result["created"] else 200,
    )
