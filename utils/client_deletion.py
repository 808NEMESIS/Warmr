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

Second-tier FK ordering (confirmed via pg_constraint 2026-07-14):
warmup_logs/bounce_log/sending_schedule/email_events/reply_inbox all
reference inboxes/campaigns/leads with ON DELETE NO ACTION (not CASCADE,
unlike the client_id -> clients layer, which is CASCADE everywhere). Any of
these left over when inboxes/campaigns/leads (or, transitively, the final
`clients` delete) run will raise a foreign-key violation and abort that
delete. They must be cleared first, regardless of client_id-table ordering.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Every table confirmed (live production schema scan, Fase 0/1) to carry a
# client_id column, MINUS the ones handled explicitly and earlier below
# (reply_inbox, sending_schedule) for FK-ordering reasons.
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
    "reply_routing_rules",
    "sequence_suggestions",
    "suppression_list",
    "unsubscribe_tokens",
    "warmup_network_accounts",
    "webhook_events",
    "webhook_logs",
]

# Tables with no client_id column, scoped instead via inbox_id.
INBOX_SCOPED_TABLES: list[str] = ["warmup_logs", "bounce_log"]

# Tables that DO have client_id but must be deleted before inboxes/campaigns/
# leads, since they reference those with ON DELETE NO ACTION.
FK_BLOCKING_CLIENT_ID_TABLES: list[str] = ["reply_inbox", "sending_schedule"]


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
    lead_ids = [
        row["id"]
        for row in (
            supabase.table("leads").select("id").eq("client_id", client_id).execute().data
            or []
        )
    ]
    campaign_ids = [
        row["id"]
        for row in (
            supabase.table("campaigns").select("id").eq("client_id", client_id).execute().data
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

    # email_events has no client_id column at all (only inbox_id/campaign_id/
    # lead_id, each ON DELETE NO ACTION) — clear via every ID set this client
    # owns, since any one of the three columns may be populated on a given row.
    email_events_deleted = 0
    email_events_error = None
    for id_col, ids in (("inbox_id", inbox_ids), ("campaign_id", campaign_ids), ("lead_id", lead_ids)):
        if not ids:
            continue
        try:
            r = supabase.table("email_events").delete().in_(id_col, ids).execute()
            email_events_deleted += len(r.data or [])
        except Exception as exc:
            email_events_error = f"error: {exc}"
    deleted["email_events"] = email_events_error or email_events_deleted

    for table in FK_BLOCKING_CLIENT_ID_TABLES:
        try:
            r = supabase.table(table).delete().eq("client_id", client_id).execute()
            deleted[table] = len(r.data or [])
        except Exception as exc:
            deleted[table] = f"error: {exc}"

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
