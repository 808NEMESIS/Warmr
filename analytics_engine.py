"""
analytics_engine.py

Daily analytics aggregation engine for Warmr.

Reads raw events from email_events, warmup_logs, reply_inbox, and inboxes,
computes per-day metric snapshots, and upserts them into analytics_cache.

Called by n8n once per day (e.g. 01:00 UTC after midnight daily_reset).
Can also be triggered on-demand from POST /analytics/recompute (admin use).

Metric schemas stored in analytics_cache.metrics JSONB:

  entity_type = 'campaign':
    emails_sent, unique_opens, open_rate, clicks, click_rate,
    replies, reply_rate, interested_count, meeting_rate,
    bounces, bounce_rate, unsubscribes, unsubscribe_rate

  entity_type = 'inbox':
    warmup_sent, campaign_sent, reputation_score,
    spam_rescues, spam_complaints, daily_sent

  entity_type = 'sequence_step':
    step_number, ab_variant, emails_sent, replies,
    reply_rate, bounces, bounce_rate
    (A/B significance is computed live by find_winning_variant(), not cached)
"""

import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

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

# How many days of history to (re)compute on each run.
# Running for 1 day is fast; increase for backfills.
DEFAULT_LOOKBACK_DAYS: int = 1


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _date_range(start: date, end: date) -> list[date]:
    """Return every date from start up to and including end."""
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _day_bounds_utc(d: date) -> tuple[str, str]:
    """Return (start_iso, end_iso) for a full UTC day as ISO 8601 strings."""
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Supabase upsert helper
# ---------------------------------------------------------------------------

