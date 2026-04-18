"""
diagnostics_engine.py

Warmr intelligence layer — automated anomaly detection across all active clients.

Runs as a scheduled job every hour (via n8n or standalone loop).
Performs four independent diagnostic checks and creates notifications
in the `notifications` table when issues are detected.

Checks:
  1. Reputation score drift detection (every run)
     Detects inboxes with a downward score trend over 3 consecutive days.

  2. Warmup network quality monitoring (every 6th run = every 6 hours)
     IMAP login-tests every warmup network account from .env.
     Alerts if > 20% of accounts are unreachable.

  3. SMTP connection failure pattern detection (every run)
     Pauses inboxes with ≥ 3 SMTP errors in a 60-minute window.
     Escalates to urgent notification if an inbox is auto-paused 3+ times in 24h.

  4. Inbox health forecasting (every run, cached in analytics_cache)
     Linear regression over 14 days of reputation scores.
     Creates a warning notification if projected score drops below 70 in 7 days.

Run standalone:
    python diagnostics_engine.py

Run once per hour via n8n: POST /diagnostics/run (added to main.py).

Environment variables:
    SUPABASE_URL, SUPABASE_KEY       — required
    WARMUP_NETWORK_1_EMAIL, ..._PASSWORD — warmup account credentials from .env
    (standard INBOX_* vars for SMTP/IMAP context)
"""

import imaplib
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

POLL_INTERVAL_SECONDS:  int   = 3600   # 1 hour between full cycles
NETWORK_CHECK_INTERVAL: int   = 6      # run network check every 6th cycle
IMAP_TIMEOUT:           int   = 10     # seconds per IMAP login attempt

# Reputation drift thresholds
DRIFT_MIN_DAYS:       int   = 3       # consecutive days of decline needed
DRIFT_MIN_POINTS:     float = 3.0     # total drop across those days
CRITICAL_REP_THRESHOLD: float = 35.0   # auto-pause inbox if reputation drops below this

# SMTP error thresholds
SMTP_ERROR_WINDOW_MINUTES: int = 60
SMTP_ERROR_THRESHOLD:      int = 3
AUTO_PAUSE_DURATION_HOURS: int = 2
MAX_AUTO_PAUSES_24H:       int = 3

# Forecast thresholds
FORECAST_LOOKBACK_DAYS:   int   = 14
FORECAST_HORIZON_DAYS:    int   = 7
FORECAST_DANGER_SCORE:    float = 70.0

# Network degraded threshold
NETWORK_DEGRADED_PCT:     float = 0.20   # 20% inactive → urgent alert


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _sb() -> Client:
    """Return a fresh Supabase service-role client."""
    return create_client(_SUPABASE_URL, _SUPABASE_KEY)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _all_clients(sb: Client) -> list[str]:
    """Return all distinct client_ids that have at least one active inbox."""
    resp = (
        sb.table("inboxes")
        .select("client_id")
        .neq("status", "retired")
        .execute()
    )
    seen: set[str] = set()
    ids: list[str] = []
    for row in resp.data or []:
        cid = row.get("client_id")
        if cid and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


def _create_notification(
    sb: Client,
    client_id: str,
    notification_type: str,
    message: str,
    entity_id: Optional[str] = None,
    priority: str = "normal",
) -> None:
    """Insert a row into the notifications table."""
    try:
        row: dict = {
            "client_id": client_id,
            "type":      notification_type,
            "message":   message,
            "priority":  priority,
            "read":      False,
            "timestamp": _now_utc(),
        }
        if entity_id:
            row["entity_id"] = entity_id
        sb.table("notifications").insert(row).execute()
        logger.info("Notification [%s][%s]: %s", notification_type, priority, message[:120])
    except Exception as exc:
        logger.error("Failed to create notification: %s", exc)


