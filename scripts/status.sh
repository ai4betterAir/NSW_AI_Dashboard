#!/usr/bin/env bash
set -euo pipefail
# Status helper: show listeners and process status; check local health

echo "Listeners (ports 8050, 3000, 3001):"
ss -ltnp | egrep ':8050\b|:3000\b|:3001\b' || true

echo
echo "Processes (gunicorn, tunnel supervisor, ssh localhost.run):"
ps aux | egrep 'gunicorn|watchdog[.]sh|tunnel_loop[.]sh|ssh .*localhost.run|dashboard/dash_app.py' | egrep -v egrep || true

echo
echo "HTTP health check for http://127.0.0.1:8050 :"
curl -s -o /dev/null -w 'HTTP:%{http_code}\n' http://127.0.0.1:8050 || true

echo
echo "Saved public URL:"
saved_url=""
if [[ -f "$(cd "$(dirname "$0")/.." && pwd)/dashboard_url.txt" ]]; then
  saved_url="$(head -n 1 "$(cd "$(dirname "$0")/.." && pwd)/dashboard_url.txt")"
  echo "${saved_url}"
else
  echo "none"
fi

echo
echo "GitHub Pages current.json URL:"
pages_url="$(
  curl -fsS --max-time 10 "https://ai4betterair.github.io/current.json?ts=$(date +%s)" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url", ""))' 2>/dev/null || true
)"
if [[ -n "${pages_url}" ]]; then
  echo "${pages_url}"
  if [[ -n "${saved_url}" && "${pages_url}" != "${saved_url}" ]]; then
    echo "WARNING: GitHub Pages is stale; run scripts/publish_pages_link.sh --push after fixing GitHub credentials."
  fi
else
  echo "unavailable"
fi

echo
echo "Recent tunnel supervisor log:"
tail -n 10 /tmp/tunnel_supervisor.log 2>/dev/null || true

echo
echo "Recent watchdog log:"
tail -n 10 /tmp/dashboard_watchdog.log 2>/dev/null || true
