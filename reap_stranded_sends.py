"""
reap_stranded_sends.py — revert campaign_leads stranded in 'sending'.

The atomic claim in campaign_scheduler.py (Critical 5) flips a lead's
status 'active' -> 'sending' before attempting an SMTP send, and reverts it
back to 'active' if the send raises an exception. But a process crash,
kill -9, or host restart between the claim and completion skips that
revert entirely — the lead is then stranded in 'sending' forever, since
campaign_scheduler's loader only ever picks up status='active' rows.

This job finds campaign_leads rows where status='sending' and
status_changed_at is older than REAP_STRANDED_MINUTES (default 30 — well
past any legitimate single-send SMTP timeout) and reverts them to
'active' so the next scheduler run retries them normally.

Idempotent and safely re-callable: a lead already reverted (or genuinely
still mid-send within the window) is simply not matched by the query.

Called every N minutes via launchd (see install_launchd.sh). Guarded by
the same job_lock mechanism as the other scheduled jobs so overlapping
runs cannot double-process the same stranded rows.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

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

REAP_STRANDED_MINUTES = int(os.getenv("REAP_STRANDED_MINUTES", "30"))


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL/KEY not set.")
        return 1

    from utils.job_lock import job_lock
    with job_lock("reap_stranded_sends") as acquired:
        if not acquired:
            logger.info("reap_stranded_sends already running elsewhere — skipping.")
            return 0
        return _run()


def reap(sb, minutes: int = REAP_STRANDED_MINUTES) -> list[dict]:
    """
    Revert campaign_leads stuck in 'sending' for over `minutes` back to
    'active'. Returns the reverted rows. Pure DB operation, no logging/env —
    kept separate from _run() so it's directly testable against a fake.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    resp = (
        sb.table("campaign_leads")
        .update({
            "status": "active",
            "status_changed_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("status", "sending")
        .lt("status_changed_at", cutoff)
        .execute()
    )
    return resp.data or []


def _run() -> int:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    try:
        reaped = reap(sb, REAP_STRANDED_MINUTES)
    except Exception as exc:
        logger.error("Failed to reap stranded sends: %s", exc)
        return 1

    if reaped:
        logger.warning(
            "Reaped %d campaign_lead(s) stranded in 'sending' for over %d minute(s): %s",
            len(reaped), REAP_STRANDED_MINUTES,
            ", ".join(r["id"] for r in reaped),
        )
    else:
        logger.info("No stranded sends found (older than %d minutes).", REAP_STRANDED_MINUTES)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
