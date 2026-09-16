import json
import re
from uuid import uuid4

import pytest
import requests

from app.services import gcal_client, secret_store, meet_links

MEET_SCOPE = 'https://www.googleapis.com/auth/meetings.space.created'
CAL_SCOPE = 'https://www.googleapis.com/auth/calendar.events'

def grant(scopes=None):
    value = {'token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
             'client_id': 'synthetic-client', 'client_secret': 'synthetic-secret',
             'token_uri': 'https://oauth2.googleapis.com/token', 'expiry': '2099-01-01T00:00:00Z',
             'scopes': scopes if scopes is not None else [CAL_SCOPE, MEET_SCOPE]}
    secret_store.set_secret('google.client_config', '{}')
    secret_store.set_secret('google.authorized_user', json.dumps(value))
    return value

@pytest.fixture(autouse=True)
def no_google_calls(monkeypatch):
    monkeypatch.setattr(requests, 'post', lambda *a, **k: pytest.fail('unexpected provider POST'))
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *a, **k: pytest.fail('unexpected provider call'))

def create(client, request_id):
    return client.post('/api/meet-links', json={'request_id': request_id})

def test_nickname_generation_without_credentials_and_idempotent_replay(client, conn, monkeypatch, caplog):
    monkeypatch.setattr(secret_store, 'get_secret', lambda *a: pytest.fail('nickname must not read credentials'))
    assert client.get('/api/meet-links/status').json()['ready'] is True
    request_id = str(uuid4())
    result = create(client, request_id)
    assert result.status_code == 200
    url = result.json()['url']
    assert re.fullmatch(r'https://g\.co/meet/w-(?:[0-9a-f]{4}-){3}[0-9a-f]{4}', url)
    assert create(client, request_id).json() == result.json()
    assert create(client, str(uuid4())).json()['url'] != url
    assert 'authuser' not in url
    rows = conn.execute("SELECT details_json FROM system_events WHERE kind='meet_link_create'").fetchall()
    assert len(rows) == 2
    assert url not in caplog.text + str([dict(row) for row in rows])

@pytest.mark.parametrize('old_status,code', [('in_progress', 'in_progress'), ('uncertain', 'uncertain')])
def test_legacy_ambiguous_requests_are_not_replaced(client, conn, old_status, code):
    request_id = str(uuid4())
    conn.execute("INSERT INTO meet_link_requests(request_id,status,error_code,created_at,updated_at) VALUES (?,?,?,'2000','2000')",
                 (request_id, old_status, None if old_status == 'in_progress' else code))
    conn.commit()
    assert create(client, request_id).json()['detail']['code'] == code

def test_legacy_success_replays_original_destination_without_email(client, conn):
    request_id = str(uuid4())
    conn.execute("INSERT INTO meet_link_requests(request_id,status,url,created_at,updated_at) VALUES (?,'succeeded',?,'2000','2000')",
                 (request_id, 'https://meet.google.com/abc-defg-hij'))
    conn.commit()
    assert create(client, request_id).json() == {'url': 'https://meet.google.com/abc-defg-hij'}

