"""
utils/suppression.py — single, reusable implementation of "suppress this lead
and cancel all pending sends" (Fase 2, item 1).

Previously this GDPR-critical operation was implemented once, inline, inside
api/main.py's process_unsubscribe (the link-click flow) — the reply-based
unsubscribe path in imap_processor.py only updated leads.status and never
touched suppression_list or campaign_leads, so a prospect who replied "STOP"
kept receiving scheduled campaign emails. This module extracts the working
implementation so both paths call the exact same code.

Each step is independent and best-effort (mirrors the original): a failure in
one step (e.g. already suppressed) must not prevent the others from running.
"""
from __future__ import annotations

import logging
from typing import Optional

from utils.state_machines_registry import CAMPAIGN_LEAD_STATUS

logger = logging.getLogger(__name__)


def suppress_and_cancel(
    supabase,
    client_id: str,
    lead_id: str,
    lead_email: str,
    source: str = "unsubscribe",
) -> None:
    """Add lead_email to the client's suppression list and cancel all its
    pending/active campaign sends. Never raises."""
    domain = lead_email.split("@")[-1] if lead_email and "@" in lead_email else None

    try:
        supabase.table("suppression_list").insert({
            "client_id": client_id,
            "email": lead_email,
            "domain": domain,
            "reason": "unsubscribe",
            "source": source,
        }).execute()
    except Exception:
        pass  # already suppressed

    try:
        for _from in ("active", "pending"):
            CAMPAIGN_LEAD_STATUS.check_log_only(_from, "unsubscribed", logger)
        supabase.table("campaign_leads").update({
            "status": "unsubscribed",
        }).eq("lead_id", lead_id).in_("status", ["active", "pending"]).execute()
    except Exception as exc:
        logger.debug("campaign_leads cancel failed for lead %s: %s", lead_id, exc)

    try:
        supabase.table("leads").update({"status": "unsubscribed"}).eq("id", lead_id).execute()
    except Exception as exc:
        logger.debug("leads status update failed for lead %s: %s", lead_id, exc)
