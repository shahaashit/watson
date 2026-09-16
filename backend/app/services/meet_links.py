"""Local, same-organization Google Meet nickname links. No provider writes."""
import secrets

from ..models import now_iso
from . import events

MESSAGES = {
    'reconnect_required': 'This older request failed. Try generating a new link.',
    'api_disabled': 'This older request failed. Try generating a new link.',
    'permission_denied': 'This older request failed. Try generating a new link.',
    'in_progress': 'An older request is still unresolved. It will not be replaced automatically.',
    'uncertain': 'The result could not be confirmed. Retry to recover the same request.',
    'failed': 'Could not generate a meeting link. Please try again.',
}
HTTP_STATUS = {'reconnect_required': 401, 'api_disabled': 403, 'permission_denied': 403,
               'in_progress': 409, 'uncertain': 409, 'failed': 502}


class MeetError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def status():
    # Nicknames require Workspace accounts when opened, not OAuth in Watson.
    return {'ready': True, 'reason': 'ready',
            'message': 'Share with people in your Google Workspace organization.'}


def create(conn, request_id):
    # Commit the URL and audit together. Concurrent/retried UUIDs return one URL.
    # Existing API-created requests must retain their original destination/status.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM meet_link_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is not None:
            if row['status'] == 'succeeded':
                return {'url': row['url']}
            raise MeetError(row['error_code'] or 'in_progress')

        for _ in range(5):
            token = secrets.token_hex(8)
            nickname = 'w-' + '-'.join(token[i:i + 4] for i in range(0, 16, 4))
            url = 'https://g.co/meet/' + nickname
            if not conn.execute('SELECT 1 FROM meet_link_requests WHERE url=?', (url,)).fetchone():
                break
        else:
            raise MeetError('failed')

        timestamp = now_iso()
        conn.execute(
            "INSERT INTO meet_link_requests(request_id,status,url,created_at,updated_at) VALUES (?,'succeeded',?,?,?)",
            (request_id, url, timestamp, timestamp))
        event_id = events.record(conn, 'meet_link_create', 'Google Meet nickname link generated.',
                                 details={'request_id': request_id, 'status': 'succeeded'})
        if not event_id:
            raise MeetError('uncertain')
    return {'url': url}
