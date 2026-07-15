"""
utils/state_machines_registry.py — the two state machines currently wired,
in log-only mode, into existing write sites (Fase 4b).

Both graphs below were reconstructed by directly reading every write site
for the field (not from memory/prior research) on 2026-07-15:

  campaign_leads.status: campaign_scheduler.py (:928 hard-bounce pre-check,
    :971 atomic claim, :1010 revert-on-failure/completion), funnel_engine.py
    (:249 stop_sequence, :395 domain-bounce pause), bounce_handler.py (:309),
    utils/suppression.py (:47), reap_stranded_sends.py (:70).

  inboxes.status: auto_promote.py, bounce_handler.py (:350),
    diagnostics_engine.py (3 sites: critical-reputation pause, auto-resume,
    SMTP-error pause), api/main.py's inbox-create + pause endpoints.

Two things confirmed while reconstructing these that matter for how they're
used: campaign_leads.status is NEVER written as "pending" by any code path
(the two campaign_leads insert sites in api/public_api.py rely on the
schema's DEFAULT 'active'; "pending" only appears in defensive
`.in_("status", ["active", "pending"])` read-filters) — likely dead/legacy.
Same for inboxes.status = "retired" (never written, only read via `.neq`
filters) — likely a manual, outside-the-app DB action for permanent
decommissioning, not a value.

Because of that uncertainty, and because this project has NO direct
production DB access this session, these graphs are marked DRAFT and are
wired ONLY in log-only mode (utils/state_machine.py's validate() is called,
mismatches are logged, writes are never blocked). Do not add a CHECK
constraint or a blocking trigger from these graphs until the log-only period
has run against real traffic and the "pending"/"retired" questions are
resolved against actual production data (see api/state_machine_migration.sql
for the verification query to run first).
"""
from __future__ import annotations

from utils.state_machine import StateMachine

CAMPAIGN_LEAD_STATUS = StateMachine(
    name="campaign_leads.status",
    states=frozenset({"active", "pending", "sending", "completed", "paused", "bounced", "unsubscribed"}),
    transitions={
        None: frozenset({"active"}),
        "active": frozenset({"sending", "bounced", "completed", "paused", "unsubscribed"}),
        # "pending" -> never observed as a write target; kept for parity with
        # the read-filters that treat it like "active".
        "pending": frozenset({"bounced", "completed", "paused", "unsubscribed"}),
        "sending": frozenset({"active", "completed"}),
    },
)

INBOX_STATUS = StateMachine(
    name="inboxes.status",
    states=frozenset({"warmup", "ready", "paused", "retired"}),
    transitions={
        None: frozenset({"warmup"}),
        "warmup": frozenset({"ready", "paused"}),
        "ready": frozenset({"paused"}),
        "paused": frozenset({"warmup"}),
        # "retired" has no incoming transition in any code path found —
        # legal value (schema/CHECK should allow it), not a reachable target.
    },
)
