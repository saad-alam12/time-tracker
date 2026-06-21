#!/bin/bash
# Stop and remove the Time Tracker collector LaunchAgent. Leaves the database intact.
set -euo pipefail

LABEL="com.saad.timetracker.collector"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Stopped and removed: $LABEL"
echo "(Your data in data/timetracker.sqlite is untouched.)"
