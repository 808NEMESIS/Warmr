#!/bin/bash
# install_launchd.sh — install macOS launchd agents for Warmr.
# Unlike crontab, launchd:
#   1. Catches up missed runs after wake-from-sleep
#   2. Can wake the Mac from sleep (if WakeSystem=true + Schedule used)
#   3. Runs on login and at boot via RunAtLoad
#
# Usage: bash install_launchd.sh
# Uninstall: launchctl unload ~/Library/LaunchAgents/nl.aerys.warmr.*.plist

WARMR_DIR="/Users/nemesis/warmr"
PYTHON="$WARMR_DIR/.venv/bin/python"
LOG_DIR="$WARMR_DIR/logs"
PLIST_DIR="$HOME/Library/LaunchAgents"

mkdir -p "$LOG_DIR" "$PLIST_DIR"

make_plist() {
  local label="$1"
  local script="$2"
  local interval="$3"   # seconds between runs
  local plist_path="$PLIST_DIR/nl.aerys.warmr.$label.plist"

  cat > "$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>nl.aerys.warmr.$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$WARMR_DIR/$script</string>
  </array>
  <key>WorkingDirectory</key> <string>$WARMR_DIR</string>
  <key>StartInterval</key>    <integer>$interval</integer>
  <key>RunAtLoad</key>        <true/>
  <key>StandardOutPath</key>  <string>$LOG_DIR/${label}.out.log</string>
  <key>StandardErrorPath</key> <string>$LOG_DIR/${label}.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>           <string>/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

  # Reload the agent
  launchctl unload "$plist_path" 2>/dev/null
  launchctl load "$plist_path"
  echo "  ✓ $label (every ${interval}s) → $plist_path"
}

echo "Installing launchd agents for Warmr…"

# Warmup engine — every 20 minutes (business hours enforced by engine itself)
make_plist "warmup-engine" "warmup_engine.py" 1200

# IMAP processor — every 10 minutes
make_plist "imap-processor" "imap_processor.py" 600

# Daily reset — also as StartInterval so it reruns soon after a missed midnight
make_plist "daily-reset" "daily_reset.py" 3600  # hourly re-check, the script is idempotent

# Diagnostics — every hour
make_plist "diagnostics" "diagnostics_engine.py" 3600

# DNS monitor — every 15 minutes
make_plist "dns-monitor" "dns_monitor.py" 900

echo ""
echo "Installed. To see status:"
echo "  launchctl list | grep warmr"
echo ""
echo "To tail logs:"
echo "  tail -f $LOG_DIR/warmup-engine.out.log"
echo ""
echo "To uninstall all:"
echo "  for f in $PLIST_DIR/nl.aerys.warmr.*.plist; do launchctl unload \"\$f\"; rm \"\$f\"; done"
