#!/usr/bin/env bash
set -euo pipefail

# Watch the local dashboard and public localhost.run URL. If the public tunnel
# dies, restart only the tunnel path; if the local dashboard dies, redeploy it.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/load_local_env.sh"
load_dashboard_env
PORT="${PORT:-8050}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
URL_FILE="${URL_FILE:-$ROOT_DIR/dashboard_url.txt}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/dashboard_watchdog.log}"
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-10}"
PUBLIC_TIMEOUT="${PUBLIC_TIMEOUT:-20}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/tunnel.log}"
TUNNEL_SUPERVISOR_LOG="${TUNNEL_SUPERVISOR_LOG:-/tmp/tunnel_supervisor.log}"
PUBLISH_PAGES_ON_URL="${PUBLISH_PAGES_ON_URL:-1}"
PUBLISH_PAGES_PUSH="${PUBLISH_PAGES_PUSH:-1}"
PAGES_URL="${PAGES_URL:-https://ai4betterair.github.io/current.json}"
PAGES_PUBLISH_RETRY_INTERVAL="${PAGES_PUBLISH_RETRY_INTERVAL:-300}"
TUNNEL_MAX_AGE_SECONDS="${TUNNEL_MAX_AGE_SECONDS:-3300}"
LAST_PAGES_PUBLISH_RETRY=0

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
  local debug_url
  local code
  local body

  [[ -n "${url}" ]] || return 1
  debug_url="${url%/}/debug-page"
  body="$(
    curl -k -L -s \
      --max-time "${PUBLIC_TIMEOUT}" \
      -w '\n%{http_code}' \
      "${debug_url}" 2>/dev/null || true
  )"
  code="$(printf '%s' "${body}" | tail -n 1)"
  body="$(printf '%s' "${body}" | sed '$d')"
  [[ "${code}" == "200" ]] || return 1
  grep -q "Dashboard server is reachable" <<< "${body}"
}

tunnel_url_age_seconds() {
  local modified_at

  [[ -f "${URL_FILE}" ]] || return 1
  modified_at="$(stat -c %Y "${URL_FILE}" 2>/dev/null || true)"
  [[ -n "${modified_at}" ]] || return 1
  echo "$(( $(date +%s) - modified_at ))"
}

tunnel_too_old() {
  local age

  if (( TUNNEL_MAX_AGE_SECONDS <= 0 )); then
    return 1
  fi

  age="$(tunnel_url_age_seconds || true)"
  [[ -n "${age}" ]] || return 1
  (( age >= TUNNEL_MAX_AGE_SECONDS ))
}

public_http_status() {
  local url="$1"

  curl -k -L -s -o /dev/null \
      -w '%{http_code}' \
      --max-time "${PUBLIC_TIMEOUT}" \
      "${url%/}/debug-page" 2>/dev/null || true
}

published_pages_url() {
  curl -fsS --max-time "${PUBLIC_TIMEOUT}" "${PAGES_URL}?ts=$(date +%s)" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url", ""))' 2>/dev/null || true
}

retry_pages_publish_if_stale() {
  local local_url="$1"
  local pages_url
  local now

  [[ "${PUBLISH_PAGES_ON_URL}" == "1" ]] || return 0
  [[ -n "${local_url}" ]] || return 0

  pages_url="$(published_pages_url)"
  if [[ "${pages_url}" == "${local_url}" ]]; then
    return 0
  fi

  now="$(date +%s)"
  if (( now - LAST_PAGES_PUBLISH_RETRY < PAGES_PUBLISH_RETRY_INTERVAL )); then
    return 0
  fi
  LAST_PAGES_PUBLISH_RETRY="${now}"

  log "GitHub Pages URL stale (${pages_url:-none}); retrying publish for ${local_url}"
  if [[ "${PUBLISH_PAGES_PUSH}" == "1" ]]; then
    "${ROOT_DIR}/scripts/publish_pages_link.sh" --push >> "${WATCHDOG_LOG}" 2>&1 || true
  else
    "${ROOT_DIR}/scripts/publish_pages_link.sh" >> "${WATCHDOG_LOG}" 2>&1 || true
  fi
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
  if tunnel_too_old; then
    log "tunnel age exceeded ${TUNNEL_MAX_AGE_SECONDS}s; rotating tunnel"
    restart_tunnel
    return
  fi

  if ! public_healthy "${url}"; then
    log "public debug-page unhealthy: ${url:-none} (HTTP $(public_http_status "${url}"))"
    restart_tunnel
    return
  fi

  retry_pages_publish_if_stale "${url}"

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
