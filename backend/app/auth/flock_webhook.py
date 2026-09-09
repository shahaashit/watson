"""Register / list / remove Flock Outgoing Webhook tokens.

Usage (from the backend/ directory — `app` isn't importable from repo root):
  cd backend
  .venv/bin/python -m app.auth.flock_webhook add <token> "<channel name>"
  .venv/bin/python -m app.auth.flock_webhook list
  .venv/bin/python -m app.auth.flock_webhook remove <token>

Setup flow for a new channel:
  1. dev.flock.com/webhooks/outgoing → Add Outgoing Webhook
  2. Pick channel, paste your ngrok URL + /api/flock/webhook as Callback URL,
     copy the token Flock generates, Save Settings
  3. `python -m app.auth.flock_webhook add <token> "channel name"`
  4. Send a test @-mention of yourself in that channel; verify it lands via
     `curl http://127.0.0.1:8000/api/flock/webhook/channels` (last_seen_at
     should be non-null within seconds).
"""
import sys

from ..db import connect, init_db
from ..services import flock_webhook_client


def _usage():
    print(__doc__, file=sys.stderr)
    sys.exit(2)


def main(argv):
    if len(argv) < 2:
        _usage()
    cmd = argv[1]
    init_db()  # make sure tables exist (first run on a fresh install)
    conn = connect()
    try:
        if cmd == "add":
            if len(argv) < 4:
                _usage()
            token, name = argv[2], argv[3]
            flock_webhook_client.register_channel(conn, token, name)
            print(f"registered token {token[:8]}... for channel {name!r}")
        elif cmd == "list":
            rows = flock_webhook_client.list_channels(conn)
            if not rows:
                print("(no channels registered)")
                return
            for r in rows:
                jid = r["channel_jid"] or "(not seen yet)"
                seen = r["last_seen_at"] or "(never)"
                print(f"{r['token'][:12]}...  {r['channel_name']!r:40}  jid={jid}  last={seen}")
        elif cmd == "remove":
            if len(argv) < 3:
                _usage()
            n = flock_webhook_client.unregister_channel(conn, argv[2])
            print(f"removed {n} token(s)")
        else:
            _usage()
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv)
