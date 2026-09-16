from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel

from ..db import get_db
from ..services import meet_links


class SafeValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, {'code': 'failed', 'message': 'Provide a valid UUID request_id.'}) from None
        return safe_handler


router = APIRouter(prefix='/api/meet-links', tags=['meet-links'], route_class=SafeValidationRoute)


class MeetLinkRequest(BaseModel):
    request_id: UUID


@router.get('/status')
def status():
    return meet_links.status()


@router.post('')
def create(payload: MeetLinkRequest, conn=Depends(get_db)):
    try:
        return meet_links.create(conn, str(payload.request_id))
    except meet_links.MeetError as error:
        raise HTTPException(meet_links.HTTP_STATUS[error.code],
                            {'code': error.code, 'message': meet_links.MESSAGES[error.code]}) from None
    except Exception:
        # A response can be lost after commit; retrying the UUID recovers it.
        # Never expose database or other internal exception text.
        raise HTTPException(409, {'code': 'uncertain', 'message': meet_links.MESSAGES['uncertain']}) from None
