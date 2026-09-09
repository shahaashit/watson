import json
import pytest
from app.services import app_settings, secret_store, setup_bundle, settings_service, clickup_client


@pytest.fixture
def cfg(monkeypatch):
    from app.services import clickup_oauth
    monkeypatch.setattr(clickup_oauth, '_sessions', {})
    return {'client_id': 'example-app', 'client_secret': 'example-secret',
            'redirect_uri': 'http://127.0.0.1:18766/callback'}


def test_clickup_bundle_keeps_app_secret_out_of_database_and_responses(conn, cfg, tmp_path):
    path = tmp_path / 'setup.json'
    path.write_text(json.dumps({'version': 1, 'clickup': cfg}))
    setup_bundle.import_file(conn, path)
    assert json.loads(secret_store.get_secret('clickup.client_config')) == cfg
    assert 'example-secret' not in json.dumps(app_settings.snapshot(conn))
    assert 'example-secret' not in json.dumps(settings_service.settings_snapshot(conn))
    assert settings_service.integration_view(conn, 'clickup')['oauth_available']


def test_clickup_bundle_rejects_remote_callback(conn, cfg, tmp_path):
    path = tmp_path / 'setup.json'
    path.write_text(json.dumps({'version': 1, 'clickup': {**cfg, 'redirect_uri': 'https://evil.example/callback'}}))
    with pytest.raises(ValueError): setup_bundle.import_file(conn, path)
    assert not secret_store.get_secret('clickup.client_config')


def test_revoked_oauth_never_falls_back_to_pat(conn, cfg):
    from app.services import clickup_oauth as oauth
    secret_store.set_secret('clickup.client_config', json.dumps(cfg))
    secret_store.set_secret('clickup.token', 'old-pat')
    secret_store.set_secret('clickup.oauth', json.dumps({'client_id': cfg['client_id'], 'access_token': 'oauth-token', 'reauth_required': True}))
    assert clickup_client.clickup_config()['token'] == ''
    assert oauth.view(conn)['reauth_required']


def test_callback_verifies_user_and_clears_old_destination(conn, cfg, monkeypatch):
    from app.services import clickup_oauth as oauth
    secret_store.set_secret(oauth.APP_SECRET, json.dumps(cfg))
    app_settings.set_value(conn, 'integration.clickup.create_list_id', '99')
    class Response:
        status_code = 200
        def json(self): return {'access_token':'personal-token'}
    def post(url, **kwargs):
        assert url == 'https://api.clickup.com/api/v2/oauth/token'
        assert kwargs['json'] == {'client_id':'example-app','client_secret':'example-secret','code':'code'}
        assert kwargs['allow_redirects'] is False
        return Response()
    monkeypatch.setattr(oauth.requests, 'post', post)
    monkeypatch.setattr(oauth, '_read', lambda path, auth: {'user':{'id':1}})
    oauth._complete(cfg, 'code', 0)
    assert clickup_client.clickup_config()['token'] == 'Bearer personal-token'
    assert clickup_client.clickup_config()['create_list_id'] == ''
    assert 'personal-token' not in json.dumps(settings_service.settings_snapshot(conn))
    oauth.invalidate()
    with pytest.raises(ValueError): oauth._complete(cfg, 'code', 0)


def test_stale_unauthorized_response_does_not_revoke_new_token(conn, cfg):
    from app.services import clickup_oauth as oauth
    secret_store.set_secret(oauth.APP_SECRET, json.dumps(cfg))
    secret_store.set_secret(oauth.GRANT_SECRET, json.dumps({'client_id':cfg['client_id'],'access_token':'current'}))
    oauth.mark_unauthorized('Bearer obsolete')
    assert not oauth.view(conn)['reauth_required']
    oauth.mark_unauthorized('Bearer current')
    assert oauth.view(conn)['reauth_required']


@pytest.mark.parametrize('reader', ['task', 'workspace'])
@pytest.mark.parametrize('code,revoked', [('OAUTH_027', False), ('OAUTH_026', False), ('OAUTH_025', True)])
def test_workspace_denial_does_not_revoke_valid_login(conn, cfg, monkeypatch, reader, code, revoked):
    import requests
    from app.services import clickup_oauth as oauth
    secret_store.set_secret(oauth.APP_SECRET, json.dumps(cfg))
    secret_store.set_secret(oauth.GRANT_SECRET, json.dumps({'client_id': cfg['client_id'], 'access_token': 'current'}))
    response = requests.Response()
    response.status_code = 401
    response._content = json.dumps({'ECODE': code, 'err': 'private provider text'}).encode()
    monkeypatch.setattr(oauth.requests, 'get', lambda *args, **kwargs: response)
    if reader == 'task':
        with pytest.raises(requests.HTTPError):
            clickup_client.get_task('restricted')
    else:
        with pytest.raises(ValueError):
            oauth._read('/team/1/space', 'Bearer current')
    assert oauth.view(conn)['reauth_required'] is revoked


