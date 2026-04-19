"""
webhook_dispatcher.py

Standalone worker that delivers webhook events to registered client endpoints.

Run continuously:
    python webhook_dispatcher.py

Or as a systemd service / Docker container alongside the main API.

Logic per cycle (runs every 60 seconds):
  1. Fetch pending events from webhook_events WHERE dispatched = false
  2. For each event, find all matching active webhooks (client_id + event subscribed)
  3. POST the event payload to each webhook URL with HMAC-SHA256 signature
  4. Log the result to webhook_logs
  5. Mark event as dispatched = true
  6. On failure: schedule a retry using exponential backoff (1m → 5m → 30m)
     After 3 failed attempts the event is abandoned (logged but not retried).

Retry schedule:
    Attempt 1 (immediate)
    Attempt 2 → next_retry_at = now + 1 min
    Attempt 3 → next_retry_at = now + 5 min
    Attempt 4 → next_retry_at = now + 30 min  (final — no more retries after this)

Signature header:
    X-Warmr-Signature: sha256=<hex(HMAC-SHA256(secret, body_bytes))>

Environment variables required:
    SUPABASE_URL, SUPABASE_KEY
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

POLL_INTERVAL_SECONDS: int   = 60
REQUEST_TIMEOUT_SECONDS: int = 10
MAX_ATTEMPTS: int            = 4   # attempt 1 (immediate) + 3 retries

# Backoff delays in seconds for attempts 2, 3, 4
RETRY_DELAYS: list[int] = [60, 300, 1800]  # 1m, 5m, 30m

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
    """Return a fresh Supabase client (service role — bypasses RLS)."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sign_payload(secret: str, body_bytes: bytes) -> str:
    """Return HMAC-SHA256 hex digest of body_bytes using secret."""
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _retry_at(attempt_count: int) -> Optional[str]:
    """
    Return the ISO timestamp for the next retry, or None if max attempts reached.

    attempt_count is the count AFTER the current attempt has been recorded.
    """
    if attempt_count >= MAX_ATTEMPTS:
        return None  # no more retries
    delay = RETRY_DELAYS[attempt_count - 1]
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


# ---------------------------------------------------------------------------
# Core delivery
# ---------------------------------------------------------------------------

def deliver(
    webhook: dict,
    event_type: str,
    payload: dict,
    attempt_count: int,
) -> tuple[bool, int, str]:
    """
    POST payload to webhook URL, return (success, http_status, response_body_snippet).

    Does not raise — all errors are caught and returned as failure.
    """
    # Nonce + timestamp for replay protection; receivers should reject duplicate nonces
    # and requests with timestamps older than 5 minutes.
    import secrets
    nonce = secrets.token_urlsafe(16)
    timestamp = _now_utc()

    body_bytes = json.dumps({
        "event":      event_type,
        "payload":    payload,
        "webhook_id": webhook["id"],
        "sent_at":    timestamp,
        "nonce":      nonce,
    }, ensure_ascii=False).encode("utf-8")

    signature = _sign_payload(webhook["secret"], body_bytes)

    headers = {
        "Content-Type":       "application/json",
        "X-Warmr-Signature":  f"sha256={signature}",
        "X-Warmr-Event":      event_type,
        "X-Warmr-Attempt":    str(attempt_count),
        "X-Warmr-Timestamp":  timestamp,
        "X-Warmr-Nonce":      nonce,
        "User-Agent":         "Warmr-Webhooks/1.0",
    }

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.post(webhook["url"], content=body_bytes, headers=headers)
        status_code  = resp.status_code
        body_snippet = resp.text[:500]
        success      = 200 <= status_code < 300
        return success, status_code, body_snippet
    except Exception as exc:
        logger.warning("HTTP error delivering to %s: %s", webhook["url"], exc)
        return False, 0, str(exc)[:500]


# ── Circuit breaker ────────────────────────────────────────────────────────
CIRCUIT_BREAKER_THRESHOLD = 10  # consecutive failures before auto-disable
CIRCUIT_BREAKER_WINDOW_MINUTES = 60


