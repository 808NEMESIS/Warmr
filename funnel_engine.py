"""
funnel_engine.py — Automated outbound funnel management.

Manages lead progression through funnel stages based on engagement signals:
  cold → warm → hot → meeting → won
  cold → nurture (no response after full sequence)
  any  → lost (not_interested reply)
  any  → unsubscribed (opt-out)

Also handles reply-based routing:
  interested    → auto-send calendar link, move to hot
  question      → notify user, keep in current stage
  not_interested → stop sequence, move to lost
  out_of_office  → reschedule +3 days
  referral       → create new lead from referral
  unsubscribe    → suppress, stop everything

Called after reply_classifier.py classifies an incoming reply,
and by campaign_scheduler.py after sequence completion.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

# Funnel stage definitions
STAGES = ("cold", "warm", "hot", "meeting", "won", "nurture", "lost", "unsubscribed")

# After how many sequence steps does a lead transition cold → warm?
WARM_AFTER_STEP = 2

# Nurture cooldown in days (before re-engagement is allowed)
NURTURE_COOLDOWN_DAYS = 90


def _get_sb() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Stage transitions
# ---------------------------------------------------------------------------

def move_to_stage(
    sb: Client,
    lead_id: str,
    new_stage: str,
    reason: str = "",
) -> None:
    """Move a lead to a new funnel stage + dispatch webhook."""
    if new_stage not in STAGES:
        logger.warning("Invalid funnel stage: %s", new_stage)
        return
    try:
        # Get current stage for webhook
        lead_resp = sb.table("leads").select("funnel_stage, client_id, email").eq("id", lead_id).limit(1).execute()
        old_stage = (lead_resp.data[0].get("funnel_stage") if lead_resp.data else "unknown")
        client_id = (lead_resp.data[0].get("client_id") if lead_resp.data else "")
        lead_email = (lead_resp.data[0].get("email") if lead_resp.data else "")

        update = {
            "funnel_stage": new_stage,
            "funnel_updated_at": _now(),
        }
        if new_stage == "nurture":
            update["nurture_until"] = (
                datetime.now(timezone.utc) + timedelta(days=NURTURE_COOLDOWN_DAYS)
            ).isoformat()

        sb.table("leads").update(update).eq("id", lead_id).execute()
        logger.info("Lead %s → funnel stage '%s' (reason: %s)", lead_id, new_stage, reason)

        # Dispatch webhook for stage change
        if client_id and old_stage != new_stage:
            _dispatch_stage_webhook(sb, client_id, lead_id, lead_email, old_stage, new_stage, reason)

    except Exception as exc:
        logger.error("Failed to move lead %s to stage %s: %s", lead_id, new_stage, exc)


def _dispatch_stage_webhook(sb: Client, client_id: str, lead_id: str, lead_email: str, old_stage: str, new_stage: str, reason: str) -> None:
    """Send a webhook event when a lead changes funnel stage."""
    try:
        import httpx
        integrations = sb.table("crm_integrations").select("webhook_url").eq("client_id", client_id).eq("active", True).eq("provider", "webhook").execute()
        for integ in (integrations.data or []):
            url = integ.get("webhook_url")
            if url:
                try:
                    httpx.post(url, json={
                        "event": "lead.stage_changed",
                        "lead_id": lead_id,
                        "lead_email": lead_email,
                        "old_stage": old_stage,
                        "new_stage": new_stage,
                        "reason": reason,
                        "timestamp": _now(),
                    }, timeout=10)
                except Exception:
                    pass
    except Exception:
        pass


def check_stage_progression(
    sb: Client,
    lead_id: str,
    current_step: int,
    total_steps: int,
    has_opened: bool = False,
    has_clicked: bool = False,
) -> None:
    """
    Check if a lead should progress to the next funnel stage based on engagement.

    Called after each send by campaign_scheduler.
    """
    lead_resp = sb.table("leads").select("funnel_stage").eq("id", lead_id).limit(1).execute()
    if not lead_resp.data:
        return
    current_stage = lead_resp.data[0].get("funnel_stage", "cold")

    # cold → warm: after step 2 or if opened/clicked
    if current_stage == "cold":
        if current_step >= WARM_AFTER_STEP or has_opened or has_clicked:
            move_to_stage(sb, lead_id, "warm", f"step {current_step}, opened={has_opened}")

    # warm → hot: requires a click or reaching step 4+
    if current_stage == "warm":
        if has_clicked or current_step >= 4:
            move_to_stage(sb, lead_id, "hot", f"step {current_step}, clicked={has_clicked}")


def on_sequence_complete(sb: Client, lead_id: str) -> None:
    """Called when a lead has exhausted all sequence steps without replying."""
    lead_resp = sb.table("leads").select("funnel_stage").eq("id", lead_id).limit(1).execute()
    if not lead_resp.data:
        return
    stage = lead_resp.data[0].get("funnel_stage", "cold")

    if stage not in ("meeting", "won", "lost", "unsubscribed"):
        move_to_stage(sb, lead_id, "nurture", "sequence completed without reply")


# ---------------------------------------------------------------------------
# Reply routing
# ---------------------------------------------------------------------------

def get_routing_rules(sb: Client, client_id: str) -> dict[str, dict]:
    """Fetch reply routing rules for a client. Returns {classification: rule_dict}."""
    resp = sb.table("reply_routing_rules").select("*").eq("client_id", client_id).eq("active", True).execute()
    return {r["classification"]: r for r in (resp.data or [])}


def get_default_rules() -> dict[str, dict]:
    """Default routing rules if client hasn't configured custom ones."""
    return {
        "interested": {
            "action": "send_calendar",
            "auto_reply_template": (
                "Leuk om te horen! Hier is mijn agenda om een kort gesprek in te plannen: {{calendar_link}}\n\n"
                "Kies een moment dat je uitkomt. Spreek je snel!\n\n{{sender_name}}"
            ),
            "notify": True,
        },
        "question": {
            "action": "notify_only",
            "notify": True,
        },
        "not_interested": {
            "action": "stop_sequence",
            "notify": True,
        },
        "out_of_office": {
            "action": "reschedule",
            "notify": False,
        },
        "referral": {
            "action": "create_referral",
            "notify": True,
        },
        "unsubscribe": {
            "action": "suppress",
            "notify": False,
        },
        "other": {
            "action": "notify_only",
            "notify": True,
        },
    }


