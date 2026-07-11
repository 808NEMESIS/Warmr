"""
auto_promote.py — promote warmup-inboxes to ready when they meet all criteria.

Used by the POST /inboxes/{inbox_id}/check-promotion endpoint. Pure function:
fetches one inbox row, evaluates 6 criteria, optionally updates status + logs.

Promotion criteria (ALL must be true):
  - warmup_active = true
  - days_in_warmup >= 28  (>=4 weeks since warmup_start_date)
  - reputation_score >= 70
  - last_spam_incident IS NULL  OR  last_spam_incident < NOW() - 14 days
  - geen auto-pauzes in de laatste 7 dagen (inbox_status_log-venster;
    verving de kolom auto_pause_count_24h die nooit gereset werd — W1-2)
  - daily_warmup_target >= 50  (full volume reached)

On promotion: status flips from 'warmup' to 'ready'. warmup_active stays True
(Warmr continues reputation-maintenance traffic on lower volume).

Never raises — returns a structured dict per the briefing spec. Caller
decides what to do with the outcome.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.status_log import log_status_transition

logger = logging.getLogger(__name__)


_PROMOTION_DAYS_REQUIRED = 28
_SPAM_INCIDENT_CLEAR_DAYS = 14
_MIN_REPUTATION = 70
_MIN_TARGET = 50


def _parse_ts(value: Any) -> datetime | None:
    """Parse a Supabase timestamp string into a tz-aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_PAUSE_WINDOW_DAYS = 7


def _recent_pause_count(supabase, inbox_id: str, now: datetime) -> int:
    """Pauzes in de laatste 7 dagen — venster op timestamps, geen teller.

    W1-2 (mailbox health plan): het oude criterium las auto_pause_count_24h,
    een kolom die alleen opgehoogd en NOOIT gepersisteerd gereset werd —
    één auto-pauze ooit betekende levenslang geen promotie. Nu tellen we
    échte pauze-events in een écht venster.

    Primaire bron: inbox_status_log (migratie 2026-07-11). Fallback:
    warmup_logs action='auto_paused' (historische data, en dekking zolang
    de migratie nog niet gedraaid is). FAIL-CLOSED: kunnen we de historie
    niet lezen, dan promoot je niet — ready is een privilege.
    """
    since = (now - timedelta(days=_PAUSE_WINDOW_DAYS)).isoformat()
    try:
        res = (
            supabase.table("inbox_status_log").select("id")
            .eq("inbox_id", inbox_id).eq("to_status", "paused")
            .gte("created_at", since).execute()
        )
        return len(res.data or [])
    except Exception:
        try:
            res = (
                supabase.table("warmup_logs").select("id")
                .eq("inbox_id", inbox_id).eq("action", "auto_paused")
                .gte("timestamp", since).execute()
            )
            return len(res.data or [])
        except Exception as exc:
            logger.error(
                "auto_promote: pauze-historie onleesbaar voor inbox %s: %s — "
                "FAIL-CLOSED (geen promotie).", inbox_id, exc,
            )
            return 999


def _evaluate_criteria(inbox: dict, now: datetime, recent_pauses: int) -> dict:
    """Compute the criteria_status dict per briefing spec."""
    warmup_active = bool(inbox.get("warmup_active"))

    warmup_start = _parse_ts(inbox.get("warmup_start_date"))
    days_in_warmup = (now - warmup_start).days if warmup_start else 0

    reputation_score = int(inbox.get("reputation_score") or 0)

    spam_incident = _parse_ts(inbox.get("last_spam_incident"))
    spam_clear = (
        spam_incident is None
        or spam_incident < now - timedelta(days=_SPAM_INCIDENT_CLEAR_DAYS)
    )

    # W1-2: venster i.p.v. de nooit-gerestte teller
    no_auto_pauses = recent_pauses == 0

    target_reached = int(inbox.get("daily_warmup_target") or 0) >= _MIN_TARGET

    return {
        "warmup_active": warmup_active,
        "days_in_warmup": days_in_warmup,
        "reputation_score": reputation_score,
        "spam_clear": spam_clear,
        "no_auto_pauses": no_auto_pauses,
        "recent_pauses_7d": recent_pauses,
        "target_reached": target_reached,
    }


def _decline_reason(criteria: dict) -> str:
    """Determine the first failed criterion. Order = checked priority."""
    if not criteria["warmup_active"]:
        return "warmup_inactive"
    if criteria["days_in_warmup"] < _PROMOTION_DAYS_REQUIRED:
        return "insufficient_days"
    if criteria["reputation_score"] < _MIN_REPUTATION:
        return "low_reputation"
    if not criteria["spam_clear"]:
        return "recent_spam_incident"
    if not criteria["no_auto_pauses"]:
        return "recent_auto_pauses"
    if not criteria["target_reached"]:
        return "target_not_reached"
    return "promoted"


def check_and_promote_inbox(inbox_id: str, supabase) -> dict:
    """
    Evaluate an inbox against promotion criteria and update status if eligible.

    Args:
        inbox_id: Inbox UUID.
        supabase: Supabase client (sync, the same one the rest of Warmr uses).

    Returns:
        Result dict per the briefing spec:
        {
            "inbox_id": str,
            "promoted": bool,
            "reason": str,
            "criteria_status": {...},
            "previous_status": str,
            "new_status": str | None,
        }
    """
    res = supabase.table("inboxes").select("*").eq("id", inbox_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        return {
            "inbox_id": inbox_id,
            "promoted": False,
            "reason": "not_found",
            "criteria_status": {},
            "previous_status": None,
            "new_status": None,
        }

    inbox = rows[0]
    previous_status = inbox.get("status")
    now = datetime.now(timezone.utc)

    # Idempotency: already-ready inboxes return immediately, no re-evaluation.
    if previous_status == "ready":
        return {
            "inbox_id": inbox_id,
            "promoted": False,
            "reason": "already_ready",
            "criteria_status": _evaluate_criteria(
                inbox, now, _recent_pause_count(supabase, inbox_id, now)),
            "previous_status": "ready",
            "new_status": None,
        }

    criteria = _evaluate_criteria(
        inbox, now, _recent_pause_count(supabase, inbox_id, now))
    reason = _decline_reason(criteria)

    if reason != "promoted":
        return {
            "inbox_id": inbox_id,
            "promoted": False,
            "reason": reason,
            "criteria_status": criteria,
            "previous_status": previous_status,
            "new_status": None,
        }

    # All criteria pass → promote.
    new_status = "ready"
    supabase.table("inboxes").update({
        "status": new_status,
        # warmup_active stays True — Warmr keeps reputation-maintenance traffic.
        "updated_at": now.isoformat(),
    }).eq("id", inbox_id).execute()
    log_status_transition(
        supabase, inbox_id=inbox_id, from_status=previous_status,
        to_status=new_status, reason="promotion", source="auto_promote",
        detail=criteria,
    )

    return {
        "inbox_id": inbox_id,
        "promoted": True,
        "reason": "promoted",
        "criteria_status": criteria,
        "previous_status": previous_status,
        "new_status": new_status,
    }
