#!/usr/bin/env bash
# Install the macOS LaunchAgent so the portal starts on every login/boot and
# respawns on crash. Idempotent — re-run after changing the .plist template.
set -euo pipefail

PORTAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.binance-agent-os.signal-portal"
SRC="$PORTAL_DIR/deploy/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$PORTAL_DIR/.venv/bin/python" ]; then
  echo "! $PORTAL_DIR/.venv not found — run ./run.sh once first to create it." >&2
  exit 1
fi

mkdir -p "$PORTAL_DIR/logs" "$HOME/Library/LaunchAgents"
sed "s#__PORTAL_DIR__#$PORTAL_DIR#g" "$SRC" > "$DEST"

# Stop a running instance (dev server / previous agent) so the port is free.
launchctl unload "$DEST" 2>/dev/null || true
lsof -ti tcp:"${PORTAL_PORT:-8777}" 2>/dev/null | xargs kill 2>/dev/null || true
sleep 1

launchctl load "$DEST"
echo "· installed  $DEST"
echo "· portal starts now and on every login. Logs: $PORTAL_DIR/logs/portal.{out,err}.log"
sleep 2
launchctl list | grep "$LABEL" || echo "  (not listed yet — check the logs)"
