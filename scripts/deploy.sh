#!/usr/bin/env bash
set -euo pipefail
# Deploy helper: start Gunicorn (if needed) and open a reverse SSH tunnel via localhost.run
# Does not modify any app files or styles.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/load_local_env.sh"
load_dashboard_env
PORT="${PORT:-8050}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_WORKERS="${DASHBOARD_WORKERS:-2}"
DASHBOARD_THREADS="${DASHBOARD_THREADS:-8}"
TUNNEL_REMOTE_PORT="${TUNNEL_REMOTE_PORT:-80}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/tunnel.log}"
TUNNEL_SUPERVISOR_LOG="${TUNNEL_SUPERVISOR_LOG:-/tmp/tunnel_supervisor.log}"
GUNICORN_LOG="${GUNICORN_LOG:-/tmp/gunicorn.log}"
URL_FILE="${URL_FILE:-$ROOT_DIR/dashboard_url.txt}"
PUBLISH_PAGES_ON_URL="${PUBLISH_PAGES_ON_URL:-1}"
PUBLISH_PAGES_PUSH="${PUBLISH_PAGES_PUSH:-1}"
PUBLIC_TIMEOUT="${PUBLIC_TIMEOUT:-20}"

FORCE_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --force|--restart)
      FORCE_RESTART=1
      ;;
  esac
done

dashboard_healthy() {
  curl -fsS "http://${DASHBOARD_HOST}:${PORT}/debug-page" >/dev/null 2>&1
}

find_gunicorn_bin() {
  local candidates=()
  # Prefer dashboard-local venv first.
  if [[ -x "$ROOT_DIR/.venv/bin/gunicorn" ]]; then
    candidates+=("$ROOT_DIR/.venv/bin/gunicorn")
  fi
  # If a repo root venv exists (common in this project), try that too.
  if [[ -x "$ROOT_DIR/../../.venv/bin/gunicorn" ]]; then
    candidates+=("$ROOT_DIR/../../.venv/bin/gunicorn")
  fi
  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/gunicorn" ]]; then
    candidates+=("${VIRTUAL_ENV}/bin/gunicorn")
  fi
  if command -v gunicorn >/dev/null 2>&1; then
    candidates+=("gunicorn")
  fi

  local c
  for c in "${candidates[@]}"; do
    if "${c}" --version >/dev/null 2>&1; then
      echo "${c}"
      return 0
    fi
  done

  echo "Error: could not find gunicorn in a venv or on PATH." >&2
  echo "Hint: activate the dashboard venv or install gunicorn." >&2
  exit 127
}

echo "Starting Gunicorn (dashboard.dash_app:server) if not running..."
GUNICORN_BIN="$(find_gunicorn_bin)"
if [[ "${FORCE_RESTART}" == "1" ]]; then
  if pgrep -f "gunicorn .*dashboard.dash_app:server.*:${PORT}\\b" >/dev/null 2>&1; then
    echo "Stopping existing Gunicorn (--force)..."
    pkill -f "gunicorn .*dashboard.dash_app:server.*:${PORT}\\b" >/dev/null 2>&1 || true
    sleep 1
  fi
fi
if dashboard_healthy; then
  echo "Dashboard already reachable on http://${DASHBOARD_HOST}:${PORT}; reusing existing process."
elif ! pgrep -f "gunicorn .*dashboard.dash_app:server.*:${PORT}\\b" >/dev/null 2>&1; then
  "$GUNICORN_BIN" dashboard.dash_app:server \
    -b "${DASHBOARD_HOST}:${PORT}" \
    -w "${DASHBOARD_WORKERS}" \
    --threads "${DASHBOARD_THREADS}" \
    --worker-class gthread \
    --timeout 120 \
    --log-level info \
    --error-logfile "${GUNICORN_LOG}" \
    --access-logfile "${GUNICORN_LOG}.access" \
    --daemon
  echo "Gunicorn start requested (logs: ${GUNICORN_LOG})"
else
  echo "Gunicorn already running"
fi

echo "Waiting for local healthcheck..."
READY_TIMEOUT="${DASHBOARD_READY_TIMEOUT:-30}"
deadline="$(( $(date +%s) + READY_TIMEOUT ))"
while :; do
  now="$(date +%s)"
  if (( now >= deadline )); then
    echo "Warning: dashboard did not become reachable within ${READY_TIMEOUT}s at http://${DASHBOARD_HOST}:${PORT}/debug-page" >&2
    break
  fi
  if command -v curl >/dev/null 2>&1; then
    if dashboard_healthy; then
      echo "Local healthcheck OK."
      break
    fi
  fi
  sleep 0.25