def _count_recent_failures(sb: Client, webhook_id: str) -> int:
    """Count consecutive webhook_logs failures for this webhook within the window."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(minutes=CIRCUIT_BREAKER_WINDOW_MINUTES)).isoformat()
    try:
        resp = (
            sb.table("webhook_logs")
            .select("success, timestamp")
            .eq("webhook_id", webhook_id)
            .gte("timestamp", since)
            .order("timestamp", desc=True)
            .limit(CIRCUIT_BREAKER_THRESHOLD)
            .execute()
        )
        rows = resp.data or []
        # Count leading consecutive failures (most recent first)
        count = 0
        for row in rows:
            if row.get("success"):
                break
            count += 1
        return count
    except Exception:
        return 0


def _trip_circuit_breaker(sb: Client, webhook: dict) -> None:
    """Disable the webhook and create an urgent notification for the client."""
    try:
        sb.table("webhooks").update({
            "active": False,
            "disabled_reason": "circuit_breaker",
            "disabled_at": _now_utc(),
        }).eq("id", webhook["id"]).execute()
    except Exception as exc:
        logger.error("Failed to disable webhook %s: %s", webhook["id"], exc)

    try:
        sb.table("notifications").insert({
            "client_id": webhook.get("client_id", ""),
            "type": "webhook_auto_disabled",
            "entity_id": webhook["id"],
            "entity_type": "webhook",
            "message": (
                f"Webhook {webhook.get('url', '')[:60]} is automatisch uitgeschakeld na "
                f"{CIRCUIT_BREAKER_THRESHOLD} opeenvolgende mislukkingen. "
                "Controleer je endpoint en herstart handmatig."
            ),
            "priority": "urgent",
        }).execute()
    except Exception:
        pass

    logger.warning(
        "Circuit breaker TRIPPED for webhook %s (%s) — auto-disabled after %d consecutive failures",
        webhook["id"], webhook.get("url", "")[:60], CIRCUIT_BREAKER_THRESHOLD,
    )


def _log_attempt(
    sb: Client,
    webhook: dict,
    event: dict,
    success: bool,
    status_code: int,
    body_snippet: str,
    attempt_count: int,
    next_retry_at: Optional[str],
) -> None:
    """Write a row to webhook_logs."""
    try:
        sb.table("webhook_logs").insert({
            "webhook_id":      webhook["id"],
            "client_id":       event["client_id"],
            "event_type":      event["event_type"],
            "payload":         event["payload"],
            "response_status": status_code or None,
            "response_body":   body_snippet,
            "attempt_count":   attempt_count,
            "next_retry_at":   next_retry_at,
            "success":         success,
            "timestamp":       _now_utc(),
        }).execute()
    except Exception as exc:
        logger.error("Failed to write webhook_log: %s", exc)


def process_event(sb: Client, event: dict) -> None:
    """
    Deliver one webhook_event to all matching active webhooks.

    After all deliveries (success or final failure), marks event as dispatched.
    """
    client_id  = event["client_id"]
    event_type = event["event_type"]
    payload    = event["payload"] or {}

    # Find all active webhooks for this client that subscribe to this event type
    try:
        resp = (
            sb.table("webhooks")
            .select("id, url, secret, events")
            .eq("client_id", client_id)
            .eq("active", True)
            .execute()
        )
        webhooks = [w for w in (resp.data or []) if event_type in (w.get("events") or [])]
    except Exception as exc:
        logger.error("Failed to fetch webhooks for event %s: %s", event["id"], exc)
        return

    if not webhooks:
        # No matching webhooks — mark as dispatched immediately
        _mark_dispatched(sb, event["id"])
        return

    for webhook in webhooks:
        # Include client_id in the webhook dict so the circuit breaker notification
        # has the right tenant. It's not always selected above.
        webhook = {**webhook, "client_id": client_id}

        attempt_count = 1
        success, status_code, body_snippet = deliver(webhook, event_type, payload, attempt_count)
        next_retry = _retry_at(attempt_count) if not success else None

        _log_attempt(sb, webhook, event, success, status_code, body_snippet, attempt_count, next_retry)

        # Circuit breaker: after each delivery, check consecutive failures
        if not success:
            fails = _count_recent_failures(sb, webhook["id"])
            if fails >= CIRCUIT_BREAKER_THRESHOLD:
                _trip_circuit_breaker(sb, webhook)

        if success:
            logger.info(
                "Delivered event %s (%s) to webhook %s → %d",
                event["id"], event_type, webhook["id"], status_code,
            )
        else:
            logger.warning(
                "Failed to deliver event %s (%s) to webhook %s → %s (attempt %d/%d)",
                event["id"], event_type, webhook["id"], status_code or "no-response",
                attempt_count, MAX_ATTEMPTS,
            )

    _mark_dispatched(sb, event["id"])


def _mark_dispatched(sb: Client, event_id: str) -> None:
    """Set dispatched = true on a webhook_event row."""
    try:
        sb.table("webhook_events").update({"dispatched": True}).eq("id", event_id).execute()
    except Exception as exc:
        logger.error("Failed to mark event %s as dispatched: %s", event_id, exc)


def process_retries(sb: Client) -> None:
    """
    Re-attempt previously failed deliveries whose next_retry_at is now in the past.

    Reads webhook_logs where success = false AND next_retry_at <= now,
    then re-sends and updates (or inserts a new log row).
    """
    now_iso = _now_utc()

    try:
        resp = (
            sb.table("webhook_logs")
            .select("id, webhook_id, client_id, event_type, payload, attempt_count, next_retry_at")
            .eq("success", False)
            .lte("next_retry_at", now_iso)
            .order("next_retry_at", desc=False)
            .limit(100)
            .execute()
        )
        pending_retries = resp.data or []
    except Exception as exc:
        logger.error("Failed to fetch retry queue: %s", exc)
        return

    if not pending_retries:
        return

    logger.info("Processing %d retry(ies)...", len(pending_retries))

    for log_row in pending_retries:
        webhook_id    = log_row["webhook_id"]
        attempt_count = log_row["attempt_count"] + 1

        # Fetch the webhook (may have been deleted or deactivated)
        try:
            wh_resp = (
                sb.table("webhooks")
                .select("id, url, secret, active")
                .eq("id", webhook_id)
                .limit(1)
                .execute()
            )
            wh_rows = wh_resp.data or []
        except Exception as exc:
            logger.error("Failed to fetch webhook %s for retry: %s", webhook_id, exc)
            continue

        if not wh_rows or not wh_rows[0].get("active"):
            # Webhook was deleted or disabled — clear the retry
            _clear_retry(sb, log_row["id"])
            continue

        webhook = wh_rows[0]
        event_type    = log_row["event_type"]
        payload       = log_row["payload"] or {}

        success, status_code, body_snippet = deliver(webhook, event_type, payload, attempt_count)
        next_retry = _retry_at(attempt_count) if not success else None

        # Update the existing log row
        try:
            sb.table("webhook_logs").update({
                "response_status": status_code or None,
                "response_body":   body_snippet,
                "attempt_count":   attempt_count,
                "next_retry_at":   next_retry,
                "success":         success,
                "timestamp":       _now_utc(),
            }).eq("id", log_row["id"]).execute()
        except Exception as exc:
            logger.error("Failed to update webhook_log %s: %s", log_row["id"], exc)

        if success:
            logger.info(
                "Retry succeeded: webhook %s, event %s (attempt %d)",
                webhook_id, event_type, attempt_count,
            )
        elif next_retry is None:
            logger.warning(
                "Giving up on webhook %s, event %s after %d attempts",
                webhook_id, event_type, attempt_count,
            )
        else:
            logger.info(
                "Retry %d/%d failed for webhook %s — next retry at %s",
                attempt_count, MAX_ATTEMPTS, webhook_id, next_retry,
            )


def _clear_retry(sb: Client, log_id: str) -> None:
    """Clear next_retry_at on a log row (webhook no longer active)."""
    try:
        sb.table("webhook_logs").update({"next_retry_at": None}).eq("id", log_id).execute()
    except Exception as exc:
        logger.error("Failed to clear retry for log %s: %s", log_id, exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle(sb: Client) -> None:
    """One poll cycle: process new events + due retries."""
    # 1. Process retries first (older work takes priority)
    process_retries(sb)

    # 2. Fetch new undispatched events
    try:
        resp = (
            sb.table("webhook_events")
            .select("id, client_id, event_type, payload")
            .eq("dispatched", False)
            .order("created_at", desc=False)
            .limit(200)
            .execute()
        )
        events = resp.data or []
    except Exception as exc:
        logger.error("Failed to fetch webhook_events: %s", exc)
        return

    if events:
        logger.info("Dispatching %d new event(s)...", len(events))
        for event in events:
            process_event(sb, event)


def main() -> None:
    """Entry point — runs the dispatch loop indefinitely."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Exiting.")
        raise SystemExit(1)

    logger.info("Webhook dispatcher started. Poll interval: %ds", POLL_INTERVAL_SECONDS)

    while True:
        try:
            sb = _sb()
            run_cycle(sb)
        except Exception as exc:
            logger.error("Unhandled error in dispatch cycle: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
