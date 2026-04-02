#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MEMORY_DIR="${REPO_ROOT}/memory"

LABEL="com.micrott.memoryd"
WORKSPACE="${REPO_ROOT}"
MEMORY_HOME="${CODEX_MEMORY_HOME:-}"
BACKEND="codex"
POLL_INTERVAL="5"
PYTHON_BIN="$(command -v python3 || true)"
CODEX_BIN="$(command -v codex || true)"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage:
  memory/scripts/install_launchd.sh [options]

Options:
  --label <label>               LaunchAgent label (default: com.micrott.memoryd)
  --workspace <path>            Workspace root (default: repo root)
  --memory-home <path>          Memory home; default derives from memory-admin bootstrap
  --backend <codex|heuristic>   Worker backend (default: codex)
  --poll-interval <seconds>     Poll interval (default: 5)
  --python-bin <path>           Python interpreter for launchd (default: current python3)
  --codex-bin <path>            Codex binary path for PATH injection (default: command -v codex)
  --dry-run                     Print planned plist without installing
  -h, --help                    Show help
EOF
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
    --backend)
      BACKEND="$2"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="$2"
      shift 2
      ;;
    --python-bin)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --codex-bin)
      CODEX_BIN="$2"
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

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 not found in PATH. Use --python-bin." >&2
  exit 1
fi

if [[ -z "${MEMORY_HOME}" ]]; then
  MEMORY_HOME="$(
    "${MEMORY_DIR}/bin/memory-admin" --cwd "${WORKSPACE}" bootstrap \
      | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["memory_home"])'
  )"
fi

if [[ "${BACKEND}" == "codex" && -z "${CODEX_BIN}" ]]; then
  echo "codex binary not found in PATH. Use --codex-bin or switch --backend heuristic." >&2
  exit 1
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
if [[ -n "${CODEX_BIN}" ]]; then
  append_path "$(dirname "${CODEX_BIN}")"
fi
append_path "/opt/homebrew/bin"
append_path "/usr/local/bin"
append_path "/usr/bin"
append_path "/bin"
append_path "/usr/sbin"
append_path "/sbin"

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
STDOUT_LOG="${MEMORY_HOME}/control/memoryd.launchd.out.log"
STDERR_LOG="${MEMORY_HOME}/control/memoryd.launchd.err.log"
UID_NUM="$(id -u)"

mkdir -p "${MEMORY_HOME}/control"

TMP_PLIST="$(mktemp /tmp/${LABEL}.XXXXXX.plist)"
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
    <string>${PYTHON_BIN}</string>
    <string>${MEMORY_DIR}/bin/memoryd</string>
    <string>daemon</string>
    <string>--cwd</string>
    <string>${WORKSPACE}</string>
    <string>--memory-home</string>
    <string>${MEMORY_HOME}</string>
    <string>--backend</string>
    <string>${BACKEND}</string>
    <string>--poll-interval</string>
    <string>${POLL_INTERVAL}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${WORKSPACE}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${PATH_ENV}</string>
  </dict>
  <key>StandardOutPath</key>
  <string>${STDOUT_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${STDERR_LOG}</string>
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
launchctl print "gui/${UID_NUM}/${LABEL}" | sed -n '1,40p'
