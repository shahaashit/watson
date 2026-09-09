"""Local GitLab PKCE authorization and serialized refresh; tokens stay in Keychain."""
import base64
import hashlib
import json
import secrets
import threading
import time
import fcntl
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from . import app_settings, secret_store

SECRET = 'gitlab.oauth'
REDIRECT_URI = 'http://127.0.0.1:18765/callback'
_lock = threading.RLock()
_generation = 0
_sessions = {}
_local = threading.local()


@contextmanager
def credential_lock():
    """Serialize Keychain mutations across threads AND installer processes."""
    from ..config import settings
    with _lock:
        if getattr(_local, 'depth', 0):
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with (settings.data_dir / 'oauth.lock').open('a') as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            _local.depth = 1
            try:
                yield
            finally:
                _local.depth = 0
                fcntl.flock(file, fcntl.LOCK_UN)


class OAuthError(RuntimeError):
    pass


def config(conn):
    return {
        'base_url': app_settings.get(conn, 'integration.gitlab.base_url', ''),
        'client_id': app_settings.get(conn, 'integration.gitlab.oauth_client_id', ''),
        'allow_http': app_settings.get(conn, 'integration.gitlab.oauth_allow_http', False),
    }


def validate_config(cfg):
    u = urlsplit(cfg['base_url'])
    if (not u.hostname or u.username or u.password or u.query or u.fragment
            or u.scheme not in ('http', 'https')
            or (u.scheme == 'http' and cfg.get('allow_http') is not True)
            or not cfg.get('client_id')):
        raise OAuthError('Invalid OAuth configuration')


def invalidate():
    global _generation
    from ..db import connect
    with credential_lock():
        conn = connect()
        try:
            _generation = app_settings.get(conn, 'integration.gitlab.oauth_generation', 0) + 1
            app_settings.set_value(conn, 'integration.gitlab.oauth_generation', _generation)
        finally:
            conn.close()
        for record in _sessions.values():
            if record['status'] == 'pending':
                record['status'] = 'failed'
                record['server'].server_close()


def _record():
    raw = secret_store.get_secret(SECRET)
    return json.loads(raw) if raw else {}


def view(conn):
    cfg = config(conn)
    try:
        record = _record()
        present = bool(record.get('access_token'))
        reconnect = bool(record.get('reauth_required')) or (present and (
            record.get('base_url') != cfg['base_url'] or record.get('client_id') != cfg['client_id']))
    except Exception:
        present, reconnect = False, True
    return {'oauth_available': bool(cfg['client_id']), 'oauth_connected': present,
            'reauth_required': reconnect, 'oauth_http': cfg['base_url'].startswith('http://')}


def _token_request(cfg, payload):
    with requests.Session() as session:
        response = session.post(cfg['base_url'].rstrip('/') + '/oauth/token',
            data={'client_id': cfg['client_id'], **payload}, timeout=20, allow_redirects=False)
    if response.status_code != 200:
        raise OAuthError('reauth' if response.status_code in (400, 401, 403) else 'temporary')
    data = response.json()
    if (not isinstance(data.get('access_token'), str) or not data['access_token']
            or not isinstance(data.get('refresh_token'), str) or not data['refresh_token']
            or str(data.get('token_type', '')).lower() != 'bearer'):
        raise OAuthError('Invalid token response')
    expiry = int(data.get('expires_in', 0))
    if expiry <= 0:
        raise OAuthError('Invalid token expiry')
    return {**cfg, 'access_token': data['access_token'], 'refresh_token': data['refresh_token'],
            'expires_at': time.time() + expiry, 'reauth_required': False}


