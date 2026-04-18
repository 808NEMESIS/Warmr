"""
send_time_optimizer.py

Learns optimal send times for each campaign by analysing historical open data.

Logic:
  1. For each campaign: query email_events grouped by (day_of_week, hour_of_day)
  2. Calculate open rate per slot: opens / sends  (minimum MIN_SENDS_PER_SLOT = 20 to be reliable)
  3. Identify the top 3 slots by open rate
  4. Store in analytics_cache with entity_type = 'send_time_recommendation'

Used by campaign_scheduler.py:
  - Before scheduling a send, check if a recommendation exists
  - If current time is not in a top-3 slot: defer up to MAX_DEFER_HOURS (4 hours)
  - If defer would exceed 4 hours: send anyway

Provider match rate tracking:
  - Weekly: log how often recipient provider matched inbox provider
  - Stored in analytics_cache with entity_type = 'provider_match_rate'

Run standalone:
    python send_time_optimizer.py

Or call check_send_time(campaign_id, supabase) directly from campaign_scheduler.
"""

import logging
import os
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

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

MIN_SENDS_PER_SLOT: int   = 20   # minimum sends before a slot is statistically reliable
MAX_DEFER_HOURS:    int   = 4    # never defer a send more than this many hours
TOP_SLOTS:          int   = 3    # number of optimal slots to recommend
LOOKBACK_DAYS:      int   = 30   # how far back to analyse open data

# Day-of-week labels for human-readable output
DOW_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _sb() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def compute_send_time_recommendations(
    sb: Client,
    campaign_id: str,
    client_id: str,
) -> Optional[dict]:
    """
    Analyse open events for a campaign and return top-3 send time recommendations.

    Returns None if there is insufficient data.
    Stores result in analytics_cache.

    Returns dict:
        {
            "campaign_id": str,
            "top_slots": [{"day_of_week": int, "hour": int, "open_rate": float, "sends": int}, ...],
            "slot_grid": {"{dow}_{hour}": {"sends": int, "opens": int, "open_rate": float}},
            "min_sends_threshold": int,
            "analysed_sends": int,
            "computed_at": str,
        }
    """
    since_iso = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).isoformat()

    # Fetch all sent + opened events for this campaign
    sends_resp = (
        sb.table("email_events")
        .select("timestamp, ab_variant")
        .eq("campaign_id", campaign_id)
        .eq("event_type", "sent")
        .gte("timestamp", since_iso)
        .execute()
    )
    opens_resp = (
        sb.table("email_events")
        .select("timestamp")
        .eq("campaign_id", campaign_id)
        .eq("event_type", "opened")
        .gte("timestamp", since_iso)
        .execute()
    )

    sends = sends_resp.data or []
    opens = opens_resp.data or []

    if not sends:
        return None

    # Group sends by (day_of_week, hour)
    slot_sends: dict[tuple[int, int], int] = defaultdict(int)
    for row in sends:
        ts_str = row.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            key = (ts.weekday(), ts.hour)  # weekday(): 0=Monday, 6=Sunday
            slot_sends[key] += 1
        except Exception:
            continue

    # Group opens by (day_of_week, hour) — use open timestamp as proxy for sent time
    slot_opens: dict[tuple[int, int], int] = defaultdict(int)
    for row in opens:
        ts_str = row.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            key = (ts.weekday(), ts.hour)
            slot_opens[key] += 1
        except Exception:
            continue

    # Build slot grid with open rates
    slot_grid: dict[str, dict] = {}
    qualified_slots: list[tuple[float, int, int]] = []  # (open_rate, dow, hour)

    for (dow, hour), send_count in slot_sends.items():
        open_count = slot_opens.get((dow, hour), 0)
        open_rate  = open_count / send_count if send_count > 0 else 0.0
        slot_key   = f"{dow}_{hour}"

        slot_grid[slot_key] = {
            "day_of_week":      dow,
            "day_label":        DOW_LABELS[dow],
            "hour":             hour,
            "sends":            send_count,
            "opens":            open_count,
            "open_rate":        round(open_rate, 4),
            "reliable":         send_count >= MIN_SENDS_PER_SLOT,
        }

        if send_count >= MIN_SENDS_PER_SLOT:
            qualified_slots.append((open_rate, dow, hour))

    if not qualified_slots:
        logger.info(
            "Campaign %s has no slots with >= %d sends — cannot recommend yet.",
            campaign_id, MIN_SENDS_PER_SLOT,
        )
        return None

    # Top 3 slots by open rate
    qualified_slots.sort(reverse=True)
    top = [
        {
            "day_of_week":  dow,
            "day_label":    DOW_LABELS[dow],
            "hour":         hour,
            "open_rate":    round(rate, 4),
            "sends":        slot_sends[(dow, hour)],
            "label":        f"{DOW_LABELS[dow]} {hour:02d}:00–{hour:02d}:59 UTC",
        }
        for rate, dow, hour in qualified_slots[:TOP_SLOTS]
    ]

    result = {
        "campaign_id":         campaign_id,
        "top_slots":           top,
        "slot_grid":           slot_grid,
        "min_sends_threshold": MIN_SENDS_PER_SLOT,
        "analysed_sends":      sum(slot_sends.values()),
        "computed_at":         _now_utc(),
    }

    # Upsert into analytics_cache
    try:
        existing = (
            sb.table("analytics_cache")
            .select("id")
            .eq("entity_type", "send_time_recommendation")
            .eq("entity_id", campaign_id)
            .limit(1)
            .execute()
        ).data or []

        if existing:
            sb.table("analytics_cache").update({
                "metrics":    result,
                "updated_at": _now_utc(),
            }).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("analytics_cache").insert({
                "client_id":   client_id,
                "entity_type": "send_time_recommendation",
                "entity_id":   campaign_id,
                "metrics":     result,
                "updated_at":  _now_utc(),
            }).execute()
    except Exception as exc:
        logger.warning("Failed to cache send time recommendation for %s: %s", campaign_id, exc)

    logger.info(
        "Campaign %s — top slot: %s (open rate %.1f%%)",
        campaign_id, top[0]["label"] if top else "none", (top[0]["open_rate"] * 100) if top else 0,
    )
    return result


