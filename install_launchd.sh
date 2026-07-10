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

make_calendar_plist() {
  local label="$1"
  local script="$2"
  local hour="$3"     # 0-23
  local minute="$4"   # 0-59
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
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>    <integer>$hour</integer>
    <key>Minute</key>  <integer>$minute</integer>
  </dict>
  <key>RunAtLoad</key>        <false/>
  <key>StandardOutPath</key>  <string>$LOG_DIR/${label}.out.log</string>
  <key>StandardErrorPath</key> <string>$LOG_DIR/${label}.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>           <string>/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

  launchctl unload "$plist_path" 2>/dev/null
  launchctl load "$plist_path"
  echo "  ✓ $label (daily $(printf '%02d:%02d' "$hour" "$minute")) → $plist_path"
}

echo "Installing launchd agents for Warmr…"

# Warmup engine — every 20 minutes (business hours enforced by engine itself)
make_plist "warmup-engine" "warmup_engine.py" 1200

# IMAP processor — every 10 minutes
make_plist "imap-processor" "imap_processor.py" 600

# Daily reset — run ONCE per day at 00:05 (StartCalendarInterval), NOT hourly.
# Running it hourly (StartInterval 3600) zeroed daily_sent mid-day and defeated
# the daily caps. daily_reset.py now also has a last_reset_date guard, but the
# schedule must not fire it 24x/day. launchd catches a missed midnight on wake.
make_calendar_plist "daily-reset" "daily_reset.py" 0 5

# Diagnostics — every hour
make_plist "diagnostics" "diagnostics_engine.py" 3600

# DNS monitor — every 15 minutes
make_plist "dns-monitor" "dns_monitor.py" 900

# Bounce handler — every 30 minutes (scans IMAP for DSNs + ARF complaints)
make_plist "bounce-handler" "bounce_handler.py" 1800

# Weekly report — hourly poll, script itself only runs on Mondays
make_plist "weekly-report" "weekly_report.py" 3600

# Reap stranded sends — every 10 minutes. Reverts campaign_leads stuck in
# 'sending' (process crash between the atomic claim and completion) back to
# 'active' after REAP_STRANDED_MINUTES (default 30) so they get retried.
make_plist "reap-stranded-sends" "reap_stranded_sends.py" 600

echo ""
echo "Installed. To see status:"
echo "  launchctl list | grep warmr"
echo ""
echo "To tail logs:"
echo "  tail -f $LOG_DIR/warmup-engine.out.log"
echo ""
echo "To uninstall all:"
echo "  for f in $PLIST_DIR/nl.aerys.warmr.*.plist; do launchctl unload \"\$f\"; rm \"\$f\"; done"
