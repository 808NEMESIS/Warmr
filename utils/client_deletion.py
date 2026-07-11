"""
utils/client_deletion.py — GDPR Article 17 hard-delete for an entire client
account (Fase 2, item 3).

The previous inline implementation in api/main.py's admin_delete_client only
touched 7 tables (warmup_logs, sending_schedule, bounce_log, inboxes,
domains, campaigns, clients) out of the ~30 tables that actually carry
client_id — every other table (leads, campaign_leads, email_tracking,
suppression_list, webhook_events, ...) was silently left behind. Every
failure was also swallowed by a bare `except: pass`, so there was no way to
tell what actually got deleted.

This module is the single, reusable implementation — called from both the
admin API route and, in Track 2, retention_engine.py's closed-account purge.
Kept dependency-free of FastAPI (same pattern as utils/notifier.py and
utils/job_lock.py) so it can be imported from a standalone script.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Every table confirmed (live production schema scan, Fase 0/1) to carry a
# client_id column. warmup_logs and bounce_log are deliberately NOT in this
# list — they have no client_id column, only inbox_id (see step 2 below).
CLIENT_ID_TABLES: list[str] = [
    "analytics_cache",
    "api_cost_log",
    "api_keys",
    "campaign_leads",
    "campaigns",
    "client_settings",
    "content_scores",
    "crm_integrations",
    "crm_sync_log",
    "decision_log",
    "diagnostics_log",
    "domains",
    "email_tracking",
    "enrichment_queue",
    "experiments",
    "funnel_analytics",
    "inboxes",
    "leads",
    "network_health_log",
    "notifications",
    "placement_tests",
    "reply_inbox",
    "reply_routing_rules",
    "sending_schedule",
    "sequence_suggestions",
    "suppression_list",
    "unsubscribe_tokens",
    "warmup_network_accounts",
    "webhook_events",
    "webhook_logs",
]

# Tables with no client_id column, scoped instead via inbox_id.
INBOX_SCOPED_TABLES: list[str] = ["warmup_logs", "bounce_log"]


def hard_delete_client(supabase, client_id: str) -> dict:
    """Permanently delete a client and every row that belongs to them.

    Returns a dict of {table: deleted_row_count | "error: ..."} — every step
    is independently try/except'd and recorded, never silently swallowed.
    """
    deleted: dict = {}

    inbox_ids = [
        row["id"]
        for row in (
            supabase.table("inboxes").select("id").eq("client_id", client_id).execute().data
            or []
        )
    ]

    if inbox_ids:
        for table in INBOX_SCOPED_TABLES:
            try:
                r = supabase.table(table).delete().in_("inbox_id", inbox_ids).execute()
                deleted[table] = len(r.data or [])
            except Exception as exc:
                deleted[table] = f"error: {exc}"
    else:
        for table in INBOX_SCOPED_TABLES:
            deleted[table] = 0

    for table in CLIENT_ID_TABLES:
        try:
            r = supabase.table(table).delete().eq("client_id", client_id).execute()
            deleted[table] = len(r.data or [])
        except Exception as exc:
            deleted[table] = f"error: {exc}"

    try:
        r = supabase.table("clients").delete().eq("id", client_id).execute()
        deleted["clients"] = len(r.data or [])
    except Exception as exc:
        deleted["clients"] = f"error: {exc}"

    return deleted
