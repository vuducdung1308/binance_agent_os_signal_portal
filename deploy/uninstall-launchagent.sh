#!/usr/bin/env bash
# Remove the LaunchAgent — the portal will no longer auto-start.
set -euo pipefail
LABEL="com.binance-agent-os.signal-portal"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl unload "$DEST" 2>/dev/null || true
rm -f "$DEST"
echo "· removed $DEST — portal no longer auto-starts (a running instance was stopped)"
