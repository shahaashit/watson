"""Import application setup or export a private colleague setup file."""
import argparse
import json
import os
from pathlib import Path

from .. import db
from ..services import app_settings, secret_store, setup_bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['import', 'export'])
    parser.add_argument('path')
    parser.add_argument('--gitlab-client-id')
    parser.add_argument('--allow-http', action='store_true', help='Explicitly allow an HTTP-only GitLab instance')
    args = parser.parse_args()
    db.init_db()
    conn = db.connect()
    try:
        if args.operation == 'import':
            setup_bundle.import_file(conn, args.path)
            print('Application setup imported. Connect your accounts in onboarding.')
        else:
            from ..config import settings
            data = {'version': 1, 'gitlab': {
                'base_url': app_settings.get(conn, 'integration.gitlab.base_url', settings.gitlab_base_url),
                'client_id': args.gitlab_client_id or app_settings.get(conn, 'integration.gitlab.oauth_client_id', ''),
                'allow_http': args.allow_http or app_settings.get(conn, 'integration.gitlab.oauth_allow_http', False),
            }, 'google_calendar': json.loads(secret_store.get_secret('google.client_config'))}
            clickup_config = secret_store.get_secret('clickup.client_config')
            if clickup_config:
                data['clickup'] = json.loads(clickup_config)
            setup_bundle.validate(data)
            target = Path(args.path).expanduser()
            # Exclusive creation prevents overwriting an existing private file.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, indent=2)
                stream.write('\n')
            print('Private setup file created; no personal access or refresh tokens included.')
    except Exception:
        print('Setup operation failed. Check file structure and local credential storage.')
        raise SystemExit(1) from None
    finally:
        conn.close()


if __name__ == '__main__':
    main()
