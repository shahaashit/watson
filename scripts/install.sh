#!/usr/bin/env bash
# Install the private localhost Watson Work OS on macOS. Configuration happens
# in the onboarding UI after this script starts the local LaunchAgent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="$ROOT/backend/.venv"
COMPAT_FILE="${WATSON_INSTALL_COMPAT_FILE:-$ROOT/.watson-install.env}"
SETUP_FILE="${WATSON_SETUP_FILE:-$ROOT/.watson-setup.json}"

fail() { echo "error: $*" >&2; exit 1; }
INSTALL_STARTED=$SECONDS
timed() {
  local label="$1" started=$SECONDS
  shift
  echo "==> $label"
  "$@"
  echo "    Finished in $((SECONDS - started))s"
}

compatible_python() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys, venv, ensurepip
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

if [[ $# -gt 0 ]]; then
  [[ $# -eq 2 && "$1" == "--setup" ]] || fail "Usage: ./scripts/install.sh [--setup /path/to/.watson-setup.json]"
  [[ -f "$2" ]] || fail "Setup file not found."
  SETUP_FILE="$(cd "$(dirname "$2")" && pwd -P)/$(basename "$2")"
fi
[[ -z "${WATSON_SETUP_FILE:-}" || -f "$SETUP_FILE" ]] || fail "Setup file not found."
if [[ -f "$SETUP_FILE" ]]; then
  SETUP_FILE="$(cd "$(dirname "$SETUP_FILE")" && pwd -P)/$(basename "$SETUP_FILE")"
fi

[[ "$(uname -s)" == "Darwin" ]] || fail "Watson's bootstrap currently supports macOS only."
command -v npm >/dev/null 2>&1 || fail "Node 18+ and npm are required."
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] && (( NODE_MAJOR >= 18 )) || fail "Node 18+ is required."
if [[ -e "$VENV" ]] && ! compatible_python "$VENV/bin/python"; then
  fail "Existing backend/.venv is incompatible. Move it aside and rerun; it has not been deleted."
fi
PYTHON=""
for candidate in "$VENV/bin/python" python3 python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && compatible_python "$candidate"; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  command -v brew >/dev/null 2>&1 || fail "Python 3.10+ with venv is required. Install Python from python.org (or Homebrew), then rerun. No system Python was modified."
  timed "Installing missing Python with Homebrew" brew install python@3.12
  PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
  compatible_python "$PYTHON" || fail "The installed Python is not compatible."
fi
echo "==> Using Python: $PYTHON"

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
  "$PYTHON" -m venv "$VENV"
fi

PIP_ARGS=()
[[ -n "${PIP_INDEX_URL:-}" ]] && PIP_ARGS+=(--index-url "$PIP_INDEX_URL")
[[ -n "${PIP_TRUSTED_HOST:-}" ]] && PIP_ARGS+=(--trusted-host "$PIP_TRUSTED_HOST")

# Bash 3.2 treats an empty array as unset under nounset. Expand it only when
# populated, preserving each argument without passing an empty string to pip.
if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  timed "Bootstrapping missing pip in Watson's environment" "$VENV/bin/python" -m ensurepip
fi
BACKEND_HASH="$(shasum -a 256 "$ROOT/backend/requirements.txt")"
BACKEND_STAMP="$VENV/.watson-requirements"
if [[ -f "$BACKEND_STAMP" && "$(< "$BACKEND_STAMP")" == "$BACKEND_HASH" ]] && "$VENV/bin/python" "$ROOT/scripts/check-python-deps.py" "$ROOT/backend/requirements.txt" >/dev/null 2>&1; then
  echo "==> Backend dependencies unchanged; reusing environment"
else
  timed "Installing backend dependencies" "$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/backend/requirements.txt" ${PIP_ARGS[@]+"${PIP_ARGS[@]}"}
  "$VENV/bin/python" -m pip check
  printf '%s\n' "$BACKEND_HASH" > "$BACKEND_STAMP"
fi
# Chromium is not required for GitLab, ClickUp or Google OAuth. Developer
# browser tests may install it explicitly; normal onboarding does not.

pushd "$ROOT/frontend" >/dev/null
FRONTEND_HASH="$(shasum -a 256 package.json package-lock.json) / node $NODE_MAJOR"
FRONTEND_STAMP="$VENV/.watson-frontend-dependencies"
if [[ -d node_modules && -f "$FRONTEND_STAMP" && "$(< "$FRONTEND_STAMP")" == "$FRONTEND_HASH" ]] && npm ls --depth=0 >/dev/null 2>&1; then
  echo "==> Frontend dependencies unchanged; reusing node_modules"
else
  timed "Installing frontend dependencies" npm ci --prefer-offline --no-audit --no-fund
  printf '%s\n' "$FRONTEND_HASH" > "$FRONTEND_STAMP"
  # Dependency reinstalls must rebuild even when source files are unchanged.
  rm -f "$VENV/.watson-frontend-build"
fi
BUILD_HASH="$(find . -type d \( -name node_modules -o -name dist \) -prune -o -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256)"
BUILD_STAMP="$VENV/.watson-frontend-build"
OUTPUT_STAMP="$VENV/.watson-frontend-output"
OUTPUT_HASH=""
if [[ -d dist ]]; then
  OUTPUT_HASH="$(find dist -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256)"
fi
if [[ -f dist/index.html && -f "$BUILD_STAMP" && "$(< "$BUILD_STAMP")" == "$BUILD_HASH" && -f "$OUTPUT_STAMP" && "$(< "$OUTPUT_STAMP")" == "$OUTPUT_HASH" ]]; then
  echo "==> Frontend unchanged; reusing build"
else
  timed "Building frontend" npm run build
  printf '%s\n' "$BUILD_HASH" > "$BUILD_STAMP"
  find dist -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 > "$OUTPUT_STAMP"
fi
popd >/dev/null

echo "==> Installing the local Watson service"
if [[ -f "$SETUP_FILE" ]]; then
  (cd "$ROOT/backend" && "$VENV/bin/python" -m app.auth.setup_bundle import "$SETUP_FILE")
fi
"$ROOT/scripts/install-launch-agent.sh"

echo "Watson is ready. Finish private configuration in the onboarding screen."
echo "Installation finished in $((SECONDS - INSTALL_STARTED))s."
