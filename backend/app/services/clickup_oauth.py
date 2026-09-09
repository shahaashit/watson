"""ClickUp local authorization and read-only destination discovery.

App credentials and individual grants live separately in Keychain. No provider
response or callback URL is logged or returned as an error message.
"""
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from . import app_settings, secret_store
from .gitlab_oauth import credential_lock

APP_SECRET = 'clickup.client_config'
GRANT_SECRET = 'clickup.oauth'
REDIRECT_URI = 'http://127.0.0.1:18766/callback'
API = 'https://api.clickup.com/api/v2'
_sessions = {}


def validate_config(cfg):
    if (not isinstance(cfg, dict) or set(cfg) != {'client_id', 'client_secret', 'redirect_uri'}
            or any(not isinstance(cfg.get(k), str) or not cfg[k].strip() for k in cfg)
            or cfg['redirect_uri'] != REDIRECT_URI):
        raise ValueError('Invalid ClickUp application configuration')
    return cfg


def config():
    raw = secret_store.get_secret(APP_SECRET)
    return validate_config(json.loads(raw)) if raw else {}


def _grant():
    raw = secret_store.get_secret(GRANT_SECRET)
    return json.loads(raw) if raw else {}


def view(conn):
    try:
        cfg, grant = config(), _grant()
        connected = bool(grant.get('access_token'))
        reauth = bool(grant.get('reauth_required')) or (connected and grant.get('client_id') != cfg.get('client_id'))
    except Exception:
        cfg, connected, reauth = {}, False, True
    return {'oauth_available': bool(cfg), 'oauth_connected': connected,
            'reauth_required': reauth,
            'workspace_id': app_settings.get(conn, 'integration.clickup.workspace_id', ''),
            'space_id': app_settings.get(conn, 'integration.clickup.space_id', '')}


def invalidate():
    from ..db import connect
    with credential_lock():
        conn = connect()
        try:
            generation = app_settings.get(conn, 'integration.clickup.oauth_generation', 0) + 1
            app_settings.set_value(conn, 'integration.clickup.oauth_generation', generation)
        finally:
            conn.close()
        for record in _sessions.values():
            if record['status'] == 'pending':
                record['status'] = 'failed'
                record['server'].server_close()


def access_token():
    from ..db import connect
    with credential_lock():
        conn = connect()
        try:
            if app_settings.get(conn, 'integration.clickup.disabled', False):
                return ''
            selected = app_settings.get(conn, 'integration.clickup.oauth_selected', False)
        finally:
            conn.close()
        grant = _grant()
        if not grant:
            return '' if selected else None
        if grant.get('reauth_required') or grant.get('client_id') != config().get('client_id'):
            return ''
        return 'Bearer ' + grant['access_token']


def mark_unauthorized(authorization):
    if not authorization.startswith('Bearer '):
        return
    with credential_lock():
        grant = _grant()
        if grant.get('access_token') == authorization.removeprefix('Bearer '):
            secret_store.set_secret(GRANT_SECRET, json.dumps({**grant, 'reauth_required': True}))


def _read(path, authorization):
    response = requests.get(API + path, headers={'Authorization': authorization},
                            timeout=20, allow_redirects=False)
    if response.status_code == 401:
        mark_unauthorized(authorization)
    if response.status_code != 200:
        raise ValueError('ClickUp request failed. Check account permissions and retry.')
    return response.json()


def _id(value):
    value = str(value)
    if not value.isascii() or not value.isdigit():
        raise ValueError('Invalid ClickUp destination')
    return value


def _workspaces(auth):
    return [{'id': str(t['id']), 'name': t['name']} for t in _read('/team', auth).get('teams', [])]


def workspaces():
    from .clickup_client import clickup_config
    auth = clickup_config()['token']
    if not auth:
        raise ValueError('Connect ClickUp first')
    return _workspaces(auth)


def _spaces(workspace, auth):
    workspace = _id(workspace)
    if workspace not in {w['id'] for w in _workspaces(auth)}:
        raise ValueError('Workspace is not authorized')
    return [{'id':str(s['id']), 'name':s['name']} for s in _read(f'/team/{workspace}/space?archived=false', auth).get('spaces', [])]


def spaces(workspace):
    from .clickup_client import clickup_config
    return _spaces(workspace, clickup_config()['token'])


def _lists(workspace, space, auth):
    space = _id(space)
    if space not in {s['id'] for s in _spaces(workspace, auth)}:
        raise ValueError('Space is not in the authorized Workspace')
    rows = [{**row, 'folder_name': 'No folder'} for row in _read(f'/space/{space}/list?archived=false', auth).get('lists', [])]
    for folder in _read(f'/space/{space}/folder?archived=false', auth).get('folders', []):
        folder_id = _id(folder['id'])
        rows.extend({**row, 'folder_name': folder['name']} for row in _read(f'/folder/{folder_id}/list?archived=false', auth).get('lists', []))
    return [{'id':str(row['id']), 'name':row['name'], 'folder_name':row['folder_name']}
            for row in rows if not row.get('archived')]