def test_destination_must_belong_to_authorized_workspace_and_space(conn, cfg, monkeypatch):
    from app.services import clickup_oauth as oauth
    secret_store.set_secret('clickup.token', 'pat')
    responses = {'/team':{'teams':[{'id':'1','name':'Workspace'}]},
        '/team/1/space?archived=false':{'spaces':[{'id':'2','name':'Space'}]},
        '/space/2/list?archived=false':{'lists':[{'id':'3','name':'List'}]},
        '/space/2/folder?archived=false':{'folders':[{'id':'4','name':'Folder'}]},
        '/folder/4/list?archived=false':{'lists':[{'id':'5','name':'Nested list'}]}}
    monkeypatch.setattr(oauth, '_read', lambda path, auth: responses[path])
    assert {row['id'] for row in oauth.lists('1','2')} == {'3','5'}
    for workspace, space, list_id in [('9','2','3'), ('1','9','3'), ('1','2','9')]:
        with pytest.raises(ValueError): oauth.save_destination(conn, workspace, space, list_id)
    assert oauth.save_destination(conn, '1','2','5')['create_list_id'] == '5'
    assert app_settings.get(conn, 'integration.clickup.workspace_id') == '1'


def test_listener_rejects_wrong_state_and_consumes_denial(conn, cfg, monkeypatch):
    import io
    from urllib.parse import parse_qs, urlsplit
    from app.services import clickup_oauth as oauth
    secret_store.set_secret(oauth.APP_SECRET,json.dumps(cfg))
    handlers=[]
    closed=[]
    class Server:
        def __init__(self,address,handler):
            assert address == ('127.0.0.1',18766)
            handlers.append(handler)
        def server_close(self): closed.append(True)
    class Thread:
        def __init__(self,**kwargs): pass
        def start(self): pass
    monkeypatch.setattr(oauth,'HTTPServer',Server)
    monkeypatch.setattr(oauth.threading,'Thread',Thread)
    first=oauth.start(conn)
    assert 'example-secret' not in first['authorization_url']
    with pytest.raises(ValueError): oauth.start(conn)
    query=parse_qs(urlsplit(first['authorization_url']).query)
    handler=object.__new__(handlers[0]); handler.wfile=io.BytesIO()
    errors=[]
    handler.send_error=lambda status,*_:errors.append(status)
    handler.send_response=lambda *_:None
    handler.send_header=lambda *_:None
    handler.end_headers=lambda:None
    handler.path='/callback?state=wrong&code=untrusted'
    handler.do_GET()
    assert errors == [400]
    assert oauth.status(first['session_id'])['status']=='pending'
    handler.path='/callback?state='+query['state'][0]+'&error=access_denied'
    handler.do_GET()
    assert oauth.status(first['session_id'])['status']=='failed'
    next_flow=oauth.start(conn)
    oauth.invalidate()
    assert oauth.status(next_flow['session_id'])['status']=='failed'
    assert closed


def test_disconnect_keeps_shared_app_but_removes_personal_grant(conn, cfg):
    secret_store.set_secret('clickup.client_config', json.dumps(cfg))
    secret_store.set_secret('clickup.oauth', json.dumps({'client_id':cfg['client_id'],'access_token':'token'}))
    settings_service.disconnect_integration(conn, 'clickup')
    assert secret_store.get_secret('clickup.client_config')
    assert not secret_store.get_secret('clickup.oauth')


def test_changed_application_invalidates_grant_and_destination(conn, cfg, tmp_path):
    path = tmp_path / 'setup.json'
    path.write_text(json.dumps({'version':1,'clickup':cfg}))
    setup_bundle.import_file(conn, path)
    secret_store.set_secret('clickup.oauth', json.dumps({'client_id':cfg['client_id'],'access_token':'token'}))
    app_settings.set_value(conn, 'integration.clickup.create_list_id', '9')
    setup_bundle.import_file(conn, path)
    assert secret_store.get_secret('clickup.oauth')
    path.write_text(json.dumps({'version':1,'clickup':{**cfg,'client_id':'different'}}))
    setup_bundle.import_file(conn, path)
    assert not secret_store.get_secret('clickup.oauth')
    assert app_settings.get(conn, 'integration.clickup.create_list_id') == ''
    assert clickup_client.clickup_config()['token'] == ''


