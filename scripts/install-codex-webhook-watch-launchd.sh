#!/bin/zsh
set -euo pipefail

LABEL="com.micrott.codex-webhook-watch"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
OUT_LOG="$LOG_DIR/codex-webhook-watch.log"
ERR_LOG="$LOG_DIR/codex-webhook-watch.err.log"
SCRIPT_PATH="/Users/microTT/toto/scripts/codex-webhook-watch.mjs"
NODE_PATH="${NODE_PATH:-$(command -v node)}"
WEBHOOK_URL="${CODEX_WEBHOOK_URL:-}"
AT_MOBILES="${CODEX_DINGTALK_AT_MOBILES:-}"
AT_USER_IDS="${CODEX_DINGTALK_AT_USER_IDS:-}"
AT_ALL="${CODEX_DINGTALK_AT_ALL:-false}"
EVENTS="${CODEX_WATCH_EVENTS:-task_complete,approval_needed}"
INTERVAL_MS="${CODEX_WATCH_INTERVAL_MS:-1500}"

xml_escape() {
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  value="${value//\"/&quot;}"
  value="${value//\'/&apos;}"
  printf '%s' "$value"
}

add_arg() {
  local value
  value="$(xml_escape "$1")"
  printf '      <string>%s</string>\n' "$value"
}

if [[ -z "$WEBHOOK_URL" ]]; then
  echo "CODEX_WEBHOOK_URL is required."
  echo "Example:"
  echo "  CODEX_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=...' $0"
  exit 1
fi

if [[ -z "$NODE_PATH" || ! -x "$NODE_PATH" ]]; then
  echo "Cannot find a usable node binary."
  exit 1
fi

if [[ ! -f "$SCRIPT_PATH" ]]; then
  echo "Watcher script not found: $SCRIPT_PATH"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

PLIST_CONTENT="<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
  <dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>WorkingDirectory</key>
    <string>/Users/microTT/toto</string>
    <key>StandardOutPath</key>
    <string>$OUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$ERR_LOG</string>
    <key>ProgramArguments</key>
    <array>
$(add_arg "$NODE_PATH")
$(add_arg "$SCRIPT_PATH")
$(add_arg "--url")
$(add_arg "$WEBHOOK_URL")
$(add_arg "--events")
$(add_arg "$EVENTS")
$(add_arg "--interval")
$(add_arg "$INTERVAL_MS")"

if [[ -n "$AT_MOBILES" ]]; then
  PLIST_CONTENT+="
$(add_arg "--at-mobiles")
$(add_arg "$AT_MOBILES")"
fi

if [[ -n "$AT_USER_IDS" ]]; then
  PLIST_CONTENT+="
$(add_arg "--at-user-ids")
$(add_arg "$AT_USER_IDS")"
fi

if [[ "$AT_ALL" == "true" ]]; then
  PLIST_CONTENT+="
$(add_arg "--at-all")"
fi

PLIST_CONTENT+="
    </array>
  </dict>
</plist>
"

printf '%s' "$PLIST_CONTENT" > "$PLIST_PATH"

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed and started: $LABEL"
echo "plist: $PLIST_PATH"
echo "stdout: $OUT_LOG"
echo "stderr: $ERR_LOG"
