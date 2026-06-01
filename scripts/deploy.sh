#!/usr/bin/env bash
set -euo pipefail
# Deploy helper: start Gunicorn (if needed) and open a reverse SSH tunnel via localhost.run
# Does not modify any app files or styles.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8050}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_WORKERS="${DASHBOARD_WORKERS:-1}"
DASHBOARD_THREADS="${DASHBOARD_THREADS:-4}"
TUNNEL_REMOTE_PORT="${TUNNEL_REMOTE_PORT:-80}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/tunnel.log}"
GUNICORN_LOG="${GUNICORN_LOG:-/tmp/gunicorn.log}"
URL_FILE="${URL_FILE:-$ROOT_DIR/dashboard_url.txt}"

FORCE_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --force|--restart)
      FORCE_RESTART=1
      ;;
  esac
done

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
if ! pgrep -f "gunicorn .*dashboard.dash_app:server.*:${PORT}\\b" >/dev/null 2>&1; then
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
  echo "Gunicorn started (logs: ${GUNICORN_LOG})"
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
    if curl -fsS "http://${DASHBOARD_HOST}:${PORT}/debug-page" >/dev/null 2>&1; then
      echo "Local healthcheck OK."
      break
    fi
  fi
  sleep 0.25
done

echo "Ensuring reverse SSH tunnel to localhost.run is running..."
KEY_PATH="$HOME/.ssh/localhost_run"
SSH_CMD="ssh -o ServerAliveInterval=60 -R ${TUNNEL_REMOTE_PORT}:localhost:${PORT} ssh.localhost.run"
if [ -f "$KEY_PATH" ]; then
  SSH_CMD="ssh -i $KEY_PATH -o ServerAliveInterval=60 -R ${TUNNEL_REMOTE_PORT}:localhost:${PORT} ssh.localhost.run"
fi

if ! pgrep -f "ssh .*localhost.run" >/dev/null 2>&1; then
  nohup sh -c "$SSH_CMD" > "${TUNNEL_LOG}" 2>&1 &
  sleep 1
  echo "Tunnel started (logs: ${TUNNEL_LOG})"
else
  echo "Tunnel already running"
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

tunnel_url="$(extract_url "${TUNNEL_LOG}")"
if [[ -n "${tunnel_url}" ]]; then
  echo "${tunnel_url}" > "${URL_FILE}" || true
  echo "Dashboard URL: ${tunnel_url}"
  echo "Saved: ${URL_FILE}"
else
  echo "Deployment script finished."
  echo "Open the tunnel URL from ${TUNNEL_LOG} (or re-run this script after a few seconds)."
fi
