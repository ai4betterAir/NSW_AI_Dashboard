#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"

find_python_bin() {
  local required_module="fastapi"

  python_can_import() {
    local python_exe="$1"
    "${python_exe}" -c "import ${required_module}" >/dev/null 2>&1
  }

  if [[ -n "${PYTHON_BIN:-}" ]]; then
    if python_can_import "${PYTHON_BIN}"; then
      echo "${PYTHON_BIN}"
      return 0
    fi
  fi

  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    if python_can_import "${VIRTUAL_ENV}/bin/python"; then
      echo "${VIRTUAL_ENV}/bin/python"
      return 0
    fi
  fi

  local dir="$PWD"
  for _ in 0 1 2 3 4; do
    if [[ -x "${dir}/.venv/bin/python" ]]; then
      if python_can_import "${dir}/.venv/bin/python"; then
        echo "${dir}/.venv/bin/python"
        return 0
      fi
    fi
    dir="$(dirname "$dir")"
  done

  local candidate
  for candidate in python python3.11 python3.10 python3.9 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      if python_can_import "${candidate}"; then
        echo "${candidate}"
        return 0
      fi
    fi
  done

  echo "Error: could not find a Python interpreter with '${required_module}' installed." >&2
  echo "Hint: activate the right venv or set PYTHON_BIN to its python." >&2
  exit 127
}

check_port_available() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    if ss -ltnp "( sport = :${port} )" 2>/dev/null | tail -n +2 | head -n 1 | grep -q .; then
      echo "Error: PORT=${port} is already in use." >&2
      ss -ltnp "( sport = :${port} )" 2>/dev/null || true
      echo "Hint: stop the process above (e.g. kill <pid>) or choose another port: PORT=8001 $0" >&2
      exit 98
    fi
  fi
}

PORT="${PORT:-8000}"
check_port_available "${PORT}"
PYTHON_BIN="$(find_python_bin)"
PORT="${PORT}" "${PYTHON_BIN}" app.py
