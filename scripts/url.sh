#!/usr/bin/env bash
set -euo pipefail

# Print the currently active localhost.run URL (if any).
# This is meant to be used after running scripts/deploy.sh.

TUNNEL_LOG="${TUNNEL_LOG:-/tmp/tunnel.log}"
URL_FILE="$(cd "$(dirname "$0")/.." && pwd)/dashboard_url.txt"

if [[ -f "${URL_FILE}" ]]; then
  url="$(head -n 1 "${URL_FILE}" || true)"
  if [[ -n "${url}" ]] && printf '%s' "${url}" | grep -Eq '^https?://[a-z0-9.-]+[.]lhr[.]life/?$'; then
    echo "${url}"
    exit 0
  fi
fi

if [[ -f "${TUNNEL_LOG}" ]]; then
  tr -d '\r' <"${TUNNEL_LOG}" \
    | sed -r 's/\x1B\\[[0-9;]*[A-Za-z]//g' \
    | grep -Eo 'https?://[a-z0-9.-]+[.]lhr[.]life' \
    | tail -n 1 || true
  exit 0
fi

echo "No URL found. Run scripts/deploy.sh first." >&2
exit 1
