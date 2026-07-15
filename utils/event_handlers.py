"""
utils/event_handlers.py — subscribers for domain events published via
utils/events.py (Fase 4, architecture item 2).

Importing this module registers every handler below against
utils.events.default_bus. Callers that publish events must import this
module first (see imap_processor.py) — Python only runs the @subscribe
decorators once the module is actually imported.

"lead.replied" handlers (in registration order — order doesn't matter
functionally since each runs independently, but this mirrors the order the
original inline try/except blocks ran in imap_processor.py before this fix):

  1. _notify_operator   — relocated unchanged from imap_processor.py
  2. _emit_webhook_event — relocated unchanged from imap_processor.py
  3. _update_lead_status — relocated unchanged from imap_processor.py
  4. _route_funnel       — NEW: closes the gap where the automatic reply
                            path never called funnel_engine.route_reply()
  5. _bump_engagement    — NEW: closes the gap where engagement_scorer's
                            "replied"/"interested" scores were never applied
  6. _sync_crm           — NEW: closes the gap where crm_dispatcher.dispatch_event
                            had zero production call sites

The classification -> CRM/engagement event-type mapping below is a product
decision (which reply categories count as "interested" for a CRM, whether a
"meeting" event type is ever produced), not an architecture one — flagged
here rather than assumed silently. Revisit if reply_classifier's categories
change.
"""
from __future__ import annotations

import logging

from utils.events import subscribe

logger = logging.getLogger(__name__)


@subscribe("lead.replied")
def _notify_operator(sb, event) -> None:
    from utils.notifier import notify_new_reply
    try:
        notify_new_reply(sb, event.payload)
    except Exception as exc:
        logger.debug("notify_new_reply failed: %s", exc)


@subscribe("lead.replied")
def _emit_webhook_event(sb, event) -> None:
    p = event.payload
    event_type = "lead.interested" if p.get("classification") == "interested" else "lead.replied"
    try:
        sb.table("webhook_events").insert({
            "client_id": event.client_id,
            "event_type": event_type,
            "payload": {
                "lead_id": p["lead_id"],
                "email": p["from_email"],
                "subject": p.get("subject"),
                "classification": p.get("classification"),
                "meeting_intent": bool(p.get("meeting_intent")),
                "urgency": p.get("urgency"),
            },
            "dispatched": False,
        }).execute()
    except Exception as exc:
        logger.debug("webhook_events insert failed: %s", exc)


@subscribe("lead.replied")
def _update_lead_status(sb, event) -> None:
    p = event.payload
    cat = p.get("classification")
    if cat == "unsubscribe":
        new_status = "unsubscribed"
    elif cat == "interested":
        new_status = "interested"
    else:
        new_status = "replied"
    try:
        sb.table("leads").update({"status": new_status}).eq("id", p["lead_id"]).eq("client_id", event.client_id).execute()
    except Exception as exc:
        logger.debug("leads status update failed: %s", exc)


@subscribe("lead.replied")
def _route_funnel(sb, event) -> None:
    """Closes the traced gap: the automatic reply path never called
    route_reply — funnel-stage routing was only reachable via the manual
    POST /funnel/route-reply dashboard endpoint."""
    from funnel_engine import route_reply
    p = event.payload
    try:
        route_reply(
            sb, event.client_id, p["lead_id"], p["from_email"], p.get("classification"),
            campaign_id=p.get("campaign_id"), reply_body=p.get("body", ""),
        )
    except Exception as exc:
        logger.debug("route_reply failed: %s", exc)


@subscribe("lead.replied")
def _bump_engagement(sb, event) -> None:
    """Closes the traced gap: engagement_scorer.SCORES defines "replied"
    (+25) and "interested" (+50), but no call site anywhere passed those
    event types — only "opened"/"clicked" from the tracking endpoints did."""
    from engagement_scorer import add_engagement
    p = event.payload
    try:
        add_engagement(sb, p["lead_id"], "replied")
        if p.get("classification") == "interested":
            add_engagement(sb, p["lead_id"], "interested")
    except Exception as exc:
        logger.debug("add_engagement failed: %s", exc)


@subscribe("lead.replied")
def _sync_crm(sb, event) -> None:
    """Closes the traced gap: crm_dispatcher.dispatch_event() had zero
    production call sites despite its own docstring claiming callers in
    reply_classifier.py/imap_processor.py that don't exist."""
    from crm_dispatcher import dispatch_event
    p = event.payload
    event_type = "interested" if p.get("classification") == "interested" else "reply"
    try:
        dispatch_event(event.client_id, {
            "id": p["lead_id"],
            "email": p["from_email"],
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "company": p.get("company"),
            "phone": p.get("phone"),
        }, event_type, sb=sb)
    except Exception as exc:
        logger.debug("dispatch_event failed: %s", exc)