def _log_diagnostic(
    sb: Client,
    client_id: str,
    check_type: str,
    result: str,
    details: dict,
    entity_id: Optional[str] = None,
) -> None:
    """Append a row to diagnostics_log."""
    try:
        row: dict = {
            "client_id":  client_id,
            "check_type": check_type,
            "result":     result,
            "details":    details,
            "timestamp":  _now_utc(),
        }
        if entity_id:
            row["entity_id"] = entity_id
        sb.table("diagnostics_log").insert(row).execute()
    except Exception as exc:
        logger.warning("Failed to write diagnostics_log: %s", exc)


# ---------------------------------------------------------------------------
# CHECK 1 — Reputation drift detection
# ---------------------------------------------------------------------------

def _get_daily_reputation(
    sb: Client,
    inbox_id: str,
    days: int,
) -> list[tuple[str, float]]:
    """
    Return a list of (date_str, avg_reputation_score) for the last `days` days.

    Reads reputation_score_at_time from warmup_logs grouped by calendar day.
    Days with no logs are skipped (returned list may be shorter than `days`).
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    resp = (
        sb.table("warmup_logs")
        .select("reputation_score_at_time, timestamp")
        .eq("inbox_id", inbox_id)
        .gte("timestamp", since)
        .order("timestamp")
        .execute()
    )
    rows = resp.data or []

    # Group by date → take the last score of each day (end-of-day snapshot)
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        ts = row.get("timestamp") or ""
        score = row.get("reputation_score_at_time")
        if ts and score is not None:
            day = ts[:10]  # YYYY-MM-DD
            by_day[day].append(float(score))

    return [(day, scores[-1]) for day, scores in sorted(by_day.items())]


def _count_spam_events(
    sb: Client,
    inbox_id: str,
    since_iso: str,
) -> tuple[int, int]:
    """
    Count spam rescues and spam complaints from warmup_logs since `since_iso`.
    Returns (rescues, complaints).
    """
    resp = (
        sb.table("warmup_logs")
        .select("action, landed_in_spam")
        .eq("inbox_id", inbox_id)
        .gte("timestamp", since_iso)
        .execute()
    )
    rows = resp.data or []
    rescues    = sum(1 for r in rows if r.get("action") == "spam_rescued")
    complaints = sum(1 for r in rows if r.get("landed_in_spam"))
    return rescues, complaints


def check_reputation_drift(sb: Client, client_id: str) -> None:
    """
    Check 1: Detect inboxes whose reputation score is trending downward.

    For each active inbox, fetches daily reputation scores over the last 7 days.
    If any 3 consecutive days show a cumulative drop > DRIFT_MIN_POINTS:
      - creates a 'reputation_drift' notification with a human-readable body
      - logs to diagnostics_log
    """
    inboxes_resp = (
        sb.table("inboxes")
        .select("id, email, reputation_score, status")
        .eq("client_id", client_id)
        .neq("status", "retired")
        .execute()
    )
    inboxes = inboxes_resp.data or []

    for inbox in inboxes:
        inbox_id = inbox["id"]
        email    = inbox.get("email", "")
        current_score = float(inbox.get("reputation_score") or 50)
        current_status = inbox.get("status", "")

        # ── Critical reputation threshold check — auto-pause if below ──
        if current_score < CRITICAL_REP_THRESHOLD and current_status != "paused":
            try:
                sb.table("inboxes").update({
                    "status": "paused",
                    "warmup_active": False,
                    "notes": f"Auto-paused {datetime.now(timezone.utc).date().isoformat()}: reputation {current_score:.1f} below critical threshold {CRITICAL_REP_THRESHOLD}",
                }).eq("id", inbox_id).execute()

                _create_notification(
                    sb, client_id,
                    notification_type="inbox_auto_paused",
                    message=(
                        f"Inbox {email} is automatisch gepauzeerd. Reputatie ({current_score:.1f}) "
                        f"is gezakt onder de kritieke drempel ({CRITICAL_REP_THRESHOLD}). "
                        "Onderzoek de oorzaak en hervat handmatig."
                    ),
                    entity_id=inbox_id,
                    priority="urgent",
                )
                _log_diagnostic(
                    sb, client_id,
                    check_type="reputation_critical_pause",
                    result="critical",
                    details={
                        "inbox_id": inbox_id,
                        "email": email,
                        "current_score": current_score,
                        "threshold": CRITICAL_REP_THRESHOLD,
                        "action": "auto_paused",
                    },
                    entity_id=inbox_id,
                )
                logger.warning("Auto-paused inbox %s — reputation %.1f < %.1f", email, current_score, CRITICAL_REP_THRESHOLD)
            except Exception as exc:
                logger.error("Failed to auto-pause inbox %s: %s", email, exc)

        daily = _get_daily_reputation(sb, inbox_id, days=7)
        if len(daily) < DRIFT_MIN_DAYS:
            continue

        # Sliding window: look for any 3-day run with cumulative drop > threshold
        found_drift = False
        for i in range(len(daily) - DRIFT_MIN_DAYS + 1):
            window = daily[i : i + DRIFT_MIN_DAYS]
            start_score = window[0][1]
            end_score   = window[-1][1]
            drop        = start_score - end_score

            if drop > DRIFT_MIN_POINTS:
                # Characterise the drop
                since_iso = (
                    datetime.now(timezone.utc) - timedelta(days=DRIFT_MIN_DAYS + 1)
                ).isoformat()
                rescues, complaints = _count_spam_events(sb, inbox_id, since_iso)

                day_labels = " → ".join(d[:10] for d, _ in window)

                if complaints >= 2:
                    cause = (
                        f"{complaints} spam complaint(s) received — recipient mail providers "
                        "flagged outgoing emails. Check content quality and opt-out handling."
                    )
                    action = "Stop all campaign sends from this inbox for 48 hours. "
                    action += "Review recent email content for spam triggers."
                elif rescues >= 3:
                    cause = (
                        f"{rescues} warmup emails landed in spam and had to be rescued. "
                        "Outlook or corporate filters may be filtering by sending domain or IP."
                    )
                    action = "Reduce daily warmup volume by 30% for 48 hours. "
                    action += "Check DMARC phase and SPF alignment for the sending domain."
                else:
                    cause = (
                        "Score decline without clear spam signals. Likely cause: "
                        "reduced engagement (lower open/reply rate) from warmup network contacts."
                    )
                    action = "Verify warmup network accounts are active and replying. "
                    action += "Check network_health dashboard."

                message = (
                    f"Inbox {email} dropped {drop:.1f} points over 3 days "
                    f"({day_labels}). Primary cause: {cause} "
                    f"Recommended action: {action}"
                )

                _create_notification(
                    sb, client_id,
                    notification_type="reputation_drift",
                    message=message,
                    entity_id=inbox_id,
                    priority="high",
                )
                _log_diagnostic(
                    sb, client_id,
                    check_type="reputation_drift",
                    result="warning",
                    details={
                        "inbox_id":    inbox_id,
                        "email":       email,
                        "drop_points": round(drop, 2),
                        "days":        day_labels,
                        "rescues":     rescues,
                        "complaints":  complaints,
                        "window":      [{"day": d, "score": s} for d, s in window],
                    },
                    entity_id=inbox_id,
                )
                found_drift = True
                break  # one notification per inbox per cycle

        if not found_drift:
            _log_diagnostic(
                sb, client_id,
                check_type="reputation_drift",
                result="ok",
                details={"inbox_id": inbox_id, "email": email, "daily_scores": daily[-3:]},
                entity_id=inbox_id,
            )

    logger.info("check_reputation_drift done for client %s (%d inboxes)", client_id, len(inboxes))


# ---------------------------------------------------------------------------
# CHECK 2 — Warmup network quality monitoring
# ---------------------------------------------------------------------------

def _load_network_accounts_from_env() -> list[dict]:
    """
    Load warmup network account credentials from WARMUP_NETWORK_*_EMAIL + _PASSWORD env vars.

    Returns list of {email, password} dicts.
    """
    accounts: list[dict] = []
    i = 1
    while True:
        email = os.getenv(f"WARMUP_NETWORK_{i}_EMAIL")
        if not email:
            break
        password = os.getenv(f"WARMUP_NETWORK_{i}_PASSWORD", "")
        accounts.append({"email": email.strip(), "password": password.strip()})
        i += 1
    return accounts


def _test_imap_login(email: str, password: str) -> bool:
    """
    Attempt an IMAP SSL login to Gmail for the given account.

    Returns True if login succeeds, False on any error.
    Times out after IMAP_TIMEOUT seconds.
    """
    try:
        import socket
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as conn:
            conn.socket().settimeout(IMAP_TIMEOUT)
            conn.login(email, password)
            return True
    except Exception:
        return False


def check_network_health(sb: Client, client_id: Optional[str] = None) -> None:
    """
    Check 2: Test IMAP login for every warmup network account.

    Updates warmup_network_accounts.status per account.
    Creates an urgent 'network_degraded' notification if > 20% fail.
    Logs a snapshot to network_health_log.
    """
    accounts = _load_network_accounts_from_env()
    if not accounts:
        logger.info("No warmup network accounts found in .env — skipping network health check.")
        return

    total   = len(accounts)
    active  = 0
    now_iso = _now_utc()

    for acc in accounts:
        email    = acc["email"]
        password = acc["password"]

        # Upsert into warmup_network_accounts if not already there
        try:
            existing = (
                sb.table("warmup_network_accounts")
                .select("id, failure_count")
                .eq("email", email)
                .limit(1)
                .execute()
            ).data or []
        except Exception:
            existing = []

        ok = _test_imap_login(email, password)

        if ok:
            active += 1
            row_patch = {
                "status":              "active",
                "last_login_check":    now_iso,
                "last_login_success":  now_iso,
                "failure_count":       0,
            }
        else:
            failures = (existing[0].get("failure_count") or 0) + 1 if existing else 1
            new_status = "suspended" if failures >= 5 else "inactive"
            row_patch = {
                "status":           new_status,
                "last_login_check": now_iso,
                "failure_count":    failures,
            }

        try:
            if existing:
                sb.table("warmup_network_accounts").update(row_patch).eq("email", email).execute()
            else:
                sb.table("warmup_network_accounts").insert({
                    "email":    email,
                    "provider": "gmail",
                    "client_id": client_id,
                    **row_patch,
                }).execute()
        except Exception as exc:
            logger.warning("Failed to update network account %s: %s", email, exc)

    health_score = (active / total * 100) if total else 0.0
    inactive_pct = 1.0 - (active / total) if total else 0.0

    # Log snapshot
    try:
        sb.table("network_health_log").insert({
            "client_id":      client_id,
            "total_accounts": total,
            "active_accounts": active,
            "health_score":   round(health_score, 1),
            "timestamp":      now_iso,
        }).execute()
    except Exception as exc:
        logger.warning("Failed to write network_health_log: %s", exc)

    # Alert if degraded
    if inactive_pct > NETWORK_DEGRADED_PCT and client_id:
        inactive_count = total - active
        message = (
            f"Warmup network degraded: {inactive_count}/{total} accounts are unreachable "
            f"({inactive_pct:.0%} failure rate). Health score: {health_score:.0f}%. "
            "Warmup reply rates will drop until accounts are restored. "
            "Re-authorise Google app passwords for failed accounts."
        )
        _create_notification(
            sb, client_id,
            notification_type="network_degraded",
            message=message,
            priority="urgent",
        )

    logger.info(
        "Network health: %d/%d active (%.0f%%)",
        active, total, health_score,
    )


# ---------------------------------------------------------------------------
# CHECK 3 — SMTP error pattern detection + auto-pause
# ---------------------------------------------------------------------------

def check_smtp_errors(sb: Client, client_id: str) -> None:
    """
    Check 3: Detect SMTP error bursts and auto-pause affected inboxes.

    Queries warmup_logs for action='error' over the last 60 minutes.
    If any inbox has 3+ errors in a 60-min window:
      - Sets inboxes.status = 'paused'
      - Increments auto_pause_count_24h
      - Schedules auto-resume via auto_pause_reset_at
      - If auto_pause_count_24h >= MAX_AUTO_PAUSES_24H: escalates to urgent notification
      - Creates 'inbox_auto_paused' notification
    """
    since_iso = (
        datetime.now(timezone.utc) - timedelta(minutes=SMTP_ERROR_WINDOW_MINUTES)
    ).isoformat()
    now_utc   = datetime.now(timezone.utc)

    inboxes_resp = (
        sb.table("inboxes")
        .select("id, email, status, auto_pause_count_24h, auto_pause_reset_at")
        .eq("client_id", client_id)
        .neq("status", "retired")
        .execute()
    )
    inboxes = {i["id"]: i for i in (inboxes_resp.data or [])}

    if not inboxes:
        return

    # Fetch SMTP errors in the window
    errors_resp = (
        sb.table("warmup_logs")
        .select("inbox_id, timestamp, notes")
        .in_("inbox_id", list(inboxes.keys()))
        .eq("action", "error")
        .gte("timestamp", since_iso)
        .execute()
    )
    error_rows = errors_resp.data or []

    # Also auto-resume any paused inboxes whose pause window has expired
    for iid, inbox in inboxes.items():
        reset_at_str = inbox.get("auto_pause_reset_at")
        if inbox.get("status") == "paused" and reset_at_str:
            try:
                reset_at = datetime.fromisoformat(reset_at_str.replace("Z", "+00:00"))
                if now_utc >= reset_at:
                    sb.table("inboxes").update({
                        "status":             "warmup",
                        "auto_pause_reset_at": None,
                    }).eq("id", iid).execute()
                    # Log resumption
                    sb.table("warmup_logs").insert({
                        "inbox_id": iid,
                        "action":   "auto_resumed",
                        "notes":    "Auto-pause window expired. Inbox resumed automatically.",
                        "timestamp": _now_utc(),
                    }).execute()
                    logger.info("Auto-resumed inbox %s", inbox.get("email"))
            except Exception as exc:
                logger.warning("Failed to auto-resume inbox %s: %s", iid, exc)

    # Count errors per inbox within the window
    errors_by_inbox: dict[str, int] = defaultdict(int)
    for row in error_rows:
        iid = row.get("inbox_id")
        if iid:
            errors_by_inbox[iid] += 1

    for iid, error_count in errors_by_inbox.items():
        if error_count < SMTP_ERROR_THRESHOLD:
            continue

        inbox    = inboxes.get(iid, {})
        email    = inbox.get("email", iid)
        cur_pauses = inbox.get("auto_pause_count_24h") or 0

        # Reset 24h counter if needed
        reset_str = inbox.get("auto_pause_reset_at")
        if cur_pauses > 0 and reset_str:
            try:
                reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                if now_utc >= reset_dt + timedelta(hours=24):
                    cur_pauses = 0
            except Exception:
                pass

        if cur_pauses >= MAX_AUTO_PAUSES_24H:
            # Escalate — don't auto-pause again
            _create_notification(
                sb, client_id,
                notification_type="inbox_auto_paused",
                message=(
                    f"Inbox {email} has been auto-paused {cur_pauses} times in the last 24 hours "
                    f"due to recurring SMTP errors ({error_count} errors in the last 60 minutes). "
                    "This inbox requires manual intervention. "
                    "Possible causes: invalid app password, account suspension, or IP block."
                ),
                entity_id=iid,
                priority="urgent",
            )
            _log_diagnostic(
                sb, client_id,
                check_type="smtp_pattern",
                result="critical",
                details={"inbox_id": iid, "email": email, "errors": error_count, "pauses": cur_pauses},
                entity_id=iid,
            )
            continue

        # Auto-pause
        resume_at = (now_utc + timedelta(hours=AUTO_PAUSE_DURATION_HOURS)).isoformat()
        try:
            sb.table("inboxes").update({
                "status":                "paused",
                "auto_pause_count_24h":  cur_pauses + 1,
                "auto_pause_reset_at":   resume_at,
            }).eq("id", iid).execute()

            sb.table("warmup_logs").insert({
                "inbox_id":  iid,
                "action":    "auto_paused",
                "notes":     f"Auto-paused after {error_count} SMTP errors in 60 min. Resume at {resume_at}.",
                "timestamp": _now_utc(),
            }).execute()

        except Exception as exc:
            logger.error("Failed to auto-pause inbox %s: %s", iid, exc)
            continue

        _create_notification(
            sb, client_id,
            notification_type="inbox_auto_paused",
            message=(
                f"Inbox {email} was automatically paused after {error_count} SMTP errors "
                f"in the last 60 minutes (pause #{cur_pauses + 1} today). "
                f"It will resume automatically in {AUTO_PAUSE_DURATION_HOURS} hours. "
                "Possible causes: app password expired, daily SMTP quota exceeded, "
                "or transient provider outage."
            ),
            entity_id=iid,
            priority="high",
        )
        _log_diagnostic(
            sb, client_id,
            check_type="smtp_pattern",
            result="warning",
            details={
                "inbox_id":    iid,
                "email":       email,
                "errors":      error_count,
                "pauses":      cur_pauses + 1,
                "resume_at":   resume_at,
            },
            entity_id=iid,
        )

    logger.info("check_smtp_errors done for client %s", client_id)


# ---------------------------------------------------------------------------
# CHECK 4 — Inbox health forecasting (linear regression)
# ---------------------------------------------------------------------------

def _linear_regression(x: list[float], y: list[float]) -> tuple[float, float]:
    """
    Compute slope and intercept of the best-fit line through (x, y) pairs.

    Pure Python implementation of ordinary least squares.
    Returns (slope, intercept) such that y_hat = slope * x + intercept.
    """
    n = len(x)
    if n < 2:
        return 0.0, (y[0] if y else 0.0)

    sum_x  = sum(x)
    sum_y  = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)

    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0.0:
        return 0.0, sum_y / n

    slope     = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def check_inbox_forecast(sb: Client, client_id: str) -> None:
    """
    Check 4: Project inbox reputation scores 7 days forward using linear regression.

    For each warmup inbox with >= 5 data points over the last 14 days:
      - Fits a linear trend to daily reputation scores
      - Projects score at day+7
      - If projected score < FORECAST_DANGER_SCORE (70):
          creates 'reputation_forecast_warning' notification
          stores forecast in analytics_cache
    """
    inboxes_resp = (
        sb.table("inboxes")
        .select("id, email, reputation_score, status, warmup_start_date")
        .eq("client_id", client_id)
        .in_("status", ["warmup", "ready"])
        .execute()
    )
    inboxes = inboxes_resp.data or []
    now_utc = datetime.now(timezone.utc)

    for inbox in inboxes:
        inbox_id = inbox["id"]
        email    = inbox.get("email", "")

        daily = _get_daily_reputation(sb, inbox_id, days=FORECAST_LOOKBACK_DAYS)

        if len(daily) < 5:
            continue  # Not enough data for a meaningful forecast

        # x = day index (0, 1, 2, …), y = score
        x = list(range(len(daily)))
        y = [score for _, score in daily]

        slope, intercept = _linear_regression(x, y)

        # Project to day (len(daily) - 1 + FORECAST_HORIZON_DAYS)
        future_x    = len(daily) - 1 + FORECAST_HORIZON_DAYS
        projected_y = slope * future_x + intercept
        projected_y = max(0.0, min(100.0, projected_y))  # clamp to valid range

        # Current score for context
        current_score = y[-1]
        trend_label   = "declining" if slope < -0.2 else ("stable" if abs(slope) < 0.2 else "improving")

        # Store forecast in analytics_cache
        forecast_payload = {
            "inbox_id":       inbox_id,
            "email":          email,
            "current_score":  round(current_score, 1),
            "projected_score": round(projected_y, 1),
            "slope_per_day":  round(slope, 3),
            "trend":          trend_label,
            "data_points":    len(daily),
            "projected_at":   (now_utc + timedelta(days=FORECAST_HORIZON_DAYS)).date().isoformat(),
            "computed_at":    _now_utc(),
        }

        try:
            existing = (
                sb.table("analytics_cache")
                .select("id")
                .eq("entity_type", "inbox_forecast")
                .eq("entity_id", inbox_id)
                .limit(1)
                .execute()
            ).data or []

            if existing:
                sb.table("analytics_cache").update({
                    "metrics":    forecast_payload,
                    "updated_at": _now_utc(),
                }).eq("id", existing[0]["id"]).execute()
            else:
                sb.table("analytics_cache").insert({
                    "client_id":   client_id,
                    "entity_type": "inbox_forecast",
                    "entity_id":   inbox_id,
                    "metrics":     forecast_payload,
                    "updated_at":  _now_utc(),
                }).execute()
        except Exception as exc:
            logger.warning("Failed to cache forecast for inbox %s: %s", inbox_id, exc)

        # Create notification if projected score will drop below threshold
        if projected_y < FORECAST_DANGER_SCORE and slope < 0:
            # Estimate date when it would drop below threshold
            if slope < 0:
                days_to_threshold = (current_score - FORECAST_DANGER_SCORE) / abs(slope)
                projected_drop_date = (
                    now_utc + timedelta(days=int(days_to_threshold))
                ).date().isoformat()
            else:
                projected_drop_date = (
                    now_utc + timedelta(days=FORECAST_HORIZON_DAYS)
                ).date().isoformat()

            message = (
                f"Inbox {email} is projected to drop below campaign-ready threshold "
                f"(score 70) by approximately {projected_drop_date}. "
                f"Current score: {current_score:.0f}/100. "
                f"Projected score in 7 days: {projected_y:.0f}/100 "
                f"(trend: {slope:+.2f} points/day). "
                "Recommended action: check warmup network activity, increase reply rate, "
                "and review recent spam folder rates for this inbox."
            )
            _create_notification(
                sb, client_id,
                notification_type="reputation_forecast_warning",
                message=message,
                entity_id=inbox_id,
                priority="normal",
            )

        _log_diagnostic(
            sb, client_id,
            check_type="forecast",
            result="warning" if projected_y < FORECAST_DANGER_SCORE else "ok",
            details=forecast_payload,
            entity_id=inbox_id,
        )

    logger.info("check_inbox_forecast done for client %s (%d inboxes)", client_id, len(inboxes))


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run_diagnostics(run_network_check: bool = False) -> None:
    """
    Run all diagnostic checks for all active clients.

    Args:
        run_network_check: If True, also run the IMAP network health check.
                           Should be True every 6th cycle (every 6 hours).
    """
    sb = _sb()
    clients = _all_clients(sb)

    if not clients:
        logger.info("No active clients found — nothing to diagnose.")
        return

    logger.info("Running diagnostics for %d client(s)...", len(clients))

    for client_id in clients:
        try:
            check_reputation_drift(sb, client_id)
        except Exception as exc:
            logger.error("check_reputation_drift failed for %s: %s", client_id, exc)

        try:
            check_smtp_errors(sb, client_id)
        except Exception as exc:
            logger.error("check_smtp_errors failed for %s: %s", client_id, exc)

        try:
            check_inbox_forecast(sb, client_id)
        except Exception as exc:
            logger.error("check_inbox_forecast failed for %s: %s", client_id, exc)

    if run_network_check:
        # Network check is client-agnostic (shared .env accounts)
        try:
            check_network_health(sb, client_id=clients[0] if clients else None)
        except Exception as exc:
            logger.error("check_network_health failed: %s", exc)

    logger.info("Diagnostics cycle complete.")


def main() -> None:
    """
    Entry point for standalone loop.

    Tracks cycle count to trigger network check every 6th run.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Exiting.")
        raise SystemExit(1)

    logger.info("Diagnostics engine started. Poll interval: %ds", POLL_INTERVAL_SECONDS)
    cycle = 0

    while True:
        try:
            run_network = (cycle % NETWORK_CHECK_INTERVAL == 0)
            run_diagnostics(run_network_check=run_network)
        except Exception as exc:
            logger.error("Unhandled diagnostics cycle error: %s", exc)

        cycle += 1
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