# ---------------------------------------------------------------------------
# Scheduler integration helpers
# ---------------------------------------------------------------------------

def get_cached_recommendation(sb: Client, campaign_id: str) -> Optional[dict]:
    """
    Fetch the cached send time recommendation for a campaign from analytics_cache.

    Returns None if no recommendation exists (insufficient data or not yet computed).
    """
    try:
        resp = (
            sb.table("analytics_cache")
            .select("metrics")
            .eq("entity_type", "send_time_recommendation")
            .eq("entity_id", campaign_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0]["metrics"] if rows else None
    except Exception:
        return None


def get_next_optimal_send_time(
    sb: Client,
    campaign_id: str,
    from_time: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Return the next optimal send time for this campaign based on cached recommendations.

    If the current time is already in a top-3 slot: returns None (send now).
    If the next optimal slot is within MAX_DEFER_HOURS: return that datetime.
    If all optimal slots are more than MAX_DEFER_HOURS away: return None (send now anyway).

    Args:
        sb:          Supabase client.
        campaign_id: Campaign to check.
        from_time:   Reference time (defaults to now UTC).

    Returns:
        datetime to schedule the send, or None to send immediately.
    """
    rec = get_cached_recommendation(sb, campaign_id)
    if not rec or not rec.get("top_slots"):
        return None  # No recommendation — send now

    now = from_time or datetime.now(timezone.utc)
    top_slots = rec["top_slots"]

    # Build set of optimal (dow, hour) pairs
    optimal: set[tuple[int, int]] = {
        (s["day_of_week"], s["hour"]) for s in top_slots
    }

    # Check if current time slot is already optimal
    if (now.weekday(), now.hour) in optimal:
        return None  # Already in a good slot — send now

    # Search forward up to MAX_DEFER_HOURS for the next optimal slot
    for delta_hours in range(1, MAX_DEFER_HOURS + 1):
        candidate = now + timedelta(hours=delta_hours)
        if (candidate.weekday(), candidate.hour) in optimal:
            # Schedule at the start of that hour
            return candidate.replace(minute=0, second=0, microsecond=0)

    # No optimal slot within 4 hours — send now rather than wait
    return None


# ---------------------------------------------------------------------------
# Provider match rate tracking
# ---------------------------------------------------------------------------

_GOOGLE_DOMAINS:    frozenset[str] = frozenset({"gmail.com", "googlemail.com", "google.com"})
_MICROSOFT_DOMAINS: frozenset[str] = frozenset({
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "outlook.nl", "hotmail.nl", "live.nl",
    "outlook.be", "hotmail.be",
})


def detect_recipient_provider(email: str) -> str:
    """
    Detect the mail provider of a recipient from their email domain.

    Returns: 'google' | 'microsoft' | 'unknown'
    """
    if "@" not in email:
        return "unknown"
    domain = email.split("@")[1].lower()
    if domain in _GOOGLE_DOMAINS:
        return "google"
    if domain in _MICROSOFT_DOMAINS:
        return "microsoft"
    return "unknown"


def log_provider_match_rate(
    sb: Client,
    client_id: str,
    total_sends: int,
    matched_sends: int,
) -> None:
    """
    Upsert a weekly provider_match_rate snapshot into analytics_cache.

    Called by campaign_scheduler at the end of each weekly cycle.
    """
    if total_sends == 0:
        return
    rate = matched_sends / total_sends

    now_week = datetime.now(timezone.utc).strftime("%Y-W%W")  # e.g. 2026-W14

    payload = {
        "week":           now_week,
        "total_sends":    total_sends,
        "matched_sends":  matched_sends,
        "match_rate":     round(rate, 4),
        "computed_at":    _now_utc(),
    }

    try:
        existing = (
            sb.table("analytics_cache")
            .select("id")
            .eq("entity_type", "provider_match_rate")
            .eq("client_id", client_id)
            .eq("metrics->week", now_week)
            .limit(1)
            .execute()
        ).data or []

        if existing:
            sb.table("analytics_cache").update({
                "metrics":    payload,
                "updated_at": _now_utc(),
            }).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("analytics_cache").insert({
                "client_id":   client_id,
                "entity_type": "provider_match_rate",
                "entity_id":   None,
                "metrics":     payload,
                "updated_at":  _now_utc(),
            }).execute()
    except Exception as exc:
        logger.warning("Failed to log provider_match_rate: %s", exc)


# ---------------------------------------------------------------------------
# Bulk run — compute recommendations for all active campaigns
# ---------------------------------------------------------------------------

def run_all_campaigns(sb: Optional[Client] = None) -> None:
    """
    Compute send time recommendations for all active campaigns.

    Called once per day (e.g. after midnight analytics run).
    """
    if sb is None:
        sb = _sb()

    campaigns_resp = (
        sb.table("campaigns")
        .select("id, client_id")
        .eq("status", "active")
        .execute()
    )
    campaigns = campaigns_resp.data or []

    if not campaigns:
        logger.info("No active campaigns to analyse.")
        return

    logger.info("Computing send time recommendations for %d campaigns...", len(campaigns))
    for camp in campaigns:
        try:
            compute_send_time_recommendations(sb, camp["id"], camp["client_id"])
        except Exception as exc:
            logger.error("Failed to compute recommendations for campaign %s: %s", camp["id"], exc)


if __name__ == "__main__":
    run_all_campaigns()
