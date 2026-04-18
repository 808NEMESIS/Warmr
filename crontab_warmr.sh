#!/bin/bash
# Warmr — Crontab installer
# Run: bash crontab_warmr.sh
#
# This script installs all Warmr scheduled jobs into the current user's crontab.
# Logs go to /Users/nemesis/warmr/logs/ (one file per script, rotated daily).

WARMR_DIR="/Users/nemesis/warmr"
PYTHON="/Users/nemesis/warmr/.venv/bin/python"
LOG_DIR="/Users/nemesis/warmr/logs"
API="http://localhost:8000"
TOKEN=$(grep '^WARMR_API_TOKEN=' "$WARMR_DIR/.env" | cut -d= -f2 | cut -d' ' -f1)

mkdir -p "$LOG_DIR"

# Build the crontab entries
CRON_ENTRIES=$(cat <<'CRON'
# ══════════════════════════════════════════════════════════════
# Warmr — Automated schedules
# Installed by crontab_warmr.sh — do not edit manually
# ══════════════════════════════════════════════════════════════

# ── Warmup engine: every 20 min, 07:00-18:40, Mon-Fri ──
*/20 7-18 * * 1-5 cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python warmup_engine.py >> /Users/nemesis/warmr/logs/warmup_engine.log 2>&1

# ── IMAP processor: every 10 min (spam rescue + replies) ──
*/10 * * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python imap_processor.py >> /Users/nemesis/warmr/logs/imap_processor.log 2>&1

# ── Daily reset: midnight + 5 min ──
5 0 * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python daily_reset.py >> /Users/nemesis/warmr/logs/daily_reset.log 2>&1

# ── Diagnostics: every hour ──
0 * * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python diagnostics_engine.py >> /Users/nemesis/warmr/logs/diagnostics.log 2>&1

# ── DNS monitor: every 15 min ──
*/15 * * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python dns_monitor.py >> /Users/nemesis/warmr/logs/dns_monitor.log 2>&1

# ── Blacklist checker: daily 06:00 ──
0 6 * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python blacklist_checker.py >> /Users/nemesis/warmr/logs/blacklist.log 2>&1

# ── Bounce handler: every 30 min ──
*/30 * * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python bounce_handler.py >> /Users/nemesis/warmr/logs/bounces.log 2>&1

# ── Analytics aggregation: daily 00:30 (after daily reset) ──
30 0 * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python analytics_engine.py >> /Users/nemesis/warmr/logs/analytics.log 2>&1

# ── Weekly report: Monday 08:00 ──
0 8 * * 1 cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python weekly_report.py >> /Users/nemesis/warmr/logs/weekly_report.log 2>&1

# ── Daily briefing: weekdays 07:45 ──
45 7 * * 1-5 cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python daily_briefing.py >> /Users/nemesis/warmr/logs/daily_briefing.log 2>&1

# ── AB optimizer: every 6 hours ──
0 */6 * * * cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python ab_optimizer.py >> /Users/nemesis/warmr/logs/ab_optimizer.log 2>&1

# ── Sequence analyzer: Monday 07:00 ──
0 7 * * 1 cd /Users/nemesis/warmr && /Users/nemesis/warmr/.venv/bin/python sequence_analyzer.py >> /Users/nemesis/warmr/logs/sequence_analyzer.log 2>&1

# ── Log rotation: daily 23:55 — archive and truncate ──
55 23 * * * for f in /Users/nemesis/warmr/logs/*.log; do mv "$f" "${f}.$(date +\%Y\%m\%d)" 2>/dev/null; done

CRON
)

# Preserve existing non-Warmr crontab entries
EXISTING=$(crontab -l 2>/dev/null | grep -v "# .*Warmr" | grep -v "warmr/" | grep -v "^$" | grep -v "^#.*crontab_warmr")

# Combine and install
echo "$EXISTING"$'\n'"$CRON_ENTRIES" | crontab -

echo "✓ Warmr crontabs installed. Verify with: crontab -l"
echo "  Logs dir: $LOG_DIR"
echo ""
echo "  Schedules:"
echo "    Warmup engine     — every 20 min (07-19, Mon-Fri)"
echo "    IMAP processor    — every 10 min"
echo "    Daily reset       — 00:05"
echo "    Diagnostics       — every hour"
echo "    DNS monitor       — every 15 min"
echo "    Blacklist checker — 06:00 daily"
echo "    Bounce handler    — every 30 min"
echo "    Analytics         — 00:30 daily"
echo "    Weekly report     — Mon 08:00"
echo "    Daily briefing    — weekdays 07:45"
echo "    AB optimizer      — every 6 hours"
echo "    Sequence analyzer — Mon 07:00"
echo "    Log rotation      — 23:55 daily"
