#!/bin/zsh
set -euo pipefail

LABEL="com.micrott.codex-webhook-watch"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "Removed: $LABEL"
