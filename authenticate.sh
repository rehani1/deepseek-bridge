#!/bin/zsh
set -eu
ROOT="${0:A:h}"
cd "$ROOT"
PLIST="$HOME/Library/LaunchAgents/com.deepseek.bridge.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/com.deepseek.bridge" >/dev/null 2>&1 || true

restart_daemon() {
    launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    launchctl kickstart -k "$DOMAIN/com.deepseek.bridge" >/dev/null 2>&1 || true
}
trap restart_daemon EXIT

"$ROOT/.venv/bin/python" -m deepseek_bridge.authenticate
