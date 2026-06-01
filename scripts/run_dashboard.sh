#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"

usage() {
  cat <<'EOF'
Usage:
  run_dashboard.sh [--open] [--force]

Options:
  --open   Try to open the dashboard URL in a browser on this machine.
  --force  If the selected PORT is already in use, try to stop a prior
           dashboard process owned by you and restart.

Environment:
  PORT=8050           Bind port (default: 8050)
  DASHBOARD_HOST=addr Bind address (default: 127.0.0.1)
  DASHBOARD_READY_TIMEOUT=60   Seconds to wait for /debug-page (default: 60)
  OPEN_BROWSER=1      Same as --open
  FORCE_RESTART=1     Same as --force
  DASHBOARD_LOG=path  Log file (default: dashboard.log)
  DASHBOARD_LOG_APPEND=1  Append to log (default: overwrite)
  OPEN_BROWSER_REMOTE=1   Allow browser open over SSH
  PYTHON_BIN=/path    Force python interpreter
EOF
}

OPEN_BROWSER="${OPEN_BROWSER:-0}"
FORCE_RESTART="${FORCE_RESTART:-0}"
OPEN_BROWSER_REMOTE="${OPEN_BROWSER_REMOTE:-0}"
while [[ $# -gt 0 ]]; do
  case "${1}" in
    -h|--help)
      usage
      exit 0
      ;;
    --open)
      OPEN_BROWSER=1
      shift
      ;;
    --force)
      FORCE_RESTART=1
      shift
      ;;
    *)
      echo "Error: unexpected argument: ${1}" >&2
      usage >&2
      exit 2
      ;;
  esac
done

find_python_bin() {
  # Keep probing minimal: only try a small set of likely interpreters and validate
  # with a lightweight import check.
  python_can_import() {
    local python_exe="$1"
    "${python_exe}" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1); import dash, dotenv, requests, pandas" >/dev/null 2>&1
  }

  local candidates=()
  # Honour an explicit interpreter first (matches docs + avoids stale local venvs).
  if [[ -n "${PYTHON_BIN:-}" && -x "${PYTHON_BIN}" ]]; then
    candidates+=("${PYTHON_BIN}")
  fi
  # Prefer a local project venv next, even if another venv is currently activated.
  if [[ -x "$PWD/.venv/bin/python" ]]; then
    candidates+=("$PWD/.venv/bin/python")
  fi
  if [[ -x "$PWD/.venv_py/bin/python" ]]; then
    candidates+=("$PWD/.venv_py/bin/python")
  fi
  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    candidates+=("${VIRTUAL_ENV}/bin/python")
  fi
  # Finally, look for a nearby `.venv` / `.venv_py` (common in this repo) even if not activated.
  local dir="$PWD"
  for _ in 0 1 2 3 4; do
    if [[ -x "${dir}/.venv/bin/python" ]]; then
      candidates+=("${dir}/.venv/bin/python")
      break
    fi
    if [[ -x "${dir}/.venv_py/bin/python" ]]; then
      candidates+=("${dir}/.venv_py/bin/python")
      break
    fi
    dir="$(dirname "$dir")"
  done
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("python3")
  fi
  if command -v python >/dev/null 2>&1; then
    candidates+=("python")
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    if python_can_import "${candidate}"; then
      echo "${candidate}"
      return 0
    fi
  done

  echo "Error: could not find a Python interpreter with 'dash' installed." >&2
  echo "Hint: activate the right venv or set PYTHON_BIN to its python." >&2
  exit 127
}

check_port_available() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    # When unused, `ss` prints only the header line.
    if ss -ltnp "( sport = :${port} )" 2>/dev/null | tail -n +2 | head -n 1 | grep -q .; then
      if [[ "${FORCE_RESTART}" == "1" ]]; then
        local line pids pid owner cmd
        line="$(ss -ltnp "( sport = :${port} )" 2>/dev/null | tail -n +2 | head -n 1 || true)"
        # Extract one or more pid=NNN occurrences without requiring ripgrep.
        pids="$(printf '%s\n' "${line}" | grep -oE 'pid=[0-9]+' 2>/dev/null | cut -d= -f2 | tr '\n' ' ' || true)"
        if [[ -z "${pids// }" ]]; then
          # Fallback: grab the first pid only.
          pids="$(printf '%s\n' "${line}" | sed -n 's/.*pid=\\([0-9][0-9]*\\).*/\\1/p' | tr '\n' ' ' || true)"
        fi
        for pid in ${pids}; do
          owner="$(ps -o user= -p "${pid}" 2>/dev/null | tr -d ' ' || true)"
          cmd="$(ps -o args= -p "${pid}" 2>/dev/null || true)"
          if [[ -n "${owner}" && "${owner}" == "$(id -un)" ]] && printf '%s\n' "${cmd}" | grep -Eq 'dashboard/dash_app\.py|dash_app\.py'; then
            echo "PORT=${port} is in use by your previous dashboard (pid=${pid}); stopping it (--force)..." >&2
            kill "${pid}" >/dev/null 2>&1 || true
            sleep 0.6
            ps -p "${pid}" >/dev/null 2>&1 && kill -9 "${pid}" >/dev/null 2>&1 || true
            return 0
          fi
        done
      fi
      echo "Error: PORT=${port} is already in use." >&2
      ss -ltnp "( sport = :${port} )" 2>/dev/null || true
      echo "Hint: stop the process above (e.g. kill <pid>), run '$0 --force', or choose another port: PORT=8060 $0" >&2
      exit 98
    fi
  fi
}

PORT="${PORT:-8050}"
check_port_available "${PORT}"

