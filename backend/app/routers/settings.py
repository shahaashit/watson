"""Permanent Settings and onboarding HTTP API."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from ..db import get_db
from ..schemas import (
    DataSettingsIn,
    GitLabProjectsIn,
    OnboardingPatch,
    PersonIn,
    PersonPatch,
    ProfileSettingsIn,
)
from ..services import secret_store, settings_service


router = APIRouter(prefix="/api", tags=["settings"])


def _validation_detail(error: ValidationError) -> list[dict]:
    """Return useful field errors without echoing request values or secrets."""
    return [
        {
            "loc": list(item["loc"]),
            "msg": item["msg"],
            "type": item["type"],
        }
        for item in error.errors(include_input=False, include_context=False)
    ]


def _source_or_404(source: str):
    model = settings_service.SOURCE_MODELS.get(source)
    if model is None:
        raise HTTPException(404, "integration source not found")
    return model


def _credential_store_error() -> None:
    raise HTTPException(503, "Credential store unavailable.")


@router.get("/settings")
def get_settings(conn=Depends(get_db)):
    return settings_service.settings_snapshot(conn)


@router.patch("/settings/profile")
def patch_profile(payload: ProfileSettingsIn, conn=Depends(get_db)):
    try:
        profile = settings_service.update_profile(
            conn,
            display_name=payload.display_name,
            timezone=payload.timezone,
            email_domain=payload.email_domain,
            separate_work_by_status=payload.separate_work_by_status,
            auto_create_review_tasks=payload.auto_create_review_tasks,
            ai_group_review_mrs=payload.ai_group_review_mrs,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except settings_service.ProfileApplyError:
        raise HTTPException(503, "Unable to apply profile settings.") from None
    return {"profile": profile}


@router.post(
    "/settings/integrations/google-calendar/connect",
    status_code=202,
)
def connect_google_calendar():
    from ..services import oauth_sessions

    try:
        session_id = oauth_sessions.start_google_calendar()
    except oauth_sessions.OAuthSessionCapacityError:
        raise HTTPException(503, "OAuth session capacity reached.") from None
    except Exception:
        raise HTTPException(503, "Unable to start OAuth session.") from None
    return {"session_id": session_id, "status": "pending"}


@router.get("/settings/integrations/google-calendar/connect/{session_id}")
def google_calendar_connect_status(session_id: str):
    from ..services import oauth_sessions

    try:
        return oauth_sessions.status(session_id)
    except KeyError:
        raise HTTPException(404, "OAuth session not found.") from None


@router.put("/settings/integrations/{source}")
async def put_integration(source: str, request: Request, conn=Depends(get_db)):
    model = _source_or_404(source)
    try:
        raw_payload = await request.json()
        payload = model.model_validate(raw_payload)
    except (ValueError, ValidationError) as error:
        if isinstance(error, ValidationError):
            detail = _validation_detail(error)
        else:
            detail = [{"loc": [], "msg": "Invalid JSON body", "type": "json_invalid"}]
        raise HTTPException(422, detail) from None
    try:
        integration = settings_service.update_integration(conn, source, payload)
    except secret_store.SecretStoreError:
        _credential_store_error()
    return {"integration": integration}


@router.delete("/settings/integrations/{source}")
def disconnect_integration(
    source: str,
    confirm: bool = Query(default=False),
    conn=Depends(get_db),
):
    _source_or_404(source)
    if not confirm:
        raise HTTPException(400, "confirm=true is required to disconnect")
    try:
        integration = settings_service.disconnect_integration(conn, source)
    except secret_store.SecretStoreError:
        _credential_store_error()
    return {"integration": integration}


@router.post("/settings/integrations/{source}/import-env")
def import_environment(source: str, conn=Depends(get_db)):
    _source_or_404(source)
    try:
        integration = settings_service.import_environment(conn, source)
    except secret_store.SecretStoreError:
        _credential_store_error()
    return {"integration": integration}


@router.post("/settings/integrations/{source}/test")
def test_integration(source: str, conn=Depends(get_db)):
    _source_or_404(source)
    try:
        health = settings_service.test_integration(conn, source)
    except settings_service.ConnectionTestBusy:
        raise HTTPException(409, "Connection test already running.") from None
    return {"source": source, "health": health}


@router.get("/settings/integrations/gitlab/projects")
def get_gitlab_projects(q: str = Query(default="", max_length=100), conn=Depends(get_db)):
    from ..services import gitlab_client

    query = q.strip()
    if query:
        try:
            projects = gitlab_client.list_accessible_projects(search=query)
        except Exception:
            raise HTTPException(
                503, "Unable to search repositories in GitLab."
            ) from None
    else:
        projects = settings_service.known_gitlab_projects(conn)
    return {
        "projects": projects,
        "selected": settings_service.gitlab_projects(conn),
    }


@router.put("/settings/integrations/gitlab/projects")
def put_gitlab_projects(payload: GitLabProjectsIn, conn=Depends(get_db)):
    try:
        selected = settings_service.update_gitlab_projects(
            conn, payload.project_ids
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception:
        raise HTTPException(
            503, "Unable to save repositories from GitLab."
        ) from None
    return {"selected": selected}


@router.get("/settings/people")
def get_people(conn=Depends(get_db)):
    return {"people": settings_service.list_people(conn)}


@router.post("/settings/people", status_code=201)
def create_person(payload: PersonIn, conn=Depends(get_db)):
    try:
        person = settings_service.create_person(conn, payload)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"person": person}


@router.patch("/settings/people/{person_id}")
def patch_person(person_id: int, payload: PersonPatch, conn=Depends(get_db)):
    try:
        person = settings_service.update_person(conn, person_id, payload)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"person": person}


@router.delete("/settings/people/{person_id}")
def delete_person(person_id: int, conn=Depends(get_db)):
    try:
        return settings_service.delete_person(conn, person_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/onboarding")
def get_onboarding(conn=Depends(get_db)):
    return settings_service.onboarding(conn)


@router.patch("/onboarding")
def patch_onboarding(payload: OnboardingPatch, conn=Depends(get_db)):
    return settings_service.update_onboarding(
        conn, completed=payload.completed, step=payload.step
    )


@router.patch("/settings/data")
def patch_data(payload: DataSettingsIn, conn=Depends(get_db)):
    try:
        data = settings_service.update_data_settings(conn, payload.backup_dir)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"data": data}
