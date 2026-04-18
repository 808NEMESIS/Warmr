"""
enrichment_queue.py

Asynchronous enrichment worker.

Polls the enrichment_queue table for pending jobs and processes them
with a maximum of 10 concurrent enrichments (respecting external API rate limits).

Priority ordering:
    1. Leads linked to an active campaign  (priority = 1)
    2. All other leads                     (priority = 5, default)

Leads receive a lower (= higher priority) number when they are queued via
POST /api/v1/leads with a campaign_id attached.

Retry logic:
    - Max 3 attempts per lead
    - On failure: log error_message, status = 'failed' (no automatic retry —
      queue the lead again from the dashboard or API if needed)

Webhook integration:
    After a successful enrichment, a `lead.enriched` event is emitted
    to webhook_events so the LeadGen tool (or any registered webhook) is notified.

Run:
    python enrichment_queue.py

Or alongside the main API:
    uvicorn api.main:app & python enrichment_queue.py

Environment variables:
    SUPABASE_URL, SUPABASE_KEY  — required
    (enrichment_engine.py reads its own env vars)
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

from enrichment_engine import EnrichmentResult, enrich_lead

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

POLL_INTERVAL_SECONDS: int = 15   # check for new jobs every 15s
MAX_CONCURRENT:        int = 10   # max parallel enrichments (API rate limit buffer)
MAX_ATTEMPTS:          int = 3    # give up after 3 failures

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sb() -> Client:
    """Return a fresh Supabase client (service role)."""
    return create_client(_SUPABASE_URL, _SUPABASE_KEY)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_enriched_event(sb: Client, lead_id: str, client_id: str, result: EnrichmentResult) -> None:
    """
    Write a lead.enriched event to webhook_events.
    The webhook_dispatcher will pick this up and deliver to registered webhooks.
    """
    try:
        payload = {
            "lead_id":             lead_id,
            "email":               result.email,
            "verified":            result.verified,
            "enriched":            True,
            "personalized_opener": result.personalized_opener,
            "enrichment_errors":   result.errors,
        }
        sb.table("webhook_events").insert({
            "client_id":  client_id,
            "event_type": "lead.enriched",
            "payload":    payload,
            "dispatched": False,
            "created_at": _now_utc(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to emit lead.enriched event for %s: %s", lead_id, exc)


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

def _claim_job(sb: Client, job: dict) -> bool:
    """
    Atomically mark a job as 'processing'.
    Returns True if the claim succeeded (no concurrent worker grabbed it first).
    """
    try:
        resp = (
            sb.table("enrichment_queue")
            .update({
                "status":     "processing",
                "started_at": _now_utc(),
                "attempts":   job["attempts"] + 1,
            })
            .eq("id", job["id"])
            .eq("status", "pending")  # only claim if still pending
            .execute()
        )
        return bool(resp.data)
    except Exception as exc:
        logger.error("Failed to claim job %s: %s", job["id"], exc)
        return False


def _complete_job(sb: Client, job_id: str) -> None:
    """Mark a job as completed."""
    try:
        sb.table("enrichment_queue").update({
            "status":       "completed",
            "completed_at": _now_utc(),
            "error_message": None,
        }).eq("id", job_id).execute()
    except Exception as exc:
        logger.error("Failed to mark job %s completed: %s", job_id, exc)


def _fail_job(sb: Client, job: dict, error: str) -> None:
    """
    Increment attempt count on a job.
    If max attempts reached: status = 'failed'.
    Otherwise: status = 'pending' (will be retried next cycle).
    """
    attempts = job["attempts"]   # already incremented in _claim_job
    new_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"

    try:
        sb.table("enrichment_queue").update({
            "status":        new_status,
            "error_message": error[:500],
        }).eq("id", job["id"]).execute()
    except Exception as exc:
        logger.error("Failed to update failed job %s: %s", job["id"], exc)

    if new_status == "failed":
        logger.warning("Job %s permanently failed after %d attempts: %s", job["id"], attempts, error)
    else:
        logger.info("Job %s will be retried (attempt %d/%d)", job["id"], attempts, MAX_ATTEMPTS)


# ---------------------------------------------------------------------------
# Worker function (runs in thread pool)
# ---------------------------------------------------------------------------

def _process_job(job: dict) -> tuple[str, Optional[str]]:
    """
    Process one enrichment job in a worker thread.

    Returns (job_id, error_message | None).
    """
    job_id    = job["id"]
    lead_id   = job["lead_id"]
    client_id = job["client_id"]

    try:
        logger.info("Processing enrichment job %s for lead %s", job_id, lead_id)
        result = enrich_lead(lead_id, client_id)

        # Emit webhook event
        sb = _sb()
        _emit_enriched_event(sb, lead_id, client_id, result)
        _complete_job(sb, job_id)

        logger.info(
            "Job %s done — lead %s verified=%s opener=%s errors=%d",
            job_id, lead_id, result.verified,
            bool(result.personalized_opener), len(result.errors),
        )
        return job_id, None

    except Exception as exc:
        error_str = str(exc)
        logger.error("Job %s failed: %s", job_id, error_str)
        return job_id, error_str


# ---------------------------------------------------------------------------
# Priority assignment helper
# ---------------------------------------------------------------------------

def _calculate_priority(sb: Client, lead_id: str) -> int:
    """
    Return priority 1 if lead belongs to an active campaign, else 5.
    Used when queueing leads from outside the normal API flow.
    """
    try:
        resp = (
            sb.table("campaign_leads")
            .select("campaign_id, campaigns(status)")
            .eq("lead_id", lead_id)
            .execute()
        )
        for row in (resp.data or []):
            campaign = row.get("campaigns") or {}
            if campaign.get("status") == "active":
                return 1
    except Exception:
        pass
    return 5


# ---------------------------------------------------------------------------
# Queue management — called by public_api.py
# ---------------------------------------------------------------------------

def enqueue_lead(lead_id: str, client_id: str, priority: Optional[int] = None) -> bool:
    """
    Add a lead to the enrichment queue.

    Skips silently if the lead is already pending or processing
    (due to the unique index on enrichment_queue).

    priority:
        None  → calculated automatically (1 if active campaign, else 5)
        int   → use this value directly

    Returns True if enqueued, False if already in queue or on error.
    """
    sb = _sb()

    if priority is None:
        priority = _calculate_priority(sb, lead_id)

    try:
        sb.table("enrichment_queue").insert({
            "lead_id":   lead_id,
            "client_id": client_id,
            "status":    "pending",
            "priority":  priority,
            "attempts":  0,
            "queued_at": _now_utc(),
        }).execute()
        logger.info("Queued lead %s for enrichment (priority %d)", lead_id, priority)
        return True
    except Exception as exc:
        # Unique index violation means lead is already queued — not an error
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            logger.debug("Lead %s already in enrichment queue", lead_id)
            return False
        logger.error("Failed to enqueue lead %s: %s", lead_id, exc)
        return False


def enqueue_leads_bulk(lead_ids: list[str], client_id: str, priority: int = 5) -> int:
    """
    Enqueue multiple leads at once. Returns the count successfully enqueued.
    Uses batch insert; silently ignores duplicates per row.
    """
    if not lead_ids:
        return 0

    sb = _sb()
    now = _now_utc()
    rows = [
        {
            "lead_id":   lid,
            "client_id": client_id,
            "status":    "pending",
            "priority":  priority,
            "attempts":  0,
            "queued_at": now,
        }
        for lid in lead_ids
    ]

    enqueued = 0
    # Insert one at a time to handle individual duplicate conflicts gracefully
    for row in rows:
        try:
            sb.table("enrichment_queue").insert(row).execute()
            enqueued += 1
        except Exception:
            pass  # already queued or error — skip

    logger.info("Bulk enqueued %d/%d leads for client %s", enqueued, len(lead_ids), client_id)
    return enqueued


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def fetch_pending_jobs(sb: Client, limit: int) -> list[dict]:
    """
    Fetch the highest-priority pending jobs, ordered by priority ASC then queued_at ASC.

    Excludes jobs that have exceeded MAX_ATTEMPTS.
    """
    try:
        resp = (
            sb.table("enrichment_queue")
            .select("id, lead_id, client_id, attempts, priority, queued_at")
            .eq("status", "pending")
            .lt("attempts", MAX_ATTEMPTS)
            .order("priority", desc=False)
            .order("queued_at", desc=False)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("Failed to fetch pending enrichment jobs: %s", exc)
        return []


def run_cycle(sb: Client) -> int:
    """
    Process one batch of pending jobs.
    Returns the number of jobs processed.
    """
    jobs = fetch_pending_jobs(sb, limit=MAX_CONCURRENT)
    if not jobs:
        return 0

    logger.info("Found %d enrichment job(s) to process", len(jobs))

    # Claim jobs atomically before dispatching to threads
    claimed: list[dict] = []
    for job in jobs:
        if _claim_job(sb, job):
            # Update local copy with incremented attempts
            job["attempts"] += 1
            claimed.append(job)

    if not claimed:
        return 0

    logger.info("Claimed %d job(s), starting enrichment...", len(claimed))

    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = {executor.submit(_process_job, job): job for job in claimed}
        for future in as_completed(futures):
            job = futures[future]
            try:
                job_id, error = future.result()
                if error:
                    _fail_job(_sb(), job, error)
                else:
                    completed += 1
            except Exception as exc:
                logger.error("Unexpected error processing job %s: %s", job["id"], exc)
                _fail_job(_sb(), job, str(exc))

    logger.info("Cycle complete — %d/%d jobs succeeded", completed, len(claimed))
    return len(claimed)


def main() -> None:
    """Entry point — run the enrichment worker loop indefinitely."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Exiting.")
        raise SystemExit(1)

    logger.info(
        "Enrichment queue worker started. Poll interval: %ds, max concurrent: %d",
        POLL_INTERVAL_SECONDS, MAX_CONCURRENT,
    )

    while True:
        try:
            sb = _sb()
            run_cycle(sb)
        except Exception as exc:
            logger.error("Unhandled error in enrichment cycle: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
