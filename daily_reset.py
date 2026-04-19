"""
daily_reset.py — reset per-inbox daily counters and apply engagement decay.

Called at 00:05 via cron/launchd, and safely re-callable (idempotent).

Actions:
  1. Set inboxes.daily_sent = 0 for all non-retired inboxes
  2. Apply engagement score decay to all leads
  3. Check nurture re-engagement (leads whose cooldown has expired)
  4. Snapshot funnel distribution per client for trend analytics

Idempotency: re-running within the same calendar day is safe — the counter
reset is a no-op (already 0), decay skips leads that were active today,
and the funnel snapshot uses UPSERT.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL/KEY not set.")
        return 1

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    today = date.today().isoformat()

    # 1. Reset daily_sent counters
    try:
        resp = sb.table("inboxes").update({
            "daily_sent": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).neq("status", "retired").execute()
        logger.info("Reset daily_sent for %d inbox(es).", len(resp.data or []))
    except Exception as exc:
        logger.error("Failed to reset daily_sent: %s", exc)

    # 2. Engagement decay
    try:
        from engagement_scorer import apply_daily_decay
        decayed = apply_daily_decay(sb)
        logger.info("Engagement decay applied to %d lead(s).", decayed)
    except Exception as exc:
        logger.warning("Engagement decay failed: %s", exc)

    # 3. Nurture re-engagement — per client
    try:
        from funnel_engine import check_nurture_reengagement, snapshot_funnel
        clients = sb.table("clients").select("id").eq("suspended", False).execute().data or []
        for c in clients:
            try:
                reengaged = check_nurture_reengagement(sb, c["id"])
                if reengaged:
                    logger.info("Client %s: %d lead(s) re-engaged from nurture.", c["id"][:8], len(reengaged))
            except Exception as exc:
                logger.warning("Re-engagement failed for client %s: %s", c["id"][:8], exc)

            # 4. Funnel snapshot
            try:
                snapshot_funnel(sb, c["id"])
            except Exception as exc:
                logger.debug("Funnel snapshot failed for client %s: %s", c["id"][:8], exc)
    except Exception as exc:
        logger.warning("Funnel/nurture processing skipped: %s", exc)

    logger.info("Daily reset complete for %s.", today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
