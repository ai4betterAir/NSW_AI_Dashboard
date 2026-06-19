#!/usr/bin/env bash
set -euo pipefail

# Cron-safe safety net for the public dashboard. It starts the dashboard,
# tunnel supervisor, and watchdog if any of them are missing, but avoids
# interrupting a healthy tunnel.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="${DASHBOARD_KEEPALIVE_LOG:-/tmp/dashboard_keepalive.log}"
LOCK_FILE="${DASHBOARD_KEEPALIVE_LOCK:-/tmp/dashboard_keepalive.lock}"

mkdir -p "$(dirname "${LOG_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  printf '[%s] keepalive already running; exiting\n' "$(date -Is)" >> "${LOG_FILE}"
  exit 0
fi

cd "${ROOT_DIR}"

{
  printf '\n[%s] keepalive start\n' "$(date -Is)"
  FORCE_TUNNEL_RESTART=0 \
    PUBLISH_PAGES_ON_URL=1 \
    PUBLISH_PAGES_PUSH=1 \
    scripts/start_pages_watchdog.sh
  printf '[%s] keepalive done\n' "$(date -Is)"
} >> "${LOG_FILE}" 2>&1