def _upsert_cache(
    supabase: Client,
    client_id: str,
    entity_type: str,
    entity_id: str,
    d: date,
    metrics: dict,
) -> None:
    """
    Upsert one row into analytics_cache.

    Uses the UNIQUE constraint on (entity_id, entity_type, date) to update
    an existing row rather than duplicating it.
    """
    try:
        supabase.table("analytics_cache").upsert(
            {
                "client_id": client_id,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "date": d.isoformat(),
                "metrics": metrics,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="entity_id,entity_type,date",
        ).execute()
    except Exception as exc:
        logger.error(
            "Failed to upsert analytics_cache (%s / %s / %s): %s",
            entity_type, entity_id, d, exc,
        )


# ---------------------------------------------------------------------------
# Campaign analytics
# ---------------------------------------------------------------------------

def _count_events(events: list[dict], event_type: str) -> int:
    """Count events matching a specific event_type."""
    return sum(1 for e in events if e.get("event_type") == event_type)


def _unique_opens(events: list[dict]) -> int:
    """
    Count unique lead_ids that have at least one 'opened' event.

    Prevents inflating open_rate when the same lead opens multiple times.
    """
    return len({e["lead_id"] for e in events if e.get("event_type") == "opened" and e.get("lead_id")})


def compute_campaign_metrics_for_day(
    supabase: Client,
    campaign_id: str,
    client_id: str,
    d: date,
) -> None:
    """
    Aggregate all email_events for one campaign on one UTC calendar day
    and upsert into analytics_cache.
    """
    start_iso, end_iso = _day_bounds_utc(d)

    resp = (
        supabase.table("email_events")
        .select("event_type, lead_id, ab_variant")
        .eq("campaign_id", campaign_id)
        .gte("timestamp", start_iso)
        .lte("timestamp", end_iso)
        .execute()
    )
    events: list[dict] = resp.data or []

    # reply_inbox is the source of truth for classification data
    reply_resp = (
        supabase.table("reply_inbox")
        .select("classification")
        .eq("campaign_id", campaign_id)
        .gte("received_at", start_iso)
        .lte("received_at", end_iso)
        .execute()
    )
    reply_rows: list[dict] = reply_resp.data or []

    emails_sent = _count_events(events, "sent")
    unique_op = _unique_opens(events)
    clicks = _count_events(events, "clicked")
    replies = _count_events(events, "replied")
    bounces = _count_events(events, "bounced")
    unsubscribes = _count_events(events, "unsubscribed")
    interested_count = sum(
        1 for r in reply_rows if r.get("classification") == "interested"
    )

    def _rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator > 0 else 0.0

    metrics: dict[str, Any] = {
        "emails_sent": emails_sent,
        "unique_opens": unique_op,
        "open_rate": _rate(unique_op, emails_sent),
        "clicks": clicks,
        "click_rate": _rate(clicks, emails_sent),
        "replies": replies,
        "reply_rate": _rate(replies, emails_sent),
        "interested_count": interested_count,
        "meeting_rate": _rate(interested_count, emails_sent),
        "bounces": bounces,
        "bounce_rate": _rate(bounces, emails_sent),
        "unsubscribes": unsubscribes,
        "unsubscribe_rate": _rate(unsubscribes, emails_sent),
    }

    _upsert_cache(supabase, client_id, "campaign", campaign_id, d, metrics)


# ---------------------------------------------------------------------------
# Inbox analytics
# ---------------------------------------------------------------------------

def compute_inbox_metrics_for_day(
    supabase: Client,
    inbox: dict,
    client_id: str,
    d: date,
) -> None:
    """
    Aggregate warmup_logs and email_events for one inbox on one UTC day
    and upsert into analytics_cache.
    """
    inbox_id: str = inbox["id"]
    start_iso, end_iso = _day_bounds_utc(d)

    # Warmup sends from warmup_logs
    wl_resp = (
        supabase.table("warmup_logs")
        .select("action, reputation_score_at_time")
        .eq("inbox_id", inbox_id)
        .gte("timestamp", start_iso)
        .lte("timestamp", end_iso)
        .execute()
    )
    wl_rows: list[dict] = wl_resp.data or []

    warmup_sent = sum(1 for r in wl_rows if r.get("action") == "sent")
    spam_rescues = sum(1 for r in wl_rows if r.get("action") == "spam_rescued")

    # Latest reputation snapshot for this day (last log entry's score)
    rep_scores = [
        r["reputation_score_at_time"]
        for r in wl_rows
        if r.get("reputation_score_at_time") is not None
    ]
    reputation_snapshot = rep_scores[-1] if rep_scores else (inbox.get("reputation_score") or 50.0)

    # Campaign sends from email_events
    ee_resp = (
        supabase.table("email_events")
        .select("event_type")
        .eq("inbox_id", inbox_id)
        .eq("event_type", "sent")
        .gte("timestamp", start_iso)
        .lte("timestamp", end_iso)
        .execute()
    )
    campaign_sent = len(ee_resp.data or [])

    # Spam complaints from bounce_log
    bl_resp = (
        supabase.table("bounce_log")
        .select("id")
        .eq("inbox_id", inbox_id)
        .eq("bounce_type", "spam_complaint")
        .gte("timestamp", start_iso)
        .lte("timestamp", end_iso)
        .execute()
    )
    spam_complaints = len(bl_resp.data or [])

    metrics: dict[str, Any] = {
        "warmup_sent": warmup_sent,
        "campaign_sent": campaign_sent,
        "daily_sent": warmup_sent + campaign_sent,
        "reputation_score": round(reputation_snapshot, 2),
        "spam_rescues": spam_rescues,
        "spam_complaints": spam_complaints,
    }

    _upsert_cache(supabase, client_id, "inbox", inbox_id, d, metrics)


# ---------------------------------------------------------------------------
# Sequence step analytics
# ---------------------------------------------------------------------------

def compute_step_metrics(
    supabase: Client,
    campaign_id: str,
    client_id: str,
    d: date,
) -> None:
    """
    Aggregate email_events per sequence_step for one campaign on one UTC day.

    Creates one analytics_cache row per sequence_step (entity_type='sequence_step').
    """
    start_iso, end_iso = _day_bounds_utc(d)

    # Fetch all events for this campaign today with step and variant info
    ee_resp = (
        supabase.table("email_events")
        .select("event_type, sequence_step_id, ab_variant")
        .eq("campaign_id", campaign_id)
        .gte("timestamp", start_iso)
        .lte("timestamp", end_iso)
        .execute()
    )
    events: list[dict] = ee_resp.data or []

    if not events:
        return

    # Group by (sequence_step_id, ab_variant)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for event in events:
        key = (event.get("sequence_step_id") or "", event.get("ab_variant"))
        groups[key].append(event)

    # Fetch step metadata to enrich the cached metrics
    step_ids = list({k[0] for k in groups if k[0]})
    steps_meta: dict[str, dict] = {}
    if step_ids:
        sr = (
            supabase.table("sequence_steps")
            .select("id, step_number, ab_variant")
            .in_("id", step_ids)
            .execute()
        )
        for row in (sr.data or []):
            steps_meta[row["id"]] = row

    def _rate(n: int, d_: int) -> float:
        return round(n / d_, 4) if d_ > 0 else 0.0

    for (step_id, ab_variant), step_events in groups.items():
        if not step_id:
            continue

        sent = _count_events(step_events, "sent")
        replies = _count_events(step_events, "replied")
        bounces = _count_events(step_events, "bounced")
        meta = steps_meta.get(step_id, {})

        metrics: dict[str, Any] = {
            "step_number": meta.get("step_number"),
            "ab_variant": ab_variant,
            "emails_sent": sent,
            "replies": replies,
            "reply_rate": _rate(replies, sent),
            "bounces": bounces,
            "bounce_rate": _rate(bounces, sent),
        }

        _upsert_cache(supabase, client_id, "sequence_step", step_id, d, metrics)


# ---------------------------------------------------------------------------
# Top-level per-client aggregation
# ---------------------------------------------------------------------------

def aggregate_client(
    supabase: Client,
    client_id: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> None:
    """
    Run all aggregations for one client across the lookback window.

    Processes campaigns, inboxes, and sequence steps independently.
    Errors in one entity never abort the others.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days - 1)
    days = _date_range(start_date, end_date)

    logger.info(
        "Client %s: aggregating %d day(s) (%s → %s).",
        client_id, len(days), start_date, end_date,
    )

    # ── Campaigns ─────────────────────────────────────────────────────────
    campaigns_resp = (
        supabase.table("campaigns")
        .select("id")
        .eq("client_id", client_id)
        .execute()
    )
    campaigns: list[dict] = campaigns_resp.data or []

    for campaign in campaigns:
        cid = campaign["id"]
        for d in days:
            try:
                compute_campaign_metrics_for_day(supabase, cid, client_id, d)
                compute_step_metrics(supabase, cid, client_id, d)
            except Exception as exc:
                logger.error("Campaign %s / %s aggregation failed: %s", cid, d, exc)

    logger.info("Client %s: aggregated %d campaign(s).", client_id, len(campaigns))

    # ── Inboxes ───────────────────────────────────────────────────────────
    inboxes_resp = (
        supabase.table("inboxes")
        .select("*")
        .eq("client_id", client_id)
        .execute()
    )
    inboxes: list[dict] = inboxes_resp.data or []

    for inbox in inboxes:
        for d in days:
            try:
                compute_inbox_metrics_for_day(supabase, inbox, client_id, d)
            except Exception as exc:
                logger.error("Inbox %s / %s aggregation failed: %s", inbox.get("email"), d, exc)

    logger.info("Client %s: aggregated %d inbox(es).", client_id, len(inboxes))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> None:
    """
    Main entry point: run aggregations for every client in the database.

    Errors for one client do not abort processing of others.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Aborting.")
        return

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Fetch all distinct client IDs from the clients table
    clients_resp = supabase.table("clients").select("id").execute()
    clients: list[dict] = clients_resp.data or []

    if not clients:
        logger.info("No clients found — nothing to aggregate.")
        return

    logger.info("Running analytics aggregation for %d client(s).", len(clients))

    for client in clients:
        client_id = client["id"]
        try:
            aggregate_client(supabase, client_id, lookback_days=lookback_days)
        except Exception as exc:
            logger.error("Aggregation failed for client %s: %s", client_id, exc)

    logger.info("Analytics aggregation complete.")


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOOKBACK_DAYS
    main(lookback_days=days)
