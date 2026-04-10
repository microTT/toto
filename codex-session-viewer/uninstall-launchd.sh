#!/usr/bin/env bash

set -euo pipefail

LABEL="com.micrott.codex-session-viewer"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
KEEP_PLIST=0

usage() {
  cat <<'EOF'
Usage:
  codex-session-viewer/uninstall-launchd.sh [options]

Options:
  --keep-plist   Stop the LaunchAgent but keep the plist file
  -h, --help     Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-plist)
      KEEP_PLIST=1
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

launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true

if [[ "${KEEP_PLIST}" -eq 0 ]]; then
  rm -f "${PLIST_PATH}"
  echo "Removed plist: ${PLIST_PATH}"
else
  echo "Kept plist: ${PLIST_PATH}"
fi

echo "Stopped ${LABEL}"
