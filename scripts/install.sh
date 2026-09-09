#!/usr/bin/env bash
# Install the private localhost Watson Work OS on macOS. Configuration happens
# in the onboarding UI after this script starts the local LaunchAgent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="$ROOT/backend/.venv"
COMPAT_FILE="${WATSON_INSTALL_COMPAT_FILE:-$ROOT/.watson-install.env}"

fail() { echo "error: $*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "Watson's bootstrap currently supports macOS only."
command -v python3 >/dev/null 2>&1 || fail "Python 3.10+ is required."
command -v npm >/dev/null 2>&1 || fail "Node 18+ and npm are required."

python3 - <<'PY' || exit 1
import sys
if sys.version_info < (3, 10):
    raise SystemExit("error: Python 3.10+ is required.")
PY

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] && (( NODE_MAJOR >= 18 )) || fail "Node 18+ is required."

# Optional compatibility import for networks using a package mirror. Only
# these three non-runtime variables are recognized; API tokens in .env are
# deliberately neither required nor read by the installer.
if [[ -f "$COMPAT_FILE" ]]; then
  while IFS='=' read -r key value; do
    key="${key//[[:space:]]/}"
    [[ -z "$key" || "$key" == \#* ]] && continue
    case "$key" in
      PIP_INDEX_URL|PIP_TRUSTED_HOST|NPM_CONFIG_REGISTRY) export "$key=$value" ;;
    esac
  done < "$COMPAT_FILE"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "==> Creating Python environment"
  python3 -m venv "$VENV"
fi

PIP_ARGS=()
[[ -n "${PIP_INDEX_URL:-}" ]] && PIP_ARGS+=(--index-url "$PIP_INDEX_URL")
[[ -n "${PIP_TRUSTED_HOST:-}" ]] && PIP_ARGS+=(--trusted-host "$PIP_TRUSTED_HOST")

echo "==> Installing backend dependencies"
"$VENV/bin/python" -m pip install --upgrade pip "${PIP_ARGS[@]}"
"$VENV/bin/python" -m pip install -r "$ROOT/backend/requirements.txt" "${PIP_ARGS[@]}"

if "$VENV/bin/python" -c 'import playwright' >/dev/null 2>&1; then
  echo "==> Installing Chromium for optional local-browser integrations"
  "$VENV/bin/python" -m playwright install chromium
fi

echo "==> Installing and building the frontend"
pushd "$ROOT/frontend" >/dev/null
if [[ -n "${NPM_CONFIG_REGISTRY:-}" ]]; then
  npm install --registry="$NPM_CONFIG_REGISTRY"
else
  npm install
fi
npm run build
popd >/dev/null

echo "==> Installing the local Watson service"
"$ROOT/scripts/install-launch-agent.sh"

echo "Watson is ready. Finish private configuration in the onboarding screen."
