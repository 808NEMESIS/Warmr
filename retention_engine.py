"""
retention_engine.py — GDPR data retention enforcement (Fase 2, Track 2, item 8).

Two responsibilities:
  1. purge_aged_events: delete event/tracking rows older than each client's
     retention_days (or the global default) while the account stays active.
  2. purge_closed_accounts: hard-delete an entire client account once
     closed_at is older than the grace period (default 30 days) — CLAUDE.md's
     GDPR section: "Delete all data for opted-out contacts within 30 days."

Mirrors reap_stranded_sends.py: job_lock guarded, pure functions separated
from _run() for testability, never raises unhandled.
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

DEFAULT_RETENTION_DAYS = int(os.getenv("WARMR_DEFAULT_RETENTION_DAYS", "365"))
CLOSED_ACCOUNT_GRACE_DAYS = int(os.getenv("WARMR_CLOSED_ACCOUNT_GRACE_DAYS", "30"))


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL/KEY not set.")
        return 1

    from utils.job_lock import job_lock
    with job_lock("retention_engine") as acquired:
        if not acquired:
            logger.info("retention_engine already running elsewhere — skipping.")
            return 0
        return _run()


def _resolve_retention_days(sb, client_id: str, default_days: int) -> int:
    """This client's retention_days override, or the global default."""
    try:
        resp = sb.table("client_settings").select("retention_days").eq("client_id", client_id).limit(1).execute()
        rows = resp.data or []
        days = rows[0].get("retention_days") if rows else None
        return int(days) if days else default_days
    except Exception:
        return default_days


def purge_aged_events(sb, default_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """
    Delete event/tracking rows older than each client's retention window.
    Returns {client_id: {table: deleted_count_or_error}}. Pure DB operation,
    kept separate from _run() so it's directly testable against a fake.
    """
    results: dict = {}
    clients_resp = sb.table("clients").select("id").execute()
    for client in (clients_resp.data or []):
        client_id = client["id"]
        days = _resolve_retention_days(sb, client_id, default_days)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted: dict = {}

        # Direct client_id columns
        for table, ts_col in [("reply_inbox", "received_at"), ("email_tracking", "created_at")]:
            try:
                r = sb.table(table).delete().eq("client_id", client_id).lt(ts_col, cutoff).execute()
                deleted[table] = len(r.data or [])
            except Exception as exc:
                deleted[table] = f"error: {exc}"

        # inbox_id-scoped tables — no client_id column, only inbox_id
        inbox_ids = [
            row["id"]
            for row in (sb.table("inboxes").select("id").eq("client_id", client_id).execute().data or [])
        ]
        for table in ("warmup_logs", "bounce_log"):
            try:
                if inbox_ids:
                    r = sb.table(table).delete().in_("inbox_id", inbox_ids).lt("timestamp", cutoff).execute()
                    deleted[table] = len(r.data or [])
                else:
                    deleted[table] = 0
            except Exception as exc:
                deleted[table] = f"error: {exc}"

        # lead_id-scoped table — email_events has no client_id column either
        lead_ids = [
            row["id"]
            for row in (sb.table("leads").select("id").eq("client_id", client_id).execute().data or [])
        ]
        try:
            if lead_ids:
                r = sb.table("email_events").delete().in_("lead_id", lead_ids).lt("timestamp", cutoff).execute()
                deleted["email_events"] = len(r.data or [])
            else:
                deleted["email_events"] = 0
        except Exception as exc:
            deleted["email_events"] = f"error: {exc}"

        results[client_id] = deleted

    return results


def purge_closed_accounts(sb, grace_days: int = CLOSED_ACCOUNT_GRACE_DAYS) -> list[str]:
    """
    Hard-delete every client whose closed_at is older than grace_days.
    Returns the list of purged client_ids. NULL closed_at (active accounts)
    never matches `lt`, so no explicit IS NOT NULL check is needed.
    """
    from utils.client_deletion import hard_delete_client

    cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).isoformat()
    resp = sb.table("clients").select("id, closed_at").lt("closed_at", cutoff).execute()

    purged = []
    for row in (resp.data or []):
        client_id = row["id"]
        try:
            hard_delete_client(sb, client_id)
            purged.append(client_id)
        except Exception as exc:
            logger.error("Failed to hard-delete closed account %s: %s", client_id, exc)
    return purged


def _run() -> int:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    try:
        aged = purge_aged_events(sb, DEFAULT_RETENTION_DAYS)
        total_aged = sum(
            v for per_client in aged.values() for v in per_client.values() if isinstance(v, int)
        )
        logger.info("purge_aged_events: %d row(s) purged across %d client(s).", total_aged, len(aged))
    except Exception as exc:
        logger.error("purge_aged_events failed: %s", exc)

    try:
        purged = purge_closed_accounts(sb, CLOSED_ACCOUNT_GRACE_DAYS)
        if purged:
            logger.warning("purge_closed_accounts: hard-deleted %d account(s): %s", len(purged), ", ".join(purged))
        else:
            logger.info("purge_closed_accounts: no accounts past the grace period.")
    except Exception as exc:
        logger.error("purge_closed_accounts failed: %s", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
