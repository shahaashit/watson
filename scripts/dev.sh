#!/usr/bin/env bash
# Run Watson backend (uvicorn, reload) + frontend (vite) together.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/backend/.venv"

if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "No venv found — run: python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

cd "$ROOT/backend"
"$VENV/bin/uvicorn" app.main:app --reload --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null' EXIT

cd "$ROOT/frontend"
npm run dev
