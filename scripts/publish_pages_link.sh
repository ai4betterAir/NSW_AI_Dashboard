#!/usr/bin/env bash
set -euo pipefail

# Update docs/current.json for the GitHub Pages redirect page. With --push,
# publish it either through GITHUB_TOKEN or through git commit/push.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/load_local_env.sh"
load_dashboard_env
URL_FILE="${URL_FILE:-$ROOT_DIR/dashboard_url.txt}"
URL_FILES="${URL_FILES:-${URL_FILE}}"
CURRENT_JSON="${CURRENT_JSON:-$ROOT_DIR/docs/current.json}"
INDEX_HTML="${INDEX_HTML:-$ROOT_DIR/docs/index.html}"
CURRENT_HTML="${CURRENT_HTML:-$ROOT_DIR/docs/current.html}"
CNAME_FILE="${CNAME_FILE:-$ROOT_DIR/docs/CNAME}"
URL_HISTORY_LIMIT="${URL_HISTORY_LIMIT:-5}"
DO_PUSH=0
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-ai4betterAir/ai4betterAir.github.io}"
GITHUB_PAGES_JSON_PATH="${GITHUB_PAGES_JSON_PATH:-current.json}"
GITHUB_PAGES_INDEX_PATH="${GITHUB_PAGES_INDEX_PATH:-index.html}"
GITHUB_PAGES_CURRENT_HTML_PATH="${GITHUB_PAGES_CURRENT_HTML_PATH:-current.html}"
GITHUB_PAGES_CNAME_PATH="${GITHUB_PAGES_CNAME_PATH:-CNAME}"
GITHUB_PAGES_BRANCH="${GITHUB_PAGES_BRANCH:-main}"

get_github_token() {
  if [[ -n "${GITHUB_TOKEN:-}" \
    && "${GITHUB_TOKEN}" != "..." \
    && "${GITHUB_TOKEN}" != "export GITHUB_TOKEN" \
    && "${GITHUB_TOKEN}" != "your_github_token_with_contents_write" \
    && "${GITHUB_TOKEN}" != *" "* ]]; then
    printf '%s' "${GITHUB_TOKEN}"
    return 0
  fi

  if command -v gh >/dev/null 2>&1; then
    gh auth token 2>/dev/null || true
  fi
}

for arg in "$@"; do
  case "$arg" in
    --push)
      DO_PUSH=1
      ;;
    *)
      echo "Usage: $0 [--push]" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$(dirname "${CURRENT_JSON}")"
updated_at="$(date -Is)"
tmp="$(mktemp)"
candidate_tmp="$(mktemp)"
trap 'rm -f "${tmp}" "${candidate_tmp}"' EXIT

IFS=':' read -r -a url_file_list <<< "${URL_FILES}"
for url_file in "${url_file_list[@]}"; do
  if [[ -f "${url_file}" ]]; then
    sed -n '1,20p' "${url_file}" | while IFS= read -r candidate_url; do
      candidate_url="$(printf '%s' "${candidate_url}" | tr -d '[:space:]')"
      if [[ -n "${candidate_url}" ]]; then
        printf '%s\n' "${candidate_url}"
      fi
    done >> "${candidate_tmp}"
  fi
done

if [[ ! -s "${candidate_tmp}" ]]; then
  echo "No dashboard URL files found with URLs: ${URL_FILES}" >&2
  exit 1
fi

if ! python3 - "${CURRENT_JSON}" "${candidate_tmp}" "${tmp}" "${updated_at}" "${URL_HISTORY_LIMIT}" <<'PY'
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

current_json = Path(sys.argv[1])
candidate_file = Path(sys.argv[2])
output_file = Path(sys.argv[3])
updated_at = sys.argv[4]
history_limit = int(sys.argv[5])


def valid_dashboard_url(raw):
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host.endswith(".lhr.life")


def url_from_item(item):
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("url", "")).strip()
    return ""


