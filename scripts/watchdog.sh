#!/usr/bin/env bash
set -euo pipefail

# Watch the local dashboard and public localhost.run URL. If the public tunnel
# dies, restart only the tunnel path; if the local dashboard dies, redeploy it.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8050}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
URL_FILE="${URL_FILE:-$ROOT_DIR/dashboard_url.txt}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/dashboard_watchdog.log}"
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-60}"
PUBLIC_TIMEOUT="${PUBLIC_TIMEOUT:-20}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/tunnel.log}"
TUNNEL_SUPERVISOR_LOG="${TUNNEL_SUPERVISOR_LOG:-/tmp/tunnel_supervisor.log}"
PUBLISH_PAGES_ON_URL="${PUBLISH_PAGES_ON_URL:-1}"
PUBLISH_PAGES_PUSH="${PUBLISH_PAGES_PUSH:-1}"

cd "${ROOT_DIR}"
mkdir -p "$(dirname "${WATCHDOG_LOG}")"

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "${WATCHDOG_LOG}" >/dev/null
}

local_healthy() {
  curl -fsS "http://${DASHBOARD_HOST}:${PORT}/debug-page" >/dev/null 2>&1
}

tunnel_supervisor_running() {
  pgrep -f "${ROOT_DIR}/scripts/tunnel_loop.sh" >/dev/null 2>&1
}

tunnel_ssh_running() {
  pgrep -f "^ssh .*localhost.run$" >/dev/null 2>&1
}

current_url() {
  if [[ -f "${URL_FILE}" ]]; then
    head -n 1 "${URL_FILE}" || true
  fi
}

public_healthy() {
  local url="$1"
  local code

  [[ -n "${url}" ]] || return 1
  code="$(
    curl -k -L -s -o /dev/null \
      -w '%{http_code}' \
      --max-time "${PUBLIC_TIMEOUT}" \
      "${url}" 2>/dev/null || true
  )"
  [[ "${code}" =~ ^[23] ]]
}

restart_tunnel() {
  log "restarting localhost.run tunnel"
  pkill -f "${ROOT_DIR}/scripts/tunnel_loop.sh" >/dev/null 2>&1 || true
  pkill -f "^ssh .*localhost.run$" >/dev/null 2>&1 || true
  rm -f "${URL_FILE}" >/dev/null 2>&1 || true
  : > "${TUNNEL_LOG}"
  PUBLISH_PAGES_ON_URL="${PUBLISH_PAGES_ON_URL}" PUBLISH_PAGES_PUSH="${PUBLISH_PAGES_PUSH}" \
    setsid "${ROOT_DIR}/scripts/tunnel_loop.sh" > "${TUNNEL_SUPERVISOR_LOG}" 2>&1 < /dev/null &
}

redeploy_dashboard() {
  log "local dashboard unhealthy; running deploy"
  "${ROOT_DIR}/scripts/deploy.sh" >> "${WATCHDOG_LOG}" 2>&1 || true
}

run_once() {
  local url

  if ! local_healthy; then
    redeploy_dashboard
    return
  fi

  if ! tunnel_supervisor_running || ! tunnel_ssh_running; then
    log "tunnel process missing"
    restart_tunnel
    return
  fi

  url="$(current_url)"
  if ! public_healthy "${url}"; then
    log "public URL unhealthy: ${url:-none}"
    restart_tunnel
    return
  fi

  log "ok local=http://127.0.0.1:${PORT} public=${url}"
}

case "${1:-}" in
  --once)
    run_once
    ;;
  *)
    log "watchdog started; interval=${WATCHDOG_INTERVAL}s"
    while :; do
      run_once
      sleep "${WATCHDOG_INTERVAL}"
    done
    ;;
esac
