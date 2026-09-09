#!/usr/bin/env bash
# Rebuild the UI and restart the always-on Watson server (launchd agent).
# Run this after any code change so the running instance picks it up.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# rebuild the frontend so the backend serves the latest UI on :8000
( cd "$ROOT/frontend" && npm run build )

# restart the launchd-managed backend (kills + relaunches the service)
launchctl kickstart -k "gui/$(id -u)/com.watson.local"

echo "Watson restarted → http://127.0.0.1:8000"