PYTHON_BIN="$(find_python_bin)"
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_LOG="${DASHBOARD_LOG:-dashboard.log}"
DASHBOARD_LOG_APPEND="${DASHBOARD_LOG_APPEND:-0}"
echo "Dashboard starting..."
echo "Python: ${PYTHON_BIN}"
echo "URL: http://${DASHBOARD_HOST}:${PORT}"
echo "If running on a remote host, forward the port, e.g.:"
echo "  ssh -L ${PORT}:127.0.0.1:${PORT} <user>@<host>"
echo "Log: ${DASHBOARD_LOG}"

start_ts="$(date +%s)"
if [[ "${DASHBOARD_LOG_APPEND}" == "1" ]]; then
  : >>"${DASHBOARD_LOG}"
else
  : >"${DASHBOARD_LOG}"
fi
PORT="${PORT}" "${PYTHON_BIN}" dashboard/dash_app.py >>"${DASHBOARD_LOG}" 2>&1 &
dash_pid="$!"
echo "${dash_pid}" > dashboard.pid

cleanup() {
  if [[ -n "${dash_pid:-}" ]]; then
    kill "${dash_pid}" >/dev/null 2>&1 || true
  fi
  rm -f dashboard.pid >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

ready_url="http://${DASHBOARD_HOST}:${PORT}/debug-page"
READY_TIMEOUT="${DASHBOARD_READY_TIMEOUT:-60}"
if ! [[ "${READY_TIMEOUT}" =~ ^[0-9]+$ ]]; then
  echo "Error: DASHBOARD_READY_TIMEOUT must be an integer number of seconds (got: ${READY_TIMEOUT})." >&2
  exit 2
fi
deadline="$((start_ts + READY_TIMEOUT))"
ready_ok=0
last_note_ts=0
while :; do
  now_ts="$(date +%s)"
  if (( now_ts >= deadline )); then
    break
  fi
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "${ready_url}" >/dev/null 2>&1; then
      ready_ok=1
      break
    fi
  else
    # Fallback if curl is unavailable: check with bash TCP socket.
    if (echo >"/dev/tcp/127.0.0.1/${PORT}") >/dev/null 2>&1; then
      ready_ok=1
      break
    fi
  fi
  # If the dashboard process has already exited, stop waiting and show logs.
  if ! ps -p "${dash_pid}" >/dev/null 2>&1; then
    break
  fi
  if (( now_ts - last_note_ts >= 10 )); then
    echo "Waiting for dashboard to become ready... ($((deadline - now_ts))s left)"
    last_note_ts="${now_ts}"
  fi
  sleep 0.25
done

end_ts="$(date +%s)"
if [[ "${ready_ok}" != "1" ]]; then
  echo "Error: dashboard did not become ready after $((end_ts - start_ts))s." >&2
  echo "Note: the dashboard process is still running (pid=${dash_pid})." >&2
  echo "Hint: this can happen if startup is slow; try increasing DASHBOARD_READY_TIMEOUT, e.g.:" >&2
  echo "  DASHBOARD_READY_TIMEOUT=240 PORT=${PORT} DASHBOARD_HOST=${DASHBOARD_HOST} $0 --force" >&2
  echo "Last 40 log lines (${DASHBOARD_LOG}):" >&2
  tail -n 40 "${DASHBOARD_LOG}" >&2 || true
  echo "Keeping the dashboard running; press Ctrl+C to stop." >&2
  wait "${dash_pid}"
  exit 1
fi
echo "Ready in $((end_ts - start_ts))s (checked ${ready_url})."
echo "Dashboard is running (pid=${dash_pid}). Press Ctrl+C to stop."

if command -v curl >/dev/null 2>&1; then
  echo "Healthcheck: / -> $(curl -fsS -o /dev/null -w 'HTTP %{http_code} %{time_total}s' "http://${DASHBOARD_HOST}:${PORT}/" 2>/dev/null || echo 'FAILED')"
  echo "Healthcheck: /_dash-layout -> $(curl -fsS -o /dev/null -w 'HTTP %{http_code} %{time_total}s' "http://${DASHBOARD_HOST}:${PORT}/_dash-layout" 2>/dev/null || echo 'FAILED')"
fi

if [[ "${OPEN_BROWSER}" == "1" ]]; then
  dash_url="http://${DASHBOARD_HOST}:${PORT}"
  if [[ -n "${SSH_CONNECTION:-}" || -n "${SSH_TTY:-}" ]]; then
    echo "Note: detected SSH session; not auto-opening a browser on the remote host."
    echo "Tip: SSH tunnel then open locally:"
    echo "  ssh -L ${PORT}:127.0.0.1:${PORT} <user>@<host>"
    echo "  open http://127.0.0.1:${PORT}  # macOS"
    echo "  xdg-open http://127.0.0.1:${PORT}  # Linux"
    if [[ "${OPEN_BROWSER_REMOTE}" != "1" ]]; then
      wait "${dash_pid}"
      exit 0
    fi
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    (xdg-open "${dash_url}" >/dev/null 2>&1 || true) &
  elif command -v open >/dev/null 2>&1; then
    (open "${dash_url}" >/dev/null 2>&1 || true) &
  elif command -v python3 >/dev/null 2>&1; then
    (python3 -m webbrowser "${dash_url}" >/dev/null 2>&1 || true) &
  elif command -v python >/dev/null 2>&1; then
    (python -m webbrowser "${dash_url}" >/dev/null 2>&1 || true) &
  else
    echo "Could not auto-open a browser (no xdg-open/open/python found)."
  fi
fi

wait "${dash_pid}"
