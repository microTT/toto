#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MEMORY_DIR="${REPO_ROOT}/memory"

LABEL="com.micrott.memory-web"
WORKSPACE="${REPO_ROOT}"
MEMORY_HOME="${CODEX_MEMORY_HOME:-}"
HOST_VALUE="127.0.0.1"
PORT_VALUE="59112"
PYTHON_BIN="$(command -v python3 || true)"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage:
  memory/scripts/install_web_launchd.sh [options]

Options:
  --label <label>         LaunchAgent label (default: com.micrott.memory-web)
  --workspace <path>      Workspace root (default: repo root)
  --memory-home <path>    Memory home; default derives from memory-admin bootstrap
  --host <host>           Bind host (default: 127.0.0.1)
  --port <port>           Bind port (default: 59112)
  --python-bin <path>     Python interpreter for launchd (default: current python3)
  --dry-run               Print planned plist without installing
  -h, --help              Show help
EOF
}

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  value="${value//\"/&quot;}"
  value="${value//\'/&apos;}"
  printf '%s' "$value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      LABEL="$2"
      shift 2
      ;;
    --workspace)
      WORKSPACE="$2"
      shift 2
      ;;
    --memory-home)
      MEMORY_HOME="$2"
      shift 2
      ;;
    --host)
      HOST_VALUE="$2"
      shift 2
      ;;
    --port)
      PORT_VALUE="$2"
      shift 2
      ;;
    --python-bin)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
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

if [[ -z "${PYTHON_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
  echo "Cannot find a usable python3 binary. Use --python-bin." >&2
  exit 1
fi

if [[ -z "${MEMORY_HOME}" ]]; then
  MEMORY_HOME="$(
    "${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" bootstrap \
      | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["memory_home"])'
  )"
fi

append_path() {
  local candidate="$1"
  if [[ -z "${candidate}" ]]; then
    return
  fi
  if [[ -z "${PATH_ENV}" ]]; then
    PATH_ENV="${candidate}"
    return
  fi
  case ":${PATH_ENV}:" in
    *":${candidate}:"*) ;;
    *) PATH_ENV="${PATH_ENV}:${candidate}" ;;
  esac
}

PATH_ENV=""
append_path "$(dirname "${PYTHON_BIN}")"
append_path "/opt/homebrew/bin"
append_path "/usr/local/bin"
append_path "/usr/bin"
append_path "/bin"
append_path "/usr/sbin"
append_path "/sbin"

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
STDOUT_LOG="${MEMORY_HOME}/control/memory-web.launchd.out.log"
STDERR_LOG="${MEMORY_HOME}/control/memory-web.launchd.err.log"
UID_NUM="$(id -u)"

mkdir -p "${MEMORY_HOME}/control"

TMP_PLIST="$(mktemp "/tmp/${LABEL}.XXXXXX.plist")"
trap 'rm -f "${TMP_PLIST}"' EXIT

cat >"${TMP_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "${LABEL}")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "${PYTHON_BIN}")</string>
    <string>$(xml_escape "${MEMORY_DIR}/bin/memory-web")</string>
    <string>--cwd</string>
    <string>$(xml_escape "${WORKSPACE}")</string>
    <string>--memory-home</string>
    <string>$(xml_escape "${MEMORY_HOME}")</string>
    <string>--host</string>
    <string>$(xml_escape "${HOST_VALUE}")</string>
    <string>--port</string>
    <string>$(xml_escape "${PORT_VALUE}")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "${WORKSPACE}")</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(xml_escape "${PATH_ENV}")</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$(xml_escape "${STDOUT_LOG}")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "${STDERR_LOG}")</string>
</dict>
</plist>
EOF

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[dry-run] generated plist:"
  cat "${TMP_PLIST}"
  exit 0
fi

mkdir -p "${LAUNCH_AGENTS_DIR}"
cp "${TMP_PLIST}" "${PLIST_PATH}"

launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${UID_NUM}" "${PLIST_PATH}"
launchctl enable "gui/${UID_NUM}/${LABEL}"
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}"

echo "Installed and started ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Workspace: ${WORKSPACE}"
echo "Memory home: ${MEMORY_HOME}"
echo "URL: http://${HOST_VALUE}:${PORT_VALUE}"
echo "stdout: ${STDOUT_LOG}"
echo "stderr: ${STDERR_LOG}"