def test_failed_local_record_rolls_back_link_and_audit(client, conn, monkeypatch):
    monkeypatch.setattr(meet_links.events, 'record', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic-secret')))
    request_id = str(uuid4())
    result = create(client, request_id)
    assert result.status_code == 409
    assert 'synthetic-secret' not in result.text
    assert conn.execute('SELECT count(*) FROM meet_link_requests WHERE request_id=?', (request_id,)).fetchone()[0] == 0

def test_local_nickname_collision_is_regenerated(client, monkeypatch):
    tokens = iter(['1111222233334444', '1111222233334444', '5555666677778888'])
    monkeypatch.setattr(meet_links.secrets, 'token_hex', lambda size: next(tokens))
    first = create(client, str(uuid4())).json()['url']
    second = create(client, str(uuid4())).json()['url']
    assert first == 'https://g.co/meet/w-1111-2222-3333-4444'
    assert second == 'https://g.co/meet/w-5555-6666-7777-8888'

def test_real_audit_insert_failure_rolls_back_nickname(client, conn):
    conn.execute("CREATE TRIGGER block_meet_audit BEFORE INSERT ON system_events BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    conn.commit()
    try:
        request_id = str(uuid4())
        response = create(client, request_id)
        assert response.status_code == 409
        assert conn.execute('SELECT count(*) FROM meet_link_requests WHERE request_id=?', (request_id,)).fetchone()[0] == 0
    finally:
        conn.execute('DROP TRIGGER block_meet_audit')
        conn.commit()

@pytest.mark.parametrize('payload', [{}, {'request_id': 'bad'}, None])
def test_bad_request_uses_safe_error_contract(client, payload):
    response = client.post('/api/meet-links', json=payload)
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'failed'

@pytest.mark.parametrize('scopes', [[CAL_SCOPE], [CAL_SCOPE, MEET_SCOPE]])
@pytest.mark.parametrize('string_scopes', [False, True])
def test_calendar_refresh_preserves_granted_scopes_without_upgrade(monkeypatch, scopes, string_scopes):
    from google.oauth2.credentials import Credentials
    value = grant(scopes)
    if string_scopes:
        value['scopes'] = ' '.join(scopes)
    value['expiry'] = '2000-01-01T00:00:00Z'
    secret_store.set_secret('google.authorized_user', json.dumps(value))
    def refresh(self, request):
        assert self.scopes == scopes
        self.token = 'new-synthetic'
    monkeypatch.setattr(Credentials, 'refresh', refresh)
    gcal_client._load_credentials()
    assert json.loads(secret_store.get_secret('google.authorized_user'))['scopes'] == scopes


def test_partial_consent_stores_actual_not_requested_scopes(monkeypatch):
    from app.auth import gcal
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from requests_oauthlib import OAuth2Session
    grant()
    credentials = Credentials(token='synthetic', refresh_token='synthetic', token_uri='https://oauth2.googleapis.com/token',
                              client_id='synthetic', client_secret='synthetic', scopes=[CAL_SCOPE, MEET_SCOPE], granted_scopes=[CAL_SCOPE])
    class Flow:
        oauth2session = OAuth2Session('synthetic', scope=[CAL_SCOPE, MEET_SCOPE])
        def run_local_server(self, **kwargs): return credentials
    def flow(config, scopes):
        assert scopes == [CAL_SCOPE]
        return Flow()
    monkeypatch.setattr(InstalledAppFlow, 'from_client_config', flow)
    gcal.run_oauth_flow()
    assert json.loads(secret_store.get_secret('google.authorized_user'))['scopes'] == [CAL_SCOPE]



def test_oauth_partial_consent_is_accepted_by_token_parser(monkeypatch):
    from app.auth import gcal
    from google_auth_oauthlib.flow import InstalledAppFlow
    from requests_oauthlib import OAuth2Session
    from google_auth_oauthlib.helpers import credentials_from_session
    grant()
    class Flow:
        oauth2session = OAuth2Session('synthetic', scope=[CAL_SCOPE, MEET_SCOPE])
        def run_local_server(self, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({'access_token': 'synthetic', 'refresh_token': 'synthetic',
                                            'token_type': 'Bearer', 'expires_in': 3600, 'scope': CAL_SCOPE}).encode()
            for hook in self.oauth2session.compliance_hook['access_token_response']:
                response = hook(response)
            self.oauth2session._client.parse_request_body_response(response.text, scope=self.oauth2session.scope)
            self.oauth2session.token = self.oauth2session._client.token
            return credentials_from_session(self.oauth2session, {'client_id': 'synthetic', 'client_secret': 'synthetic', 'token_uri': 'https://oauth2.googleapis.com/token'})
    monkeypatch.setattr(InstalledAppFlow, 'from_client_config', lambda *a: Flow())
    gcal.run_oauth_flow()
    assert json.loads(secret_store.get_secret('google.authorized_user'))['scopes'] == [CAL_SCOPE]