@pytest.mark.parametrize(('grant_present', 'oauth_selected'), [(True, True), (False, True), (True, False)])
def test_manual_oauth_destination_change_is_rejected_without_side_effects(conn, cfg, grant_present, oauth_selected):
    from app.schemas import ClickUpSettingsIn
    secret_store.set_secret('clickup.client_config', json.dumps(cfg))
    app_settings.set_value(conn, 'integration.clickup.oauth_selected', oauth_selected)
    if grant_present:
        secret_store.set_secret('clickup.oauth', json.dumps({
            'client_id': cfg['client_id'], 'access_token': 'current',
        }))
    for key, value in [('workspace_id', '1'), ('space_id', '2'), ('create_list_id', '3')]:
        app_settings.set_value(conn, 'integration.clickup.' + key, value)
    before = app_settings.snapshot(conn)
    grant = secret_store.get_secret('clickup.oauth')
    with pytest.raises(ValueError, match='destination'):
        settings_service.update_integration(conn, 'clickup', ClickUpSettingsIn(create_list_id='99'))
    assert app_settings.snapshot(conn) == before
    assert secret_store.get_secret('clickup.oauth') == grant


@pytest.mark.parametrize('replacement_pat', [None, 'replacement-pat'])
def test_manual_oauth_save_allows_unchanged_list_or_explicit_pat(conn, cfg, replacement_pat):
    from app.schemas import ClickUpSettingsIn
    secret_store.set_secret('clickup.client_config', json.dumps(cfg))
    secret_store.set_secret('clickup.oauth', json.dumps({
        'client_id': cfg['client_id'], 'access_token': 'current',
    }))
    app_settings.set_value(conn, 'integration.clickup.oauth_selected', True)
    for key, value in [('workspace_id', '1'), ('space_id', '2'), ('create_list_id', '3')]:
        app_settings.set_value(conn, 'integration.clickup.' + key, value)
    list_id = '99' if replacement_pat else '3'
    settings_service.update_integration(conn, 'clickup', ClickUpSettingsIn(
        token=replacement_pat, create_list_id=list_id,
    ))
    result = clickup_client.clickup_config()
    assert result['create_list_id'] == list_id
    assert result['token'] == (replacement_pat or 'Bearer current')
    if replacement_pat:
        assert not secret_store.get_secret('clickup.oauth')
        assert app_settings.get(conn, 'integration.clickup.workspace_id') is None
        assert app_settings.get(conn, 'integration.clickup.space_id') is None
    else:
        assert app_settings.get(conn, 'integration.clickup.workspace_id') == '1'
        assert app_settings.get(conn, 'integration.clickup.space_id') == '2'


def test_config_does_not_mix_old_destination_with_reconnected_token(conn, cfg, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app import db
    from app.services import gitlab_oauth

    secret_store.set_secret('clickup.client_config', json.dumps(cfg))
    secret_store.set_secret('clickup.oauth', json.dumps({
        'client_id': cfg['client_id'], 'access_token': 'old-account',
    }))
    app_settings.set_value(conn, 'integration.clickup.create_list_id', '3')
    list_read, switching, switched = Event(), Event(), Event()
    original_read = app_settings.effective_nonsecret

    def reconnect():
        assert list_read.wait(2)
        switching.set()
        with gitlab_oauth.credential_lock():
            connection = db.connect()
            try:
                secret_store.set_secret('clickup.oauth', json.dumps({
                    'client_id': cfg['client_id'], 'access_token': 'new-account',
                }))
                app_settings.set_value(connection, 'integration.clickup.create_list_id', '')
            finally:
                connection.close()
        switched.set()

    def pause_after_list_read(key, fallback):
        value = original_read(key, fallback)
        if key == 'integration.clickup.create_list_id':
            list_read.set()
            assert switching.wait(2)
            # An unlocked reader lets the account switch finish here. A locked
            # snapshot makes the writer wait until both list and token are read.
            switched.wait(0.2)
        return value

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reconnect)
        with monkeypatch.context() as patch:
            patch.setattr(app_settings, 'effective_nonsecret', pause_after_list_read)
            snapshot = clickup_client.clickup_config()
        future.result(timeout=2)
    assert (snapshot['token'], snapshot['create_list_id']) == ('Bearer old-account', '3')
    after = clickup_client.clickup_config()
    assert (after['token'], after['create_list_id']) == ('Bearer new-account', '')


def test_missing_selected_oauth_grant_does_not_report_old_pat_as_configured(conn, cfg):
    secret_store.set_secret('clickup.client_config', json.dumps(cfg))
    secret_store.set_secret('clickup.token', 'old-pat')
    app_settings.set_value(conn, 'integration.clickup.oauth_selected', True)
    app_settings.set_value(conn, 'integration.clickup.create_list_id', '3')
    assert clickup_client.clickup_config()['token'] == ''
    view = settings_service.integration_view(conn, 'clickup')
    assert view['configured'] is False
    assert view['reconnect_required'] is True
