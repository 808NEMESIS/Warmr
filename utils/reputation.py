"""
utils/reputation.py — canonical reputation_score delta table + atomic update
helper, shared by imap_processor.py, bounce_handler.py, warmup_engine.py
(Fase 3, KPI-tracking fixes).

Previously imap_processor.py and bounce_handler.py each kept their own
REPUTATION_DELTA dict with inconsistent naming (imap_processor.py's dict
even carried three dead keys — soft_bounce/hard_bounce/spam_complaint —
that were actually bounce_handler.py's domain under different names). This
module is the single source of truth, matching CLAUDE.md's "Reputation
Score Logic" table exactly.

bump_reputation() applies the delta atomically via the apply_reputation_delta
RPC (api/reputation_migration.sql) — the same read-then-write pattern used
to update reputation_score directly was a lost-update race: warmup sends,
IMAP receives, and bounce handling all run on independent schedules against
the same inbox. Falls back to a non-atomic read-then-write when the RPC
isn't available yet, exactly like warmup_engine.update_daily_sent (Fase 0/1).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REPUTATION_DELTA: dict[str, float] = {
    "sent": 0.2,
    "received": 0.5,
    "spam_rescued": 1.0,
    "opened": 0.3,
    "soft_bounce": -2.0,
    "hard_bounce": -5.0,
    "spam_complaint": -20.0,
}


def bump_reputation(
    sb,
    inbox_id: str,
    events: dict[str, int],
    current_score: float | None = None,
) -> float:
    """
    Apply the combined reputation delta for `events` (event name -> count) to
    an inbox. Returns the resulting score.

    Tries the atomic apply_reputation_delta RPC first; falls back to a
    non-atomic read-then-write using `current_score` (or a fresh read if not
    supplied) if the RPC isn't available yet.
    """
    delta = sum(REPUTATION_DELTA.get(event, 0.0) * count for event, count in events.items())

    try:
        resp = sb.rpc("apply_reputation_delta", {"p_inbox_id": inbox_id, "p_delta": delta}).execute()
        data = resp.data
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, (int, float)):
            return float(data)
    except Exception as exc:
        logger.debug("apply_reputation_delta RPC failed for inbox %s, falling back: %s", inbox_id, exc)

    # Fallback: non-atomic read-then-write (for backwards compatibility
    # before the migration is applied).
    base = current_score
    if base is None:
        try:
            row = sb.table("inboxes").select("reputation_score").eq("id", inbox_id).limit(1).execute()
            base = float((row.data or [{}])[0].get("reputation_score") or 50)
        except Exception as exc:
            logger.error("Failed to read reputation_score for inbox %s: %s", inbox_id, exc)
            base = 50.0

    new_score = max(0.0, min(100.0, base + delta))
    try:
        sb.table("inboxes").update(
            {"reputation_score": round(new_score, 2), "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", inbox_id).execute()
    except Exception as exc:
        logger.error("Failed to update reputation for inbox %s: %s", inbox_id, exc)

    return new_score
