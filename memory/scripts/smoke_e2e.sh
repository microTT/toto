#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MEMORY_DIR="${REPO_ROOT}/memory"

WORKSPACE="${REPO_ROOT}"
MEMORY_HOME="${CODEX_MEMORY_HOME:-}"
SKIP_LIVE=0

usage() {
  cat <<'EOF'
Usage:
  memory/scripts/smoke_e2e.sh [--workspace PATH] [--memory-home PATH] [--skip-live]

Options:
  --workspace   Workspace root used for live validation (default: repo root)
  --memory-home Installed memory_home for live validation
  --skip-live   Skip installed-stack live validation
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      WORKSPACE="$2"
      shift 2
      ;;
    --memory-home)
      MEMORY_HOME="$2"
      shift 2
      ;;
    --skip-live)
      SKIP_LIVE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "${SKIP_LIVE}" -eq 0 && -z "${MEMORY_HOME}" && -f "${HOME}/.codex/config.toml" ]]; then
  MEMORY_HOME="$(
    grep -Eo '"--memory-home",[[:space:]]*"[^"]+"' "${HOME}/.codex/config.toml" \
      | sed -E 's/.*"--memory-home",[[:space:]]*"([^"]+)"/\1/' \
      | head -n 1 || true
  )"
fi

if [[ "${SKIP_LIVE}" -eq 0 && -z "${MEMORY_HOME}" ]]; then
  echo "Live validation needs --memory-home (or CODEX_MEMORY_HOME / ~/.codex/config.toml)." >&2
  exit 1
fi

log_step() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$1"
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  if ! grep -Fq -- "${needle}" <<<"${haystack}"; then
    echo "Assertion failed: expected to find '${needle}'" >&2
    exit 1
  fi
}

TMP_DIR="$(mktemp -d /tmp/memory-smoke-e2e.XXXXXX)"
trap 'rm -rf "${TMP_DIR}"' EXIT
E2E_HOME="${TMP_DIR}/memory-home"
mkdir -p "${E2E_HOME}"

log_step "compileall"
python3 -m compileall "${MEMORY_DIR}/memory_system" "${MEMORY_DIR}/tests"

log_step "unittest"
python3 -m unittest discover -s "${MEMORY_DIR}/tests" -t "${REPO_ROOT}"

log_step "codex output-schema smoke"
SCHEMA_OUT="${TMP_DIR}/memory_patch_smoke.json"
codex exec \
  --ephemeral \
  --sandbox read-only \
  --skip-git-repo-check \
  --json \
  --output-last-message "${SCHEMA_OUT}" \
  --output-schema "${MEMORY_DIR}/schemas/memory_patch.schema.json" \
  -c features.codex_hooks=false \
  -C "${REPO_ROOT}" \
  "Return a noop patch plan that matches the schema. JSON only."
python3 -m json.tool "${SCHEMA_OUT}" >/dev/null

log_step "command e2e on isolated memory home"
"${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" bootstrap >/dev/null

"${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" upsert \
  --scope local \
  --id l_e2e_local_001 \
  --type task_context \
  --status active \
  --confidence high \
  --subject "E2E auth snapshot" \
  --summary "Revisit failing auth snapshot in e2e validation." \
  --tags auth,ci \
  --source-ref e2e:test \
  --scope-reason "repo specific" >/dev/null

"${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" upsert \
  --scope global \
  --id g_e2e_global_001 \
  --type preference \
  --status active \
  --confidence high \
  --subject "E2E language" \
  --summary "Prefer concise Chinese responses during tests." \
  --tags language,style \
  --source-ref e2e:test \
  --scope-reason "cross workspace preference" >/dev/null

CONTEXT_TEXT="$("${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" context)"
assert_contains "${CONTEXT_TEXT}" "E2E language"
assert_contains "${CONTEXT_TEXT}" "E2E auth snapshot"

GET_LOCAL="$("${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" get l_e2e_local_001)"
GET_GLOBAL="$("${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" get g_e2e_global_001)"
assert_contains "${GET_LOCAL}" "\"id\": \"l_e2e_local_001\""
assert_contains "${GET_GLOBAL}" "\"id\": \"g_e2e_global_001\""

RECENT_FILE="$(ls "${E2E_HOME}/workspace/recent/"*.md | head -n 1)"
mv "${RECENT_FILE}" "${E2E_HOME}/workspace/recent/2020-01-01.md"
"${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" archive >/dev/null

"${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" rebuild-index --json >/dev/null
SEARCH_RESULT="$("${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" --memory-home "${E2E_HOME}" search "auth snapshot" --top-k 5)"
assert_contains "${SEARCH_RESULT}" "\"record_id\": \"l_e2e_local_001\""
assert_contains "${SEARCH_RESULT}" "/archive/"

if [[ "${SKIP_LIVE}" -eq 0 ]]; then
  log_step "installed stack live validation"
  "${MEMORY_DIR}/scripts/validate_installed_stack.py" \
    --workspace "${WORKSPACE}" \
    --memory-home "${MEMORY_HOME}" >/dev/null
fi

log_step "smoke e2e passed"
echo "OK"