def lists(workspace, space):
    from .clickup_client import clickup_config
    return _lists(workspace, space, clickup_config()['token'])


def save_destination(conn, workspace, space, list_id):
    from .clickup_client import clickup_config
    auth = clickup_config()['token']
    list_id = _id(list_id)
    if list_id not in {row['id'] for row in _lists(workspace, space, auth)}:
        raise ValueError('List is not in the selected Space')
    with credential_lock():
        if auth != clickup_config()['token']:
            raise ValueError('Account changed. Select the destination again.')
        for key, value in [('workspace_id', workspace), ('space_id', space), ('create_list_id', list_id)]:
            app_settings.set_value(conn, 'integration.clickup.' + key, str(value), commit=False)
        conn.commit()
    return {'workspace_id':str(workspace), 'space_id':str(space), 'create_list_id':list_id}


def _complete(cfg, code, generation):
    from ..db import connect
    response = requests.post(API + '/oauth/token', json={'client_id':cfg['client_id'],
        'client_secret':cfg['client_secret'], 'code':code}, timeout=20, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError('Token exchange failed')
    token = response.json().get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('Invalid token response')
    user = _read('/user', 'Bearer ' + token).get('user', {})
    if not user.get('id'):
        raise ValueError('Account verification failed')
    with credential_lock():
        conn = connect()
        try:
            if cfg != config() or generation != app_settings.get(conn, 'integration.clickup.oauth_generation', 0):
                raise ValueError('Connection was cancelled')
            app_settings.set_value(conn, 'integration.clickup.disabled', True)
            secret_store.set_secret(GRANT_SECRET, json.dumps({'client_id':cfg['client_id'],
                'access_token':token, 'user_id':str(user['id']), 'reauth_required':False}))
            # Explicit destination selection after every sign-in avoids writing
            # into a former account's List or a newly unauthorized Workspace.
            for key in ('workspace_id', 'space_id', 'create_list_id'):
                app_settings.set_value(conn, 'integration.clickup.' + key, '', commit=False)
            app_settings.set_value(conn, 'integration.clickup.oauth_selected', True, commit=False)
            app_settings.set_value(conn, 'integration.clickup.disabled', False, commit=False)
            app_settings.set_value(conn, 'integration.clickup.health', {'status':'connected'}, commit=False)
            conn.commit()
        finally:
            conn.close()


def start(conn):
    with credential_lock():
        cfg = config()
        validate_config(cfg)
        now = time.time()
        for key in list(_sessions):
            if _sessions[key]['deadline'] < now - 600:
                del _sessions[key]
        if any(record['status'] == 'pending' for record in _sessions.values()):
            raise ValueError('Sign-in already running')
        session_id, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        generation = app_settings.get(conn, 'integration.clickup.oauth_generation', 0)
        record = {'status':'pending', 'deadline':now + 300}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                if (urlsplit(self.path).path != '/callback' or len(query.get('state', [])) != 1
                        or not secrets.compare_digest(query['state'][0], state)):
                    self.send_error(400, 'Invalid authorization response')
                    return
                try:
                    if (record['status'] != 'pending' or time.time() > record['deadline']
                            or 'error' in query or len(query.get('code', [])) != 1):
                        raise ValueError('Authorization cancelled or expired')
                    _complete(cfg, query['code'][0], generation)
                    record['status'] = 'connected'
                except Exception:
                    record['status'] = 'failed'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.wfile.write(('ClickUp connected. Return to Watson to select your Workspace and List.'
                    if record['status'] == 'connected' else 'Authorization failed. Return to Watson and try again.').encode())

        server = HTTPServer(('127.0.0.1', 18766), Handler)
        server.timeout = 1
        record['server'] = server
        _sessions[session_id] = record

        def run():
            try:
                with server:
                    while record['status'] == 'pending' and time.time() < record['deadline']:
                        server.handle_request()
            except (OSError, ValueError):
                record['status'] = 'failed'
            finally:
                if record['status'] == 'pending': record['status'] = 'failed'
        try:
            threading.Thread(target=run, daemon=True, name='clickup-oauth').start()
        except Exception:
            server.server_close(); record['status'] = 'failed'
            raise
        return {'session_id':session_id, 'status':'pending', 'authorization_url':'https://app.clickup.com/api?' +
                urlencode({'client_id':cfg['client_id'], 'redirect_uri':REDIRECT_URI, 'state':state})}


def status(session_id):
    with credential_lock():
        return {'status':_sessions[session_id]['status']}
