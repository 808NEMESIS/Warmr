"""
utils/service_audit.py — audit wrapper for service-role Supabase queries.

Service role bypasses RLS, so we need explicit accountability:
  - Who (which worker/script) ran this query?
  - What (operation, table, target client)?
  - When (timestamp)?

Writes are always audited (100%). Reads are sampled at 1% to avoid log bloat.

Usage:
    from utils.service_audit import audited_write, audited_read_sample, set_worker_name

    set_worker_name("campaign_scheduler")

    # Writes — always logged
    audited_write(sb, "campaign_leads", client_id="...", op="mark_completed",
                  details={"campaign_lead_id": "...", "status": "completed"})

    # Reads — sampled
    audited_read_sample(sb, "leads", client_id="...", op="fetch_due_leads")
"""

from __future__ import annotations

import logging
import os
import random
from typing import Any

logger = logging.getLogger(__name__)

# Worker name is set once on startup; used to attribute all queries to a known worker.
_WORKER_NAME: str = os.getenv("WARMR_WORKER_NAME", "unknown")

# Sampling rate for reads — 1% by default, override via env for debugging
_READ_SAMPLE_RATE = float(os.getenv("WARMR_SERVICE_AUDIT_SAMPLE", "0.01"))


def set_worker_name(name: str) -> None:
    """Attribute subsequent queries to this worker. Call once at startup."""
    global _WORKER_NAME
    _WORKER_NAME = name


def audited_write(
    sb: Any,
    table: str,
    client_id: str | None = None,
    op: str = "write",
    details: dict | None = None,
) -> None:
    """Record a service-role write. Always logs (100%)."""
    _insert_audit(sb, table=table, op=op, client_id=client_id, details=details)


def audited_read_sample(
    sb: Any,
    table: str,
    client_id: str | None = None,
    op: str = "read",
    details: dict | None = None,
) -> None:
    """Record a service-role read. Sampled at WARMR_SERVICE_AUDIT_SAMPLE rate."""
    if random.random() > _READ_SAMPLE_RATE:
        return
    _insert_audit(sb, table=table, op=op, client_id=client_id, details=details)


def _insert_audit(
    sb: Any,
    table: str,
    op: str,
    client_id: str | None,
    details: dict | None,
) -> None:
    """Insert a service_query_log row. Silent on failure — never breaks the caller."""
    try:
        sb.table("service_query_log").insert({
            "worker_name": _WORKER_NAME,
            "operation": op,
            "target_table": table,
            "target_client_id": client_id,
            "details": details or {},
        }).execute()
    except Exception as exc:
        logger.debug("service_audit insert failed: %s", exc)
