"""
engagement_scorer.py — Lead engagement scoring with time decay.

Scoring events:
  +5   email opened
  +10  link clicked
  +25  replied
  +50  interested (classified)
  -2   per day without activity (decay)
  -10  bounced

Called from tracking endpoints (open/click) and reply classifier.
Daily decay is applied by the daily_reset cron.

Score range: 0-100 (clamped).
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()
logger = logging.getLogger(__name__)

SCORES = {
    "opened": 5,
    "clicked": 10,
    "replied": 25,
    "interested": 50,
    "bounced": -10,
}

DAILY_DECAY = 2.0
MAX_SCORE = 100.0


def _get_sb() -> Client:
    return create_client(os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", ""))


def add_engagement(
    sb: Client,
    lead_id: str,
    event_type: str,
) -> float | None:
    """Add engagement points for a lead event. Returns new score or None on failure."""
    points = SCORES.get(event_type)
    if points is None:
        return None

    try:
        resp = sb.table("leads").select("engagement_score").eq("id", lead_id).limit(1).execute()
        if not resp.data:
            return None
        current = float(resp.data[0].get("engagement_score") or 0)
        new_score = max(0, min(MAX_SCORE, current + points))

        sb.table("leads").update({
            "engagement_score": new_score,
            "engagement_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", lead_id).execute()

        logger.debug("Lead %s: engagement %+d → %.1f (event: %s)", lead_id, points, new_score, event_type)
        return new_score
    except Exception as exc:
        logger.error("Failed to update engagement for lead %s: %s", lead_id, exc)
        return None


def apply_daily_decay(sb: Client | None = None) -> int:
    """
    Apply daily score decay to all leads with engagement_score > 0.

    Called by daily_reset cron. Reduces score by DAILY_DECAY per day
    since last engagement activity. Leads with recent activity (today)
    are not decayed.

    Returns number of leads decayed.
    """
    sb = sb or _get_sb()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    try:
        # Fetch leads with score > 0 that haven't been active today
        resp = (
            sb.table("leads")
            .select("id, engagement_score, engagement_updated_at")
            .gt("engagement_score", 0)
            .lt("engagement_updated_at", cutoff)
            .limit(1000)
            .execute()
        )
        leads = resp.data or []
        decayed = 0

        for lead in leads:
            lead_id = lead["id"]
            current = float(lead.get("engagement_score") or 0)
            last_updated = lead.get("engagement_updated_at", "")

            # Calculate days since last activity
            if last_updated:
                try:
                    last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                    days_inactive = (datetime.now(timezone.utc) - last_dt).days
                except Exception:
                    days_inactive = 1
            else:
                days_inactive = 1

            decay = DAILY_DECAY * days_inactive
            new_score = max(0, current - decay)

            if new_score != current:
                sb.table("leads").update({
                    "engagement_score": round(new_score, 1),
                }).eq("id", lead_id).execute()
                decayed += 1

        logger.info("Engagement decay applied to %d leads (of %d eligible).", decayed, len(leads))
        return decayed
    except Exception as exc:
        logger.error("Engagement decay failed: %s", exc)
        return 0