def route_reply(
    sb: Client,
    client_id: str,
    lead_id: str,
    lead_email: str,
    classification: str,
    campaign_id: Optional[str] = None,
    reply_body: str = "",
) -> dict:
    """
    Route an incoming reply based on its classification.

    Returns a summary dict: { action_taken, notification_created, auto_reply_sent }.
    """
    # Get client-specific rules, fall back to defaults
    rules = get_routing_rules(sb, client_id)
    if not rules:
        rules = get_default_rules()

    rule = rules.get(classification, rules.get("other", {"action": "notify_only", "notify": True}))
    action = rule.get("action", "notify_only")
    result = {"action_taken": action, "notification_created": False, "auto_reply_sent": False}

    # Execute action
    if action == "send_calendar":
        move_to_stage(sb, lead_id, "hot", f"reply classified as {classification}")
        # Auto-reply with calendar link will be handled by the caller
        # (needs SMTP access which this module doesn't have)
        result["auto_reply_template"] = rule.get("auto_reply_template", "")
        result["auto_reply_sent"] = True

    elif action == "stop_sequence":
        move_to_stage(sb, lead_id, "lost", f"reply classified as {classification}")
        # Stop all active campaign_leads for this lead
        try:
            sb.table("campaign_leads").update({
                "status": "completed",
            }).eq("lead_id", lead_id).in_("status", ["active", "pending"]).execute()
        except Exception as exc:
            logger.error("Failed to stop sequences for lead %s: %s", lead_id, exc)

    elif action == "reschedule":
        # Push next_send_at forward by 3 days for all active campaign_leads
        try:
            new_send = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
            sb.table("campaign_leads").update({
                "next_send_at": new_send,
            }).eq("lead_id", lead_id).in_("status", ["active"]).execute()
            logger.info("Lead %s: rescheduled +3 days (out of office)", lead_id)
        except Exception as exc:
            logger.error("Failed to reschedule lead %s: %s", lead_id, exc)

    elif action == "create_referral":
        move_to_stage(sb, lead_id, "lost", "referred to someone else")
        # Extract referral info from reply body — caller should handle this
        result["referral_hint"] = reply_body[:500]

    elif action == "suppress":
        move_to_stage(sb, lead_id, "unsubscribed", "opt-out via reply")
        # Add to suppression list
        try:
            domain = lead_email.split("@")[-1] if "@" in lead_email else None
            sb.table("suppression_list").insert({
                "client_id": client_id,
                "email": lead_email,
                "domain": domain,
                "reason": "unsubscribe",
                "source": campaign_id or "reply",
            }).execute()
        except Exception:
            pass  # Already suppressed
        # Stop all sequences
        try:
            sb.table("campaign_leads").update({
                "status": "unsubscribed",
            }).eq("lead_id", lead_id).in_("status", ["active", "pending"]).execute()
        except Exception:
            pass

    elif action == "notify_only":
        # Just create a notification, don't change funnel stage
        pass

    # Create notification if configured
    if rule.get("notify", True):
        try:
            cls_labels = {
                "interested": "Geïnteresseerd",
                "question": "Vraag gesteld",
                "not_interested": "Niet geïnteresseerd",
                "out_of_office": "Afwezig",
                "referral": "Doorverwijzing",
                "unsubscribe": "Uitgeschreven",
                "other": "Overig",
            }
            label = cls_labels.get(classification, classification)
            sb.table("notifications").insert({
                "client_id": client_id,
                "type": "reply_received",
                "entity_id": lead_id,
                "entity_type": "lead",
                "message": f"Reply van {lead_email}: {label}. Actie: {action}.",
                "priority": "high" if classification == "interested" else "medium",
            }).execute()
            result["notification_created"] = True
        except Exception as exc:
            logger.error("Failed to create reply notification: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Funnel analytics snapshot
# ---------------------------------------------------------------------------

def snapshot_funnel(sb: Client, client_id: str) -> dict:
    """
    Take a snapshot of current funnel distribution for analytics.

    Returns: { "cold": N, "warm": N, "hot": N, ... }
    """
    from datetime import date
    today = date.today().isoformat()

    resp = sb.table("leads").select("funnel_stage").eq("client_id", client_id).execute()
    leads = resp.data or []

    counts: dict[str, int] = {s: 0 for s in STAGES}
    for lead in leads:
        stage = lead.get("funnel_stage", "cold")
        if stage in counts:
            counts[stage] += 1

    # Store snapshot
    for stage, count in counts.items():
        try:
            sb.table("funnel_analytics").upsert({
                "client_id": client_id,
                "date": today,
                "stage": stage,
                "lead_count": count,
            }, on_conflict="client_id,date,stage").execute()
        except Exception:
            pass

    return counts


# ---------------------------------------------------------------------------
# Seed default routing rules for a new client
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Domain bounce pattern auto-pause
# ---------------------------------------------------------------------------

def check_domain_bounce_pattern(sb: Client, client_id: str, campaign_id: str) -> list[str]:
    """
    Check if any company domain has 3+ bounces in a campaign.
    If so, pause all active leads from that domain.

    Returns list of paused domains.
    """
    paused_domains: list[str] = []
    try:
        # Get bounced leads in this campaign
        bounced = (
            sb.table("email_events")
            .select("lead_id")
            .eq("campaign_id", campaign_id)
            .eq("event_type", "bounced")
            .execute()
        )
        bounced_ids = [e["lead_id"] for e in (bounced.data or []) if e.get("lead_id")]
        if len(bounced_ids) < 3:
            return []

        # Get domains of bounced leads
        leads_resp = sb.table("leads").select("id, domain").in_("id", bounced_ids).execute()
        domain_counts: dict[str, list[str]] = {}
        for lead in (leads_resp.data or []):
            domain = (lead.get("domain") or "").lower()
            if domain and domain not in ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com"):
                domain_counts.setdefault(domain, []).append(lead["id"])

        # Pause domains with 3+ bounces
        for domain, lead_ids in domain_counts.items():
            if len(lead_ids) >= 3:
                # Pause all active campaign_leads for this domain's leads
                all_domain_leads = sb.table("leads").select("id").eq("client_id", client_id).eq("domain", domain).execute()
                all_ids = [l["id"] for l in (all_domain_leads.data or [])]
                if all_ids:
                    sb.table("campaign_leads").update({
                        "status": "paused",
                    }).in_("lead_id", all_ids).in_("status", ["active", "pending"]).execute()

                    # Create notification
                    sb.table("notifications").insert({
                        "client_id": client_id,
                        "type": "domain_bounce_pause",
                        "entity_id": campaign_id,
                        "entity_type": "campaign",
                        "message": f"Domein {domain} heeft {len(lead_ids)} bounces — alle leads van dit domein zijn gepauzeerd.",
                        "priority": "high",
                    }).execute()

                    paused_domains.append(domain)
                    logger.warning("Domain %s: %d bounces → paused all leads from this domain.", domain, len(lead_ids))

    except Exception as exc:
        logger.error("Domain bounce pattern check failed: %s", exc)

    return paused_domains


# ---------------------------------------------------------------------------
# Auto re-engagement for nurture leads
# ---------------------------------------------------------------------------

def check_nurture_reengagement(sb: Client, client_id: str) -> list[str]:
    """
    Find leads in 'nurture' stage whose cooldown has expired.
    Moves them back to 'cold' so they can be re-engaged.

    Returns list of re-engaged lead IDs.
    """
    reengaged: list[str] = []
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        resp = (
            sb.table("leads")
            .select("id, email")
            .eq("client_id", client_id)
            .eq("funnel_stage", "nurture")
            .lt("nurture_until", now_iso)
            .limit(100)
            .execute()
        )
        leads = resp.data or []

        for lead in leads:
            move_to_stage(sb, lead["id"], "cold", "nurture cooldown expired — ready for re-engagement")
            reengaged.append(lead["id"])

        if reengaged:
            sb.table("notifications").insert({
                "client_id": client_id,
                "type": "nurture_reengagement",
                "message": f"{len(reengaged)} lead(s) zijn klaar voor re-engagement na {NURTURE_COOLDOWN_DAYS} dagen cooldown.",
                "priority": "medium",
            }).execute()
            logger.info("Re-engagement: %d leads moved from nurture → cold for client %s.", len(reengaged), client_id)

    except Exception as exc:
        logger.error("Nurture re-engagement check failed: %s", exc)

    return reengaged


def seed_default_rules(sb: Client, client_id: str) -> None:
    """Insert default reply routing rules for a new client."""
    defaults = get_default_rules()
    for classification, rule in defaults.items():
        try:
            sb.table("reply_routing_rules").insert({
                "client_id": client_id,
                "classification": classification,
                "action": rule["action"],
                "auto_reply_template": rule.get("auto_reply_template"),
                "notify": rule.get("notify", True),
                "active": True,
            }).execute()
        except Exception:
            pass  # Already exists