existing = {}
if current_json.exists():
    try:
        data = json.loads(current_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    for item in data.get("urls", []):
        item_url = url_from_item(item)
        if valid_dashboard_url(item_url):
            existing[item_url] = {
                "url": item_url,
                "updated_at": item.get("updated_at") if isinstance(item, dict) else data.get("updated_at", ""),
            }
    current_url = url_from_item(data)
    if valid_dashboard_url(current_url):
        existing[current_url] = {
            "url": current_url,
            "updated_at": data.get("updated_at", ""),
        }

new_urls = []
seen_new_urls = set()
for line in candidate_file.read_text(encoding="utf-8").splitlines():
    candidate_url = line.strip()
    if not candidate_url:
        continue
    if not valid_dashboard_url(candidate_url):
        print(f"Refusing to publish invalid localhost.run URL: {candidate_url}", file=sys.stderr)
        sys.exit(1)
    if candidate_url in seen_new_urls:
        continue
    seen_new_urls.add(candidate_url)
    new_urls.append(candidate_url)

if not new_urls:
    print("No valid dashboard URLs found.", file=sys.stderr)
    sys.exit(1)

for candidate_url in reversed(new_urls):
    existing.pop(candidate_url, None)

urls = [{"url": candidate_url, "updated_at": updated_at} for candidate_url in new_urls]
urls.extend(existing.values())
urls = urls[:history_limit]

payload = {
    "url": urls[0]["url"],
    "updated_at": updated_at,
    "urls": urls,
}
output_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
then
  exit 1
fi
mv "${tmp}" "${CURRENT_JSON}"
rm -f "${candidate_tmp}"
trap - EXIT

url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "${CURRENT_JSON}")"
echo "Updated ${CURRENT_JSON}: ${url}"

if [[ "${DO_PUSH}" != "1" ]]; then
  echo "Not pushed. Run with --push to commit and push docs/current.json."
  exit 0
fi

cd "${ROOT_DIR}"
branch="$(git branch --show-current || true)"

publish_with_github_token() {
  local github_token

  github_token="$(get_github_token)"
  if [[ -z "${github_token}" ]]; then
    return 1
  fi

  if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi

  update_github_file() {
    local local_path="$1"
    local remote_path="$2"
    local api_url
    local body
    local content_b64
    local get_response
    local sha
    local status
    local tmp_get
    local tmp_put

    api_url="https://api.github.com/repos/${GITHUB_REPOSITORY}/contents/${remote_path}"
    tmp_get="$(mktemp)"
    tmp_put="$(mktemp)"
    status="$(curl -sS -o "${tmp_get}" -w '%{http_code}' \
      -H "Authorization: Bearer ${github_token}" \
      -H "Accept: application/vnd.github+json" \
      "${api_url}?ref=${GITHUB_PAGES_BRANCH}")"

    sha=""
    if [[ "${status}" == "200" ]]; then
      get_response="$(cat "${tmp_get}")"
      sha="$(printf '%s' "${get_response}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])' 2>/dev/null || true)"
      if [[ -z "${sha}" ]]; then
        echo "Could not parse SHA from GitHub API response for ${remote_path}." >&2
        sed -n '1,5p' "${tmp_get}" >&2 || true
        rm -f "${tmp_get}" "${tmp_put}"
        return 1
      fi
    elif [[ "${status}" != "404" ]]; then
      echo "GitHub API read failed for ${remote_path} (HTTP ${status}). Check GITHUB_TOKEN and repository access." >&2
      sed -n '1,5p' "${tmp_get}" >&2 || true
      rm -f "${tmp_get}" "${tmp_put}"
      return 1
    fi

    content_b64="$(base64 < "${local_path}" | tr -d '\n')"
    body="$(CONTENT_B64="${content_b64}" SHA="${sha}" BRANCH="${GITHUB_PAGES_BRANCH}" python3 - <<'PY'
import json
import os

body = {
    "message": "Update dashboard redirect URL",
    "content": os.environ["CONTENT_B64"],
    "branch": os.environ["BRANCH"],
}
if os.environ["SHA"]:
    body["sha"] = os.environ["SHA"]

