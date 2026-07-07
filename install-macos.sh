#!/bin/zsh
set -eu

ROOT="${0:A:h}"
STATE_DIR="$HOME/Library/Application Support/deepseek-bridge"
APP_DIR="$HOME/Applications/DeepSeek Bridge.app"
PLIST="$HOME/Library/LaunchAgents/com.deepseek.bridge.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$STATE_DIR" "$HOME/Applications" "$HOME/Library/LaunchAgents"
chmod 700 "$STATE_DIR"
ln -sfn "$ROOT" "$STATE_DIR/install-root"

python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/playwright" install chromium

rm -rf "$APP_DIR"
cp -R "$ROOT/macos/DeepSeek Bridge.app" "$APP_DIR"
chmod 755 "$APP_DIR/Contents/MacOS/DeepSeekBridge"
cp "$ROOT/macos/com.deepseek.bridge.plist" "$PLIST"

launchctl bootout "$DOMAIN/com.deepseek.bridge" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/com.deepseek.bridge"

if command -v claude >/dev/null 2>&1; then
    claude mcp remove deepseek-free --scope user >/dev/null 2>&1 || true
    claude mcp add --scope user deepseek-free -- "$ROOT/run-mcp.sh"
else
    print "Claude CLI was not found. Register this MCP command manually:"
    print "$ROOT/run-mcp.sh"
fi

print "Installed DeepSeek Bridge. Run $ROOT/authenticate.sh to sign in."
