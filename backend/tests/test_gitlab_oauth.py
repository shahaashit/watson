import io
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services import app_settings, gitlab_client, gitlab_oauth as oauth, secret_store, settings_service, setup_bundle


@pytest.fixture
def cfg(conn, monkeypatch):
    monkeypatch.setattr(oauth, '_sessions', {})
    monkeypatch.setattr(oauth, '_generation', 0)
    cfg = {'base_url': 'https://gitlab.example.com', 'client_id': 'public-client', 'allow_http': False}
    app_settings.set_value(conn, 'integration.gitlab.base_url', cfg['base_url'])
    app_settings.set_value(conn, 'integration.gitlab.oauth_client_id', cfg['client_id'])
    return cfg


def token(cfg, **extra):
    return {**cfg, 'access_token': 'test-access', 'refresh_token': 'test-refresh',
            'expires_at': time.time() + 3600, 'reauth_required': False, **extra}


def test_bundle_import_preserves_google_grant_and_excludes_secrets_from_db(conn, cfg, tmp_path):
    google = {'installed': {'client_id': 'desktop', 'client_secret': 'local-test-secret',
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token'}}
    path = tmp_path / 'setup.json'
    path.write_text(json.dumps({'version': 1, 'gitlab': cfg, 'google_calendar': google}))
    setup_bundle.import_file(conn, path)
    secret_store.set_secret('google.authorized_user', 'existing-personal-grant')
    setup_bundle.import_file(conn, path)
    assert secret_store.get_secret('google.authorized_user') == 'existing-personal-grant'
    assert 'local-test-secret' not in json.dumps(app_settings.snapshot(conn))
    assert 'local-test-secret' not in json.dumps(settings_service.settings_snapshot(conn))
    assert oauth.view(conn)['oauth_available']


@pytest.mark.parametrize('bad', [
    {'version': 1, 'access_token': 'forbidden'},
    {'version': 1, 'google_calendar': {'web': {}}},
    {'version': 1, 'google_calendar': {'installed': {'client_id': 'a', 'client_secret': 'b',
        'auth_uri': 'https://attacker.example/auth', 'token_uri': 'https://attacker.example/token'}}},
    {'version': 1, 'gitlab': {'base_url': 'http://gitlab.example.com', 'client_id': 'a', 'allow_http': False}},
])
def test_invalid_bundle_is_rejected_before_mutation(conn, tmp_path, bad):
    before = app_settings.snapshot(conn)
    path = tmp_path / 'setup.json'
    path.write_text(json.dumps(bad))
    with pytest.raises((ValueError, oauth.OAuthError)):
        setup_bundle.import_file(conn, path)
    assert app_settings.snapshot(conn) == before


def test_http_requires_explicit_bundle_opt_in(cfg):
    oauth.validate_config({**cfg, 'base_url': 'http://gitlab.example.com', 'allow_http': True})
    with pytest.raises(oauth.OAuthError):
        oauth.validate_config({**cfg, 'base_url': 'http://gitlab.example.com'})


def test_expired_token_refreshes_once_and_uses_bearer(conn, cfg, monkeypatch):
    secret_store.set_secret(oauth.SECRET, json.dumps(token(cfg, expires_at=0)))
    calls = []
    def refresh(config, payload):
        calls.append(payload)
        return token(cfg, access_token='new-access')
    monkeypatch.setattr(oauth, '_token_request', refresh)
    assert oauth.access_token(cfg['base_url']) == 'new-access'
    assert oauth.access_token(cfg['base_url']) == 'new-access'
    assert len(calls) == 1
    resolved = gitlab_client.gitlab_config()
    assert gitlab_client._headers(resolved) == {'Authorization': 'Bearer new-access'}


def test_revoked_refresh_prompts_reconnect_without_pat_fallback(conn, cfg, monkeypatch):
    secret_store.set_secret(oauth.SECRET, json.dumps(token(cfg, expires_at=0)))
    secret_store.set_secret('gitlab.token', 'old-pat')
    def fail(*args):
        raise oauth.OAuthError('reauth')
    monkeypatch.setattr(oauth, '_token_request', fail)
    with pytest.raises(oauth.OAuthError):
        oauth.access_token(cfg['base_url'])
    assert oauth.view(conn)['reauth_required']
    assert gitlab_client.gitlab_config()['token'] == ''


def test_network_failure_does_not_invalidate_grant(conn, cfg, monkeypatch):
    secret_store.set_secret(oauth.SECRET, json.dumps(token(cfg, expires_at=0)))
    def fail(*args):
        raise oauth.requests.Timeout()
    monkeypatch.setattr(oauth, '_token_request', fail)
    with pytest.raises(oauth.requests.Timeout):
        oauth.access_token(cfg['base_url'])
    assert not oauth.view(conn)['reauth_required']


def test_old_request_401_does_not_invalidate_refreshed_token(conn, cfg):
    secret_store.set_secret(oauth.SECRET, json.dumps(token(cfg)))
    oauth.mark_unauthorized('obsolete-token')
    assert not oauth.view(conn)['reauth_required']
    oauth.mark_unauthorized('test-access')
    assert oauth.view(conn)['reauth_required']


def test_changed_host_never_receives_oauth_token(conn, cfg):
    secret_store.set_secret(oauth.SECRET, json.dumps(token(cfg)))
    app_settings.set_value(conn, 'integration.gitlab.base_url', 'https://different.example.com')
    assert oauth.access_token('https://different.example.com') == ''
    assert oauth.view(conn)['reauth_required']


def test_stale_caller_cannot_refresh_against_new_host(conn, cfg, monkeypatch):
    secret_store.set_secret(oauth.SECRET, json.dumps(token(cfg, expires_at=0)))
    app_settings.set_value(conn, 'integration.gitlab.base_url', 'https://different.example.com')
    monkeypatch.setattr(oauth, '_token_request', lambda *_: pytest.fail('must not send refresh token'))
    assert oauth.access_token(cfg['base_url']) == ''


def test_bundle_host_change_disables_existing_pat(conn, cfg, tmp_path):
    secret_store.set_secret('gitlab.token', 'old-host-only-pat')
    path = tmp_path / 'setup.json'
    path.write_text(json.dumps({'version': 1, 'gitlab': {**cfg, 'base_url': 'https://different.example.com'}}))
    setup_bundle.import_file(conn, path)
    assert gitlab_client.gitlab_config()['token'] == ''
    assert app_settings.get(conn, 'integration.gitlab.disabled') is True
    # Ordinary settings saves cannot revive a credential from the former host.
    settings_service.update_integration(conn, 'gitlab', settings_service.GitLabSettingsIn(
        base_url='https://different.example.com', username='example'))
    assert gitlab_client.gitlab_config()['token'] == ''


def test_callback_observes_persisted_invalidation_from_other_process(conn, cfg, monkeypatch):
    monkeypatch.setattr(oauth, '_token_request', lambda *_: token(cfg))
    class Response:
        status_code = 200
        def json(self): return {'username': 'example'}
    monkeypatch.setattr(oauth.requests, 'get', lambda *a, **kw: Response())
    # Another process changes the shared generation without touching our global.
    app_settings.set_value(conn, 'integration.gitlab.oauth_generation', 1)
    assert oauth._generation == 0
    with pytest.raises(oauth.OAuthError):
        oauth._complete(cfg, 'code', 'verifier', 0)
    assert not secret_store.get_secret(oauth.SECRET)


def test_invalidation_closes_pending_listener(conn, cfg, monkeypatch):
    closed = []
    class Server:
        def __init__(self, *args): pass
        def server_close(self): closed.append(True)
    class Thread:
        def __init__(self, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(oauth, 'HTTPServer', Server)
    monkeypatch.setattr(oauth.threading, 'Thread', Thread)
    first = oauth.start(conn)
    oauth.invalidate()
    assert closed == [True]
    assert oauth.status(first['session_id'])['status'] == 'failed'
    assert oauth.start(conn)['status'] == 'pending'


def test_disconnect_cancels_grant_but_preserves_shared_application(conn, cfg):
    secret_store.set_secret(oauth.SECRET, json.dumps(token(cfg)))
    settings_service.disconnect_integration(conn, 'gitlab')
    assert not secret_store.get_secret(oauth.SECRET)
    assert oauth.config(conn) == cfg
    assert oauth.access_token(cfg['base_url']) == ''


def test_callback_fails_if_settings_changed_during_exchange(conn, cfg, monkeypatch):
    monkeypatch.setattr(oauth, '_token_request', lambda *_: token(cfg))
    class Response:
        status_code = 200
        def json(self): return {'username': 'example'}
    monkeypatch.setattr(oauth.requests, 'get', lambda *a, **kw: Response())
    oauth.invalidate()
    with pytest.raises(oauth.OAuthError):
        oauth._complete(cfg, 'code', 'verifier', 0)
    assert not secret_store.get_secret(oauth.SECRET)


def test_callback_commits_only_after_account_verified(conn, cfg, monkeypatch):
    monkeypatch.setattr(oauth, '_token_request', lambda *_: token(cfg))
    class Response:
        status_code = 200
        def json(self): return {'username': 'example'}
    monkeypatch.setattr(oauth.requests, 'get', lambda *a, **kw: Response())
    oauth._complete(cfg, 'code', 'verifier', 0)
    assert json.loads(secret_store.get_secret(oauth.SECRET))['access_token'] == 'test-access'
    assert 'test-access' not in json.dumps(app_settings.snapshot(conn))
    assert app_settings.get(conn, 'integration.gitlab.username') == 'example'


def test_listener_checks_state_denial_and_single_active_flow(conn, cfg, monkeypatch):
    handlers = []
    class Server:
        def __init__(self, address, handler):
            assert address == ('127.0.0.1', 18765)
            handlers.append(handler)
        def server_close(self): pass
    class Thread:
        def __init__(self, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(oauth, 'HTTPServer', Server)
    monkeypatch.setattr(oauth.threading, 'Thread', Thread)
    result = oauth.start(conn)
    query = parse_qs(urlsplit(result['authorization_url']).query)
    assert query['code_challenge_method'] == ['S256']
    assert query['scope'] == ['read_api']
    assert 'code_verifier' not in result['authorization_url']
    with pytest.raises(oauth.OAuthError): oauth.start(conn)
    handler = object.__new__(handlers[0])
    handler.wfile = io.BytesIO()
    errors = []
    handler.send_error = lambda code, msg: errors.append(code)
    handler.send_response = lambda *args: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.path = '/callback?code=ignored&state=wrong'
    handler.do_GET()
    assert errors == [400]
    assert oauth.status(result['session_id'])['status'] == 'pending'
    handler.path = '/callback?error=access_denied&state=' + query['state'][0]
    handler.do_GET()
    assert oauth.status(result['session_id'])['status'] == 'failed'
    assert not secret_store.get_secret(oauth.SECRET)
