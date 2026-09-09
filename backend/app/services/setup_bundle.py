"""Validated application configuration import. Never imports user access tokens."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from . import app_settings, gitlab_oauth, secret_store


def validate(data):
    if not isinstance(data, dict) or set(data) - {'version', 'gitlab', 'google_calendar'} or data.get('version') != 1:
        raise ValueError('Invalid setup file')
    cfg = data.get('gitlab')
    if cfg is not None:
        if not isinstance(cfg, dict) or set(cfg) != {'base_url', 'client_id', 'allow_http'}:
            raise ValueError('Invalid GitLab application settings')
        if not isinstance(cfg['base_url'], str) or not isinstance(cfg['client_id'], str) or not isinstance(cfg['allow_http'], bool):
            raise ValueError('Invalid GitLab application settings')
        cfg = {**cfg, 'base_url': cfg['base_url'].rstrip('/').removesuffix('/api/v4')}
        gitlab_oauth.validate_config(cfg)
    google = data.get('google_calendar')
    if google is not None:
        if not isinstance(google, dict) or set(google) != {'installed'}:
            raise ValueError('Google requires a Desktop OAuth client')
        installed = google['installed']
        allowed = {'client_id', 'project_id', 'auth_uri', 'token_uri', 'auth_provider_x509_cert_url', 'client_secret', 'redirect_uris'}
        if not isinstance(installed, dict) or set(installed) - allowed:
            raise ValueError('Invalid Google client configuration')
        if not all(isinstance(installed.get(k), str) and installed[k] for k in ('client_id', 'client_secret')):
            raise ValueError('Missing Google client configuration')
        if installed.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth' or installed.get('token_uri') != 'https://oauth2.googleapis.com/token':
            raise ValueError('Unsupported Google OAuth endpoint')
        if any(urlsplit(uri).hostname not in ('localhost', '127.0.0.1', '::1') for uri in installed.get('redirect_uris', [])):
            raise ValueError('Google client must use local redirects')
    if cfg is None and google is None:
        raise ValueError('No application configuration supplied')
    return cfg, google


def import_file(conn, path):
    with gitlab_oauth.credential_lock():
        return _import_file(conn, path)


def _import_file(conn, path):
    source = Path(path).expanduser()
    if source.stat().st_size > 65536:
        raise ValueError('Setup file is too large')
    cfg, google = validate(json.loads(source.read_text()))
    # Validate the whole file before any changes. Reimports preserve user grants.
    gitlab_oauth.invalidate()
    previous_google = secret_store.get_secret('google.client_config') if google else None
    previous_user = secret_store.get_secret('google.authorized_user') if google else None
    try:
        conn.execute('BEGIN')
        if google:
            encoded = json.dumps(google)
            if previous_google and json.loads(previous_google) != google:
                secret_store.delete_secret('google.authorized_user')
            secret_store.set_secret('google.client_config', encoded)
        if cfg:
            from ..config import settings
            old_base = app_settings.get(conn, 'integration.gitlab.base_url', settings.gitlab_base_url)
            if old_base.rstrip('/').removesuffix('/api/v4') != cfg['base_url']:
                app_settings.set_value(conn, 'integration.gitlab.disabled', True, commit=False)
                app_settings.set_value(conn, 'integration.gitlab.pat_blocked', True, commit=False)
            app_settings.set_value(conn, 'integration.gitlab.base_url', cfg['base_url'], commit=False)
            app_settings.set_value(conn, 'integration.gitlab.oauth_client_id', cfg['client_id'], commit=False)
            app_settings.set_value(conn, 'integration.gitlab.oauth_allow_http', cfg['allow_http'], commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        if google:
            for name, old in [('google.client_config', previous_google), ('google.authorized_user', previous_user)]:
                if old:
                    secret_store.set_secret(name, old)
                else:
                    secret_store.delete_secret(name)
        raise
    return {'gitlab': cfg is not None, 'google_calendar': google is not None}
