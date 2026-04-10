#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.micrott.codex-session-viewer"
WORKDIR="${SCRIPT_DIR}"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
HOST_VALUE="${HOST_VALUE:-127.0.0.1}"
PORT_VALUE="${PORT_VALUE:-59111}"
SESSIONS_ROOT_VALUE="${SESSIONS_ROOT_VALUE:-}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
LOG_DIR="${WORKDIR}/logs"
STDOUT_LOG="${LOG_DIR}/launchagent.stdout.log"
STDERR_LOG="${LOG_DIR}/launchagent.stderr.log"
UID_NUM="$(id -u)"

usage() {
  cat <<'EOF'
Usage:
  codex-session-viewer/install-launchd.sh [options]

Options:
  --node-bin <path>         Node binary to use for launchd
  --host <host>             Bind host (default: 127.0.0.1)
  --port <port>             Bind port (default: 59111)
  --sessions-root <path>    Optional session root override
  --dry-run                 Print the plist without installing it
  -h, --help                Show help
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
    --node-bin)
      NODE_BIN="$2"
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
    --sessions-root)
      SESSIONS_ROOT_VALUE="$2"
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

if [[ -z "${NODE_BIN}" || ! -x "${NODE_BIN}" ]]; then
  echo "Cannot find a usable node binary." >&2
  exit 1
fi

if [[ ! -f "${WORKDIR}/server.mjs" ]]; then
  echo "server.mjs not found in ${WORKDIR}" >&2
  exit 1
fi

PATH_ENV=""
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

append_path "$(dirname "${NODE_BIN}")"
append_path "/opt/homebrew/bin"
append_path "/usr/local/bin"
append_path "/usr/bin"
append_path "/bin"
append_path "/usr/sbin"
append_path "/sbin"

mkdir -p "${LOG_DIR}"

TMP_PLIST="$(mktemp "/tmp/${LABEL}.XXXXXX.plist")"
trap 'rm -f "${TMP_PLIST}"' EXIT

cat >"${TMP_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "${NODE_BIN}")</string>
    <string>$(xml_escape "${WORKDIR}/server.mjs")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "${WORKDIR}")</string>
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
    <key>HOST</key>
    <string>$(xml_escape "${HOST_VALUE}")</string>
    <key>PORT</key>
    <string>$(xml_escape "${PORT_VALUE}")</string>
EOF

if [[ -n "${SESSIONS_ROOT_VALUE}" ]]; then
  cat >>"${TMP_PLIST}" <<EOF
    <key>SESSIONS_ROOT</key>
    <string>$(xml_escape "${SESSIONS_ROOT_VALUE}")</string>
EOF
fi

cat >>"${TMP_PLIST}" <<EOF
  </dict>
  <key>StandardOutPath</key>
  <string>$(xml_escape "${STDOUT_LOG}")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "${STDERR_LOG}")</string>
</dict>
</plist>
EOF

if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
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
echo "URL: http://${HOST_VALUE}:${PORT_VALUE}"
echo "stdout: ${STDOUT_LOG}"
echo "stderr: ${STDERR_LOG}"
