#!/bin/bash
# Install the Time Tracker collector as a per-user launchd LaunchAgent so it starts at
# login and keeps running in the background. Safe to re-run (it reloads).
#
# IMPORTANT — Full Disk Access: the LaunchAgent runs python3 directly, and macOS grants
# Full Disk Access per-executable. The collector needs it to read knowledgeC.db and the
# ~/Library/Biome files. If the dashboard shows no iPhone data after login, grant Full
# Disk Access to this python3 binary in:
#   System Settings → Privacy & Security → Full Disk Access  → add:
#   /opt/homebrew/bin/python3
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$(command -v python3)"
LABEL="com.saad.timetracker.collector"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$ROOT/collector/main.py</string>
    </array>
    <key>WorkingDirectory</key> <string>$ROOT</string>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <true/>
    <key>ProcessType</key>      <string>Background</string>
    <key>StandardOutPath</key>  <string>$ROOT/data/collector.log</string>
    <key>StandardErrorPath</key><string>$ROOT/data/collector.log</string>
</dict>
</plist>
PLIST

# Reload cleanly (ignore errors if it wasn't loaded yet).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed and started: $LABEL"
echo "  plist : $PLIST"
echo "  log   : $ROOT/data/collector.log"
echo
echo "If iPhone data never appears, grant Full Disk Access to: $PYTHON"
echo "(System Settings → Privacy & Security → Full Disk Access)"