done

echo "Ensuring reverse SSH tunnel to localhost.run is running..."
tunnel_supervisor_running() {
  pgrep -f "${ROOT_DIR}/scripts/tunnel_loop.sh" >/dev/null 2>&1
}

tunnel_ssh_running() {
  pgrep -f "^ssh .*localhost.run$" >/dev/null 2>&1
}

if [[ "${FORCE_RESTART}" == "1" ]]; then
  pkill -f "${ROOT_DIR}/scripts/tunnel_loop.sh" >/dev/null 2>&1 || true
  pkill -f "^ssh .*localhost.run$" >/dev/null 2>&1 || true
  sleep 1
fi

if ! tunnel_supervisor_running; then
  if tunnel_ssh_running; then
    echo "Tunnel already running directly (not supervised). Use scripts/stop.sh then scripts/deploy.sh to switch to auto-restart mode."
  else
    : > "${TUNNEL_LOG}"
    rm -f "${URL_FILE}" >/dev/null 2>&1 || true
    chmod +x "${ROOT_DIR}/scripts/tunnel_loop.sh" >/dev/null 2>&1 || true
    PUBLISH_PAGES_ON_URL="${PUBLISH_PAGES_ON_URL}" PUBLISH_PAGES_PUSH="${PUBLISH_PAGES_PUSH}" \
      setsid "${ROOT_DIR}/scripts/tunnel_loop.sh" > "${TUNNEL_SUPERVISOR_LOG}" 2>&1 < /dev/null &
    echo "Tunnel supervisor started (logs: ${TUNNEL_SUPERVISOR_LOG}, SSH log: ${TUNNEL_LOG})"
  fi
  sleep 1
else
  echo "Tunnel supervisor already running"
fi

extract_url() {
  local log_path="$1"
  if [[ -f "$log_path" ]]; then
    # localhost.run prints a bunch of help URLs first (twitter/docs/etc).
    # The actual tunnel URL is typically on a line like:
    #   <id>.lhr.life ... https://<id>.lhr.life
    # Prefer that pattern (and strip any ANSI control chars).
    tr -d '\r' <"$log_path" \
      | sed -r 's/\x1B\\[[0-9;]*[A-Za-z]//g' \
      | grep -Eo 'https?://[a-z0-9.-]+[.]lhr[.]life' \
      | tail -n 1 || true
  fi
}

public_dashboard_ready() {
  local url="$1"
  local body
  local code

  [[ -n "${url}" ]] || return 1
  body="$(
    curl -k -L -s \
      --max-time "${PUBLIC_TIMEOUT}" \
      -w '\n%{http_code}' \
      "${url%/}/debug-page" 2>/dev/null || true
  )"
  code="$(printf '%s' "${body}" | tail -n 1)"
  body="$(printf '%s' "${body}" | sed '$d')"
  [[ "${code}" == "200" ]] || return 1
  grep -q "Dashboard server is reachable" <<< "${body}"
}

tunnel_url="$(extract_url "${TUNNEL_LOG}")"
if [[ -z "${tunnel_url}" ]]; then
  TUNNEL_READY_TIMEOUT="${TUNNEL_READY_TIMEOUT:-60}"
  tunnel_deadline="$(( $(date +%s) + TUNNEL_READY_TIMEOUT ))"
  while [[ -z "${tunnel_url}" ]] && (( $(date +%s) < tunnel_deadline )); do
    sleep 1
    tunnel_url="$(extract_url "${TUNNEL_LOG}")"
  done
fi
if [[ -n "${tunnel_url}" ]]; then
  if public_dashboard_ready "${tunnel_url}" \
    && { [[ ! -f "${URL_FILE}" ]] || [[ "$(head -n 1 "${URL_FILE}" 2>/dev/null || true)" != "${tunnel_url}" ]]; }; then
    echo "${tunnel_url}" > "${URL_FILE}" || true
  fi
  echo "Dashboard URL: ${tunnel_url}"
  echo "Saved: ${URL_FILE}"
else
  echo "Deployment script finished."
  echo "Open the tunnel URL from ${TUNNEL_LOG} (or re-run this script after a few seconds)."
fi
