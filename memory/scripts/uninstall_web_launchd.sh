#!/usr/bin/env bash

set -euo pipefail

LABEL="com.micrott.memory-web"
KEEP_PLIST=0

usage() {
  cat <<'EOF'
Usage:
  memory/scripts/uninstall_web_launchd.sh [options]

Options:
  --label <label>   LaunchAgent label to uninstall (default: com.micrott.memory-web)
  --keep-plist      Stop/unload only, keep plist file
  -h, --help        Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      LABEL="$2"
      shift 2
      ;;
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

UID_NUM="$(id -u)"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true
launchctl disable "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true

if [[ "${KEEP_PLIST}" -eq 0 ]]; then
  rm -f "${PLIST_PATH}"
fi

if launchctl print "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1; then
  echo "Service ${LABEL} still exists in launchctl domain."
  exit 1
fi

echo "Uninstalled ${LABEL}"
if [[ "${KEEP_PLIST}" -eq 1 ]]; then
  echo "Kept plist: ${PLIST_PATH}"
else
  echo "Removed plist: ${PLIST_PATH}"
fi
