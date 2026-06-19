#!/usr/bin/env bash
set -euo pipefail
# Stop helper: stop tunnel and gunicorn started by deploy.sh

cd "$(dirname "$0")/.."

echo "Stopping localhost.run tunnels..."
pkill -f "$(pwd)/scripts/watchdog.sh" || true
pkill -f "$(pwd)/scripts/tunnel_loop.sh" || true
pkill -f "ssh .*localhost.run" || true
echo "Stopping Gunicorn processes for dashboard.dash_app..."
pkill -f "gunicorn dashboard.dash_app:server" || true

echo "Stopping Dash dev server (dashboard/dash_app.py)..."
if [[ -f dashboard.pid ]]; then
  pid="$(cat dashboard.pid || true)"
  if [[ -n "${pid}" ]] && ps -p "${pid}" >/dev/null 2>&1; then
    kill "${pid}" >/dev/null 2>&1 || true
    sleep 0.5
    ps -p "${pid}" >/dev/null 2>&1 && kill -9 "${pid}" >/dev/null 2>&1 || true
  fi
  rm -f dashboard.pid >/dev/null 2>&1 || true
fi
pkill -f "dashboard/dash_app.py" >/dev/null 2>&1 || true

echo "Stopped. Check /tmp/gunicorn.log and /tmp/tunnel.log for details."