print(json.dumps(body))
PY
)"

    status="$(curl -sS -o "${tmp_put}" -w '%{http_code}' -X PUT \
      -H "Authorization: Bearer ${github_token}" \
      -H "Accept: application/vnd.github+json" \
      -d "${body}" \
      "${api_url}")"

    if [[ "${status}" != "200" && "${status}" != "201" ]]; then
      echo "GitHub API write failed for ${remote_path} (HTTP ${status}). Check token scopes (Contents: Read and write)." >&2
      sed -n '1,8p' "${tmp_put}" >&2 || true
      rm -f "${tmp_get}" "${tmp_put}"
      return 1
    fi

    rm -f "${tmp_get}" "${tmp_put}"
  }

  update_github_file "${CURRENT_JSON}" "${GITHUB_PAGES_JSON_PATH}" || return 1
  if [[ -f "${INDEX_HTML}" ]]; then
    update_github_file "${INDEX_HTML}" "${GITHUB_PAGES_INDEX_PATH}" || return 1
  fi
  if [[ -f "${CURRENT_HTML}" ]]; then
    update_github_file "${CURRENT_HTML}" "${GITHUB_PAGES_CURRENT_HTML_PATH}" || return 1
  fi
  if [[ -f "${CNAME_FILE}" ]]; then
    update_github_file "${CNAME_FILE}" "${GITHUB_PAGES_CNAME_PATH}" || return 1
  fi

  echo "Published redirect files to GitHub via API."
  return 0
}

if publish_with_github_token; then
  exit 0
fi

# Fallback: clone the GitHub Pages repo, update redirect files, and push
publish_with_git_clone() {
  local pages_clone
  local pages_url
  pages_clone="$(mktemp -d)"
  trap 'rm -rf "${pages_clone}"' RETURN

  pages_url="https://github.com/${GITHUB_REPOSITORY}.git"
  if ! git clone --depth 1 "${pages_url}" "${pages_clone}" >/dev/null 2>&1; then
    echo "Could not clone ${pages_url} — check network and git credentials." >&2
    return 1
  fi

  cp "${CURRENT_JSON}" "${pages_clone}/${GITHUB_PAGES_JSON_PATH}"
  if [[ -f "${INDEX_HTML}" ]]; then
    cp "${INDEX_HTML}" "${pages_clone}/${GITHUB_PAGES_INDEX_PATH}"
  fi
  if [[ -f "${CURRENT_HTML}" ]]; then
    cp "${CURRENT_HTML}" "${pages_clone}/${GITHUB_PAGES_CURRENT_HTML_PATH}"
  fi
  if [[ -f "${CNAME_FILE}" ]]; then
    cp "${CNAME_FILE}" "${pages_clone}/${GITHUB_PAGES_CNAME_PATH}"
  fi

  cd "${pages_clone}"
  git config user.email "dashboard@ai4betterair" 2>/dev/null || true
  git config user.name "NSW Dashboard Bot" 2>/dev/null || true
  git add "${GITHUB_PAGES_JSON_PATH}"
  if [[ -f "${INDEX_HTML}" ]]; then
    git add "${GITHUB_PAGES_INDEX_PATH}"
  fi
  if [[ -f "${CURRENT_HTML}" ]]; then
    git add "${GITHUB_PAGES_CURRENT_HTML_PATH}"
  fi
  if [[ -f "${CNAME_FILE}" ]]; then
    git add "${GITHUB_PAGES_CNAME_PATH}"
  fi
  if git diff --cached --quiet -- "${GITHUB_PAGES_JSON_PATH}" "${GITHUB_PAGES_INDEX_PATH}" "${GITHUB_PAGES_CURRENT_HTML_PATH}" "${GITHUB_PAGES_CNAME_PATH}"; then
    echo "GitHub Pages already up to date."
    cd "${ROOT_DIR}"
    return 0
  fi
  git commit -m "Update dashboard redirect URL" >/dev/null
  if ! git push origin "${GITHUB_PAGES_BRANCH}" 2>&1; then
    echo "git push to ${GITHUB_REPOSITORY} failed — check git credentials." >&2
    cd "${ROOT_DIR}"
    return 1
  fi

  cd "${ROOT_DIR}"
  echo "Published current.json to GitHub Pages via git clone+push."
  return 0
}

if publish_with_git_clone; then
  exit 0
fi

echo "All publish methods failed. Push current.json to ${GITHUB_REPOSITORY} manually." >&2
exit 1
