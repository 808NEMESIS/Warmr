"""
inbox_rotator.py

Selects the optimal sending inbox for a campaign email.

Selection criteria (in order of priority):
  1. Inbox must belong to the correct client and not be retired or paused.
  2. Inbox status must be 'ready', OR 'warmup' with reputation_score >= 70.
  3. Today's campaign sends for this inbox must be below daily_campaign_target.
  4. Prefer inboxes with the highest reputation_score.
  5. Among equal scores, prefer least loaded (fewest campaign sends today relative to target).

Campaign sends are counted from email_events (event_type='sent') for today UTC,
rather than from inboxes.daily_sent (which counts warmup + campaign combined).
This gives precise per-inbox campaign send counts without touching the warmup counter.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

logger = logging.getLogger(__name__)

# Minimum reputation to allow a 'warmup'-status inbox into campaign rotation
MIN_WARMUP_REPUTATION: float = 70.0

# ---------------------------------------------------------------------------
# Provider detection (recipient → preferred inbox provider)
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

    Used to prefer matching-provider sending inboxes for better deliverability.
    Google recipients → Google Workspace senders.
    Microsoft recipients → Microsoft 365 senders.

    Returns: 'google' | 'microsoft' | 'unknown'
    """
    if not email or "@" not in email:
        return "unknown"
    domain = email.split("@")[1].lower().strip()
    if domain in _GOOGLE_DOMAINS:
        return "google"
    if domain in _MICROSOFT_DOMAINS:
        return "microsoft"
    return "unknown"


# ---------------------------------------------------------------------------
# Campaign send counting
# ---------------------------------------------------------------------------

def count_campaign_sends_today(supabase: Client, inbox_id: str) -> int:
    """
    Count how many campaign emails this inbox has sent today (UTC).

    Reads from email_events rather than inboxes.daily_sent to isolate
    campaign sends from warmup sends.
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    resp = (
        supabase.table("email_events")
        .select("id", count="exact")
        .eq("inbox_id", inbox_id)
        .eq("event_type", "sent")
        .gte("timestamp", today_start)
        .execute()
    )
    return resp.count or 0


def count_campaign_sends_today_bulk(
    supabase: Client,
    inbox_ids: list[str],
) -> dict[str, int]:
    """
    Count today's campaign sends for multiple inboxes in a single query.

    Returns {inbox_id: send_count}. More efficient than calling
    count_campaign_sends_today() per inbox when rotating across many inboxes.
    """
    if not inbox_ids:
        return {}

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    resp = (
        supabase.table("email_events")
        .select("inbox_id")
        .in_("inbox_id", inbox_ids)
        .eq("event_type", "sent")
        .gte("timestamp", today_start)
        .execute()
    )

    counts: dict[str, int] = {iid: 0 for iid in inbox_ids}
    for row in (resp.data or []):
        iid = row.get("inbox_id")
        if iid in counts:
            counts[iid] += 1

    return counts


# ---------------------------------------------------------------------------
# Eligibility filtering
# ---------------------------------------------------------------------------

def get_eligible_inboxes(
    supabase: Client,
    client_id: str,
) -> list[dict]:
    """
    Return all inboxes for this client that are eligible for campaign sending.

    Eligibility:
      - status = 'ready'  (fully warmed up)
        OR status = 'warmup' AND reputation_score >= MIN_WARMUP_REPUTATION
      - warmup_active = true (warmup still running — keeps reputation climbing)
      - daily_campaign_target > 0  (operator has allocated campaign quota)
      - Not 'paused' or 'retired'
    """
    resp = (
        supabase.table("inboxes")
        .select("*")
        .eq("client_id", client_id)
        .neq("status", "retired")
        .neq("status", "paused")
        .gt("daily_campaign_target", 0)
        .execute()
    )
    inboxes = resp.data or []

    eligible: list[dict] = []
    for inbox in inboxes:
        status = inbox.get("status") or ""
        rep = inbox.get("reputation_score") or 0.0

        if status == "ready":
            eligible.append(inbox)
        elif status == "warmup" and rep >= MIN_WARMUP_REPUTATION:
            eligible.append(inbox)

    return eligible


# ---------------------------------------------------------------------------
# Main selection function
# ---------------------------------------------------------------------------

def select_inbox(
    supabase: Client,
    client_id: str,
    exclude_inbox_ids: Optional[list[str]] = None,
    recipient_email: Optional[str] = None,
) -> Optional[dict]:
    """
    Select the best available inbox for a campaign send.

    Provider-aware routing:
        If recipient_email is provided, the recipient's mail provider is detected
        (google / microsoft / unknown) and inboxes with a matching provider field
        are preferred. If no matching-provider inbox is available, falls back to
        standard highest-rep / least-loaded selection.

    Args:
        supabase:          Supabase client (service role).
        client_id:         The client whose inboxes to draw from.
        exclude_inbox_ids: Optional list of inbox IDs to skip this call.
        recipient_email:   Optional recipient address for provider-aware routing.

    Returns:
        The chosen inbox dict, or None if no eligible inbox has remaining capacity.
    """
    exclude = set(exclude_inbox_ids or [])
    inboxes = get_eligible_inboxes(supabase, client_id)

    if not inboxes:
        logger.warning("Client %s has no eligible inboxes for campaign sending.", client_id)
        return None

    # Filter out explicitly excluded inboxes
    inboxes = [i for i in inboxes if i["id"] not in exclude]
    if not inboxes:
        return None

    # Detect desired provider from recipient
    preferred_provider = detect_recipient_provider(recipient_email) if recipient_email else "unknown"

    # Bulk-fetch today's campaign send counts in one query
    inbox_ids = [i["id"] for i in inboxes]
    sends_today = count_campaign_sends_today_bulk(supabase, inbox_ids)

    # Build list of available inboxes with sort keys
    # Sort key tuple: (provider_mismatch: 0/1, neg_rep, load_ratio)
    # provider_mismatch=0 means this inbox's provider matches the recipient
    available: list[tuple[int, float, float, dict]] = []

    for inbox in inboxes:
        target: int = inbox.get("daily_campaign_target") or 0
        if target <= 0:
            continue
        sent_today: int = sends_today.get(inbox["id"], 0)
        if sent_today >= target:
            logger.debug(
                "Inbox %s at campaign capacity (%d/%d) — skipping.",
                inbox.get("email"), sent_today, target,
            )
            continue

        rep: float        = inbox.get("reputation_score") or 0.0
        load_ratio: float = sent_today / target  # lower = less loaded

        inbox_provider  = (inbox.get("provider") or "other").lower()
        provider_match  = (
            0 if (
                preferred_provider != "unknown"
                and inbox_provider == preferred_provider
            ) else 1
        )

        available.append((provider_match, -rep, load_ratio, inbox))

    if not available:
        logger.info("Client %s: all eligible inboxes have reached campaign capacity today.", client_id)
        return None

    available.sort(key=lambda t: (t[0], t[1], t[2]))
    chosen = available[0][3]
    provider_matched = available[0][0] == 0

    logger.info(
        "Selected inbox %s (rep=%.1f, capacity=%d/%d, provider_match=%s).",
        chosen.get("email"),
        chosen.get("reputation_score") or 0.0,
        sends_today.get(chosen["id"], 0),
        chosen.get("daily_campaign_target") or 0,
        provider_matched,
    )

    return chosen
