#!/usr/bin/env bash
set -euo pipefail

# Start the dashboard, supervised localhost.run tunnel, and watchdog with the
# optional GitHub Pages current-link publisher enabled.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT_DIR}"

export PUBLISH_PAGES_ON_URL="${PUBLISH_PAGES_ON_URL:-1}"
export PUBLISH_PAGES_PUSH="${PUBLISH_PAGES_PUSH:-1}"
FORCE_TUNNEL_RESTART="${FORCE_TUNNEL_RESTART:-1}"

if [[ "${FORCE_TUNNEL_RESTART}" == "1" ]]; then
  pkill -f "${ROOT_DIR}/scripts/tunnel_loop.sh" >/dev/null 2>&1 || true
  pkill -f "^ssh .*localhost.run$" >/dev/null 2>&1 || true
  pkill -f "${ROOT_DIR}/scripts/watchdog.sh" >/dev/null 2>&1 || true
fi

scripts/deploy.sh

if ! pgrep -f "${ROOT_DIR}/scripts/watchdog.sh" >/dev/null 2>&1; then
  setsid "${ROOT_DIR}/scripts/watchdog.sh" >> /tmp/dashboard_watchdog.log 2>&1 < /dev/null &
  echo "Watchdog started (logs: /tmp/dashboard_watchdog.log)"
else
  echo "Watchdog already running"
fi

scripts/status.sh
