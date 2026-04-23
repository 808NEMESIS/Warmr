"""
utils/notifier.py — operator notifications via Resend (falls back silently).

Used by:
  - imap_processor: ping the operator when a real prospect reply lands, so
    they don't miss it while Warmr's dashboard is not open.
  - bounce_handler: alert on reputation-risk events (future).

Contract:
  notify_operator(sb, client_id, subject, html)
  notify_new_reply(sb, reply_row)

All functions are non-blocking and never raise — a notification failure must
never break the pipeline that triggered it.

Throttling: each (client_id, event_type) is capped via notifications.client_id
+ a per-hour window. Prevents email storms when many replies land in one cycle.

Config:
  RESEND_API_KEY        — required for actual delivery
  WARMR_NOTIFY_FROM     — sender email (default noreply@warmr.local)
  WARMR_NOTIFY_MUTE     — set to 1 to suppress all sends (dev/testing)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL: str = os.getenv("WARMR_NOTIFY_FROM", "noreply@warmr.local")
MUTE: bool = os.getenv("WARMR_NOTIFY_MUTE", "0") == "1"

# Minimum minutes between two operator pings for the same (client, kind)
_THROTTLE_WINDOW_MIN = int(os.getenv("WARMR_NOTIFY_THROTTLE_MIN", "15"))


def _operator_email_for(sb, client_id: str) -> Optional[str]:
    """Fetch the operator's email — prefer client_settings.reply_to_email,
    fall back to clients.email."""
    try:
        cs = (
            sb.table("client_settings")
            .select("reply_to_email")
            .eq("client_id", client_id)
            .limit(1)
            .execute()
        )
        if cs.data and cs.data[0].get("reply_to_email"):
            return cs.data[0]["reply_to_email"]
    except Exception as exc:
        logger.debug("client_settings lookup failed: %s", exc)

    try:
        c = (
            sb.table("clients")
            .select("email")
            .eq("id", client_id)
            .limit(1)
            .execute()
        )
        if c.data:
            return c.data[0].get("email")
    except Exception as exc:
        logger.debug("clients lookup failed: %s", exc)

    return None


def _recently_notified(sb, client_id: str, kind: str) -> bool:
    """Check the notifications table for a matching ping within the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_THROTTLE_WINDOW_MIN)).isoformat()
    try:
        resp = (
            sb.table("notifications")
            .select("id")
            .eq("client_id", client_id)
            .eq("type", kind)
            .gte("timestamp", cutoff)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception:
        return False


def _record_notification(sb, client_id: str, kind: str, title: str, body: str) -> None:
    """Write a row to the notifications table so the UI badge also updates."""
    try:
        sb.table("notifications").insert({
            "client_id": client_id,
            "type":      kind,
            "message":   (title + " — " + body)[:2000],
            "priority":  "medium",
            "read":      False,
        }).execute()
    except Exception as exc:
        logger.debug("notifications insert failed: %s", exc)


def _send_via_resend(to_email: str, subject: str, html: str) -> bool:
    if MUTE:
        logger.info("Notifier muted — would have emailed %s: %s", to_email, subject[:60])
        return False
    if not RESEND_API_KEY:
        logger.info("RESEND_API_KEY not set — would have emailed %s: %s", to_email, subject[:60])
        return False
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": to_email, "subject": subject, "html": html},
            timeout=15,
        )
        if resp.status_code >= 300:
            logger.warning("Resend returned %d: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        logger.warning("Resend send failed: %s", exc)
        return False


# ── Public API ────────────────────────────────────────────────────────────

def notify_operator(
    sb,
    client_id: str,
    kind: str,
    title: str,
    html_body: str,
    throttle: bool = True,
) -> bool:
    """Send a one-off notification to the client's operator inbox.

    Returns True on delivery. Non-delivery (no key, throttled, no address) is
    silent — records the notification in the DB so the dashboard can show it.
    """
    if not client_id:
        return False
    if throttle and _recently_notified(sb, client_id, kind):
        logger.debug("Skipping notification (throttled): %s / %s", client_id, kind)
        _record_notification(sb, client_id, kind, title, "[throttled email]")
        return False

    _record_notification(sb, client_id, kind, title, html_body[:500])

    to_email = _operator_email_for(sb, client_id)
    if not to_email:
        return False
    return _send_via_resend(to_email, title, html_body)


def notify_lead_clicked(
    sb,
    client_id: str,
    lead_id: str,
    lead_email: str,
    campaign_id: Optional[str],
    clicked_url: str,
) -> bool:
    """Ping the operator the FIRST time a lead clicks a tracked link.

    Deduped per (client_id, lead_id) via the throttle window — a prospect
    hammering 5 links in a minute gets one email, not five. Subsequent clicks
    the same day are absorbed into the dashboard badge count only.

    Always emits a lead.clicked webhook event so CRM/Heatr can react even
    when the notifier itself is throttled or muted.
    """
    # Always emit the webhook event (separate concern from operator email)
    try:
        sb.table("webhook_events").insert({
            "client_id":  client_id,
            "event_type": "lead.clicked",
            "payload": {
                "lead_id":     lead_id,
                "email":       lead_email,
                "campaign_id": campaign_id,
                "clicked_url": clicked_url,
            },
            "dispatched": False,
        }).execute()
    except Exception as exc:
        logger.debug("webhook_events insert for lead.clicked failed: %s", exc)

    # Kind is scoped to the lead so different leads don't throttle each other.
    kind = f"lead_clicked:{lead_id}"
    domain = clicked_url.split("/")[2] if "://" in clicked_url and "/" in clicked_url.split("://", 1)[1] else clicked_url[:60]
    title = f"🎯 {lead_email} klikte op je link"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;padding:24px">
      <h2 style="margin:0 0 12px">🎯 Hot signaal: click</h2>
      <p style="color:#333;line-height:1.5">
        <strong>{lead_email}</strong> heeft zojuist op een link geklikt in je campagne.
        Een klik is een sterker koopsignaal dan een open — opvolgen binnen 24 uur
        levert meetbaar meer meetings op.
      </p>
      <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;background:#fafafa;margin:16px 0">
        <div style="font-size:12px;color:#666">Bestemming</div>
        <div style="word-break:break-all;font-size:13px"><a href="{clicked_url}">{domain}</a></div>
      </div>
      <p style="color:#666;font-size:12px">
        Volgende klikken vandaag van dezelfde lead worden samengevat in je dashboard
        (geen extra email).
      </p>
    </div>
    """.strip()
    return notify_operator(sb, client_id, kind, title, html, throttle=True)


def notify_new_reply(sb, reply_row: dict) -> bool:
    """Compose and send a 'new prospect reply' operator ping.

    reply_row shape: whatever was inserted into reply_inbox (from imap_processor).
    """
    client_id = reply_row.get("client_id")
    if not client_id:
        return False

    from_email = reply_row.get("from_email", "unknown")
    subject = reply_row.get("subject", "(no subject)")
    category = reply_row.get("classification") or "other"
    urgency = reply_row.get("urgency") or "low"
    meeting_intent = bool(reply_row.get("meeting_intent"))
    body_preview = (reply_row.get("body") or "")[:500]

    badge = "🔥" if meeting_intent else ("⚠️" if urgency == "high" else "📨")
    title = f"{badge} {from_email} — {category}" + (" · meeting intent" if meeting_intent else "")

    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;padding:24px">
      <h2 style="margin:0 0 12px">{badge} Nieuwe reply van {from_email}</h2>
      <div style="color:#555;font-size:13px;margin-bottom:16px">
        <strong>Categorie:</strong> {category} &middot;
        <strong>Urgentie:</strong> {urgency} &middot;
        <strong>Meeting-intent:</strong> {"ja" if meeting_intent else "nee"}
      </div>
      <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;background:#fafafa">
        <div style="font-weight:600;margin-bottom:8px">{subject}</div>
        <div style="white-space:pre-wrap;color:#333;line-height:1.5">{body_preview}</div>
      </div>
      <p style="color:#666;font-size:12px;margin-top:16px">
        Reageer direct via Warmr's unified inbox. Analyze-intent en ↩ Stuur reply
        knoppen zitten naast het bericht.
      </p>
    </div>
    """.strip()

    return notify_operator(sb, client_id, "new_reply", title, html, throttle=False)