def access_token(base_url):
    """Refresh once across parallel sync workers. Never fall back to an old PAT."""
    from ..db import connect
    with credential_lock():
        conn = connect()
        try:
            cfg = config(conn)
            pat_blocked = app_settings.get(conn, 'integration.gitlab.pat_blocked', False)
            if app_settings.get(conn, 'integration.gitlab.disabled', False):
                return ''
        finally:
            conn.close()
        record = _record()
        if not record:
            return '' if pat_blocked else None
        if (cfg['base_url'] != base_url or record.get('base_url') != base_url or record.get('client_id') != cfg['client_id']
                or record.get('reauth_required')):
            return ''
        if record.get('expires_at', 0) > time.time() + 60:
            return record['access_token']
        validate_config(cfg)
        try:
            refreshed = _token_request(cfg, {'grant_type': 'refresh_token', 'refresh_token': record['refresh_token']})
        except OAuthError as error:
            if str(error) == 'reauth':
                secret_store.set_secret(SECRET, json.dumps({**record, 'reauth_required': True}))
            raise OAuthError('GitLab connection needs attention') from None
        secret_store.set_secret(SECRET, json.dumps(refreshed))
        return refreshed['access_token']


def mark_unauthorized(token):
    with credential_lock():
        record = _record()
        if record.get('access_token') == token:
            secret_store.set_secret(SECRET, json.dumps({**record, 'reauth_required': True}))


def _complete(cfg, code, verifier, generation):
    from ..db import connect
    tokens = _token_request(cfg, {'grant_type': 'authorization_code', 'code': code,
                                 'code_verifier': verifier, 'redirect_uri': REDIRECT_URI})
    response = requests.get(cfg['base_url'].rstrip('/') + '/api/v4/user',
        headers={'Authorization': 'Bearer ' + tokens['access_token']}, timeout=20, allow_redirects=False)
    if response.status_code != 200:
        raise OAuthError('Account verification failed')
    username = response.json().get('username')
    if not isinstance(username, str) or not username:
        raise OAuthError('Account verification failed')
    with credential_lock():
        conn = connect()
        try:
            if generation != app_settings.get(conn, 'integration.gitlab.oauth_generation', 0) or config(conn) != cfg:
                raise OAuthError('Configuration changed')
            # Disable until both storage boundaries have succeeded.
            app_settings.set_value(conn, 'integration.gitlab.disabled', True)
            secret_store.set_secret(SECRET, json.dumps(tokens))
            app_settings.set_value(conn, 'integration.gitlab.username', username, commit=False)
            app_settings.set_value(conn, 'integration.gitlab.disabled', False, commit=False)
            app_settings.set_value(conn, 'integration.gitlab.health', {'status': 'connected'}, commit=False)
            conn.commit()
        finally:
            conn.close()


def start(conn):
    cfg = config(conn)
    validate_config(cfg)
    with credential_lock():
        now = time.time()
        for key in list(_sessions):
            if _sessions[key]['deadline'] < now - 600:
                del _sessions[key]
        if any(s['status'] == 'pending' for s in _sessions.values()):
            raise OAuthError('Authorization already running')
        session_id = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        generation = app_settings.get(conn, 'integration.gitlab.oauth_generation', 0)
        record = {'status': 'pending', 'deadline': now + 300}
        authorization_url = cfg['base_url'].rstrip('/') + '/oauth/authorize?' + urlencode({
            'client_id': cfg['client_id'], 'redirect_uri': REDIRECT_URI, 'response_type': 'code',
            'scope': 'read_api', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                if (urlsplit(self.path).path != '/callback'
                        or len(query.get('state', [])) != 1
                        or not secrets.compare_digest(query['state'][0], state)):
                    self.send_error(400, 'Invalid authorization response')
                    return
                try:
                    if (time.time() > record['deadline'] or record['status'] != 'pending'
                            or 'error' in query or len(query.get('code', [])) != 1):
                        raise OAuthError('Authorization cancelled or expired')
                    _complete(cfg, query['code'][0], verifier, generation)
                    record['status'] = 'connected'
                except Exception:
                    record['status'] = 'failed'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.wfile.write(('GitLab connected. Return to Watson.' if record['status'] == 'connected'
                                 else 'Authorization failed. Return to Watson and try again.').encode())

        server = HTTPServer(('127.0.0.1', 18765), Handler)
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
                if record['status'] == 'pending':
                    record['status'] = 'failed'

        try:
            threading.Thread(target=run, daemon=True, name='gitlab-oauth').start()
        except Exception:
            server.server_close()
            record['status'] = 'failed'
            raise
        return {'session_id': session_id, 'authorization_url': authorization_url, 'status': 'pending'}


def status(session_id):
    with _lock:
        return {'status': _sessions[session_id]['status']}
