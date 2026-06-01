#!/usr/bin/env bash
set -euo pipefail
# Status helper: show listeners and process status; check local health

echo "Listeners (ports 8050, 3000, 3001):"
ss -ltnp | egrep ':8050\b|:3000\b|:3001\b' || true

echo
echo "Processes (gunicorn, ssh localhost.run):"
ps aux | egrep 'gunicorn|ssh .*localhost.run' | egrep -v egrep || true

echo
echo "HTTP health check for http://127.0.0.1:8050 :"
curl -s -o /dev/null -w 'HTTP:%{http_code}\n' http://127.0.0.1:8050 || true
