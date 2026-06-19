#!/usr/bin/env bash
set -euo pipefail

# Keep the localhost.run reverse SSH tunnel alive. localhost.run can close free
# sessions; this wrapper restarts SSH and records each new public URL.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/load_local_env.sh"
load_dashboard_env
PORT="${PORT:-8050}"
TUNNEL_REMOTE_PORT="${TUNNEL_REMOTE_PORT:-80}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/tunnel.log}"
URL_FILE="${URL_FILE:-$ROOT_DIR/dashboard_url.txt}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/localhost_run}"
RESTART_DELAY="${TUNNEL_RESTART_DELAY:-10}"
MAX_RESTART_DELAY="${TUNNEL_MAX_RESTART_DELAY:-60}"
PUBLISH_PAGES_ON_URL="${PUBLISH_PAGES_ON_URL:-1}"
PUBLISH_PAGES_PUSH="${PUBLISH_PAGES_PUSH:-1}"
PUBLIC_TIMEOUT="${PUBLIC_TIMEOUT:-20}"
TUNNEL_PUBLIC_READY_TIMEOUT="${TUNNEL_PUBLIC_READY_TIMEOUT:-60}"

mkdir -p "$(dirname "${TUNNEL_LOG}")" "$(dirname "${URL_FILE}")"
url_marker="$(mktemp -t dashboard-tunnel-url.XXXXXX)"
trap 'rm -f "${url_marker}"' EXIT

build_ssh_args() {
  local args=(
    -T
    -o ExitOnForwardFailure=yes
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=2
    -R "${TUNNEL_REMOTE_PORT}:localhost:${PORT}"
    ssh.localhost.run
  )

  if [[ -f "${KEY_PATH}" ]]; then
    args=(
      -T
      -i "${KEY_PATH}"
      -o ExitOnForwardFailure=yes
      -o ServerAliveInterval=30
      -o ServerAliveCountMax=2
      -R "${TUNNEL_REMOTE_PORT}:localhost:${PORT}"
      ssh.localhost.run
    )
  fi

  printf '%s\n' "${args[@]}"
}

extract_url_from_line() {
  tr -d '\r' \
    | sed -r 's/\x1B\[[0-9;]*[A-Za-z]//g' \
    | grep -Eo 'https?://[a-z0-9.-]+[.]lhr[.]life' \
    | tail -n 1 || true
}

publish_pages_url() {
  if [[ "${PUBLISH_PAGES_ON_URL}" != "1" ]]; then
    return 0
  fi

  if [[ "${PUBLISH_PAGES_PUSH}" == "1" ]]; then
    "${ROOT_DIR}/scripts/publish_pages_link.sh" --push || true
  else
    "${ROOT_DIR}/scripts/publish_pages_link.sh" || true
  fi
}

public_dashboard_ready() {
  local url="$1"
  local debug_url
  local body
  local code

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

publish_ready_url() {
  local url="$1"
  local deadline

  deadline="$(( $(date +%s) + TUNNEL_PUBLIC_READY_TIMEOUT ))"
  while (( $(date +%s) < deadline )); do
    if public_dashboard_ready "${url}"; then
      printf '%s\n' "${url}" > "${URL_FILE}" || true
      printf '%s\n' "${url}" > "${url_marker}" || true
      echo "[$(date -Is)] tunnel URL ready: ${url}"
      publish_pages_url
      return 0
    fi
    sleep 2
  done

  echo "[$(date -Is)] tunnel URL not publicly ready within ${TUNNEL_PUBLIC_READY_TIMEOUT}s: ${url}"
  return 1
}

current_delay="${RESTART_DELAY}"
while :; do
  mapfile -t SSH_ARGS < <(build_ssh_args)
  echo "[$(date -Is)] starting localhost.run tunnel on local port ${PORT}"
  : > "${url_marker}"

  set +e
  ssh "${SSH_ARGS[@]}" 2>&1 | while IFS= read -r line; do
    printf '%s\n' "${line}" >> "${TUNNEL_LOG}"
    url="$(printf '%s\n' "${line}" | extract_url_from_line)"
    if [[ -n "${url}" ]]; then
      echo "[$(date -Is)] tunnel URL: ${url}"
      publish_ready_url "${url}" || true
    fi
  done
  status="${PIPESTATUS[0]}"
  set -e

  if [[ -s "${url_marker}" ]]; then
    current_delay="${RESTART_DELAY}"
  else
    current_delay="$(( current_delay * 2 ))"
    if (( current_delay > MAX_RESTART_DELAY )); then
      current_delay="${MAX_RESTART_DELAY}"
    fi
  fi

  echo "[$(date -Is)] tunnel exited with status ${status}; restarting in ${current_delay}s"
  sleep "${current_delay}"
done
