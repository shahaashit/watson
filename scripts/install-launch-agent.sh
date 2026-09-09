#!/usr/bin/env bash
# Install/update only Watson's own localhost LaunchAgent. The spawned service
# uses env -i, so broad launchd-session variables (including API tokens) never
# become inherited process environment.
set -euo pipefail

LABEL="com.watson.local"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="$ROOT/backend/.venv"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
LOG_DIR="$HOME/.watson"
DOMAIN="gui/$(id -u)"
PORT=8000
SERVICE_WAS_LOADED=false
TMP_PLIST=""
PREVIOUS_PLIST=""

fail() { echo "error: $*" >&2; exit 1; }
xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"; }
xml() { printf '%s' "$1" | xml_escape; }

cleanup() {
  [[ -n "$TMP_PLIST" ]] && rm -f "$TMP_PLIST"
  [[ -n "$PREVIOUS_PLIST" ]] && rm -f "$PREVIOUS_PLIST"
}
trap cleanup EXIT

[[ "$(uname -s)" == "Darwin" ]] || fail "LaunchAgent installation is supported on macOS only."
command -v launchctl >/dev/null 2>&1 || fail "launchctl is required."
command -v curl >/dev/null 2>&1 || fail "curl is required for the local health check."
command -v lsof >/dev/null 2>&1 || fail "lsof is required to protect port $PORT."
command -v plutil >/dev/null 2>&1 || fail "plutil is required to validate the LaunchAgent plist."
[[ -x "$VENV/bin/python" ]] || fail "Backend environment is missing; run ./scripts/install.sh first."
[[ -f "$ROOT/frontend/dist/index.html" ]] || fail "Frontend build is missing; run ./scripts/install.sh first."

service_pid() {
  local details
  details="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null)" || return 1
  printf '%s\n' "$details" | sed -nE 's/^[[:space:]]*pid = ([0-9]+);?[[:space:]]*$/\1/p' | head -n 1
}

listener_pids() {
  lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

assert_port_ownership() {
  local loaded_pid listeners listener
  loaded_pid="$(service_pid || true)"
  listeners="$(listener_pids)"
  [[ -z "$listeners" ]] && return 0
  [[ -n "$loaded_pid" ]] || fail "Port $PORT is in use by an unrelated process; Watson was not stopped."
  while IFS= read -r listener; do
    [[ -z "$listener" ]] && continue
    [[ "$listener" == "$loaded_pid" ]] || fail "Port $PORT is not owned by $LABEL; Watson was not stopped."
  done <<< "$listeners"
}

wait_for_port_free() {
  local _
  for _ in $(seq 1 15); do
    [[ -z "$(listener_pids)" ]] && return 0
    sleep 1
  done
  return 1
}

health_is_watson() {
  local response
  response="$(curl --fail --silent --show-error --max-time 1 "http://127.0.0.1:$PORT/api/health")" || return 1
  printf '%s' "$response" | "$VENV/bin/python" -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if isinstance(value, dict) and value.get("ok") is True else 1)
'
}

wait_for_health() {
  local _
  for _ in $(seq 1 30); do
    health_is_watson && return 0
    sleep 1
  done
  return 1
}

restore_previous_service() {
  # This function is invoked only after this script has stopped its own label.
  # It never touches a process found merely by a port scan.
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  if [[ -n "$PREVIOUS_PLIST" && -f "$PREVIOUS_PLIST" ]]; then
    local restore_tmp
    restore_tmp="$(mktemp "$AGENT_DIR/.${LABEL}.restore.XXXXXX")"
    cp "$PREVIOUS_PLIST" "$restore_tmp"
    chmod 644 "$restore_tmp"
    mv -f "$restore_tmp" "$PLIST"
    if [[ "$SERVICE_WAS_LOADED" == true ]]; then
      launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
      launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    fi
  else
    rm -f "$PLIST"
  fi
}

mkdir -p "$AGENT_DIR" "$LOG_DIR"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  SERVICE_WAS_LOADED=true
fi
assert_port_ownership

if [[ "$SERVICE_WAS_LOADED" == true && ! -f "$PLIST" ]]; then
  fail "Existing Watson service has no plist at the expected path; no service was stopped."
fi

if [[ -f "$PLIST" ]]; then
  PREVIOUS_PLIST="$(mktemp "$AGENT_DIR/.${LABEL}.previous.XXXXXX")"
  cp "$PLIST" "$PREVIOUS_PLIST"
fi
TMP_PLIST="$(mktemp "$AGENT_DIR/.${LABEL}.XXXXXX")"

cat > "$TMP_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$(xml "$LABEL")</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/env</string><string>-i</string>
    <string>HOME=$(xml "$HOME")</string><string>PATH=/usr/bin:/bin</string>
    <string>$(xml "$VENV/bin/python")</string><string>-m</string><string>uvicorn</string>
    <string>app.main:app</string><string>--host</string><string>127.0.0.1</string><string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$ROOT/backend")</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(xml "$LOG_DIR/server.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG_DIR/server.log")</string>
</dict></plist>
EOF
chmod 644 "$TMP_PLIST"
plutil -lint "$TMP_PLIST" >/dev/null || fail "Generated LaunchAgent plist is invalid."

if [[ "$SERVICE_WAS_LOADED" == true ]]; then
  if ! launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    fail "Could not stop the existing Watson service; no files were replaced."
  fi
  if ! wait_for_port_free; then
    restore_previous_service
    fail "Port $PORT did not free after stopping Watson; restored the previous service."
  fi
fi

mv -f "$TMP_PLIST" "$PLIST"
TMP_PLIST=""
if ! launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
  restore_previous_service
  fail "Could not start the new Watson service; restored the previous service."
fi
if ! launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  restore_previous_service
  fail "Could not kickstart the new Watson service; restored the previous service."
fi
if ! wait_for_health; then
  restore_previous_service
  fail "Watson did not pass its health check within 30 seconds; restored the previous service."
fi

open http://127.0.0.1:8000/onboarding
echo "Watson LaunchAgent installed → http://127.0.0.1:8000/onboarding"
