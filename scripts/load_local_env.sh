#!/usr/bin/env bash

# Load dashboard-local environment variables, including private tokens.
# The .env file is intentionally ignored by git.

load_dashboard_env() {
  local env_file
  local old_allexport

  env_file="${DASHBOARD_ENV_FILE:-${ROOT_DIR}/.env}"
  [[ -f "${env_file}" ]] || return 0

  chmod go-rwx "${env_file}" >/dev/null 2>&1 || true

  old_allexport="$(set +o | grep '^set .*allexport' || true)"
  set -a
  # shellcheck disable=SC1090
  . "${env_file}"
  set +a
  if [[ "${old_allexport}" == "set -o allexport" ]]; then
    set -a
  fi
}
