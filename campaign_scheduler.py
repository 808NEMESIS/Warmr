"""
campaign_scheduler.py

Campaign email scheduler — the core outbound engine of Warmr.

Called by n8n every 5 minutes during the send window.

Flow per run:
  1. Load all active campaigns from Supabase
  2. Per campaign: fetch campaign_leads where next_send_at <= NOW() and status = 'active'
  3. Per lead:
     a. Fetch the correct sequence_step (with A/B variant selection)
     b. Process spintax + substitute variables
     c. Select sending inbox via inbox_rotator
     d. Send via SMTP SSL (port 465); capture Message-ID for reply threading
     e. Log to email_events
     f. Update campaign_leads: current_step, next_send_at, thread_message_id
     g. If all steps sent: mark campaign_lead as completed
  4. After each campaign: check bounce rate against bounce_threshold; auto-pause if exceeded
  5. Respect campaign daily_limit across all inboxes combined

Safety guarantees:
  - One inbox sends at most 1 campaign email per 3 minutes (enforced via inbox_rotator load ratio)
  - Any exception per lead is caught, logged to email_events, and processing continues
  - Any exception per campaign is caught and processing continues for remaining campaigns
"""

import email.utils
import logging
import os
import random
import smtplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from supabase import Client, create_client

from ab_test_engine import select_variant
from inbox_rotator import select_inbox
from spintax_engine import process_content

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

SMTP_PORT: int = 465
SMTP_SERVERS: dict[str, str] = {
    "google": "smtp.gmail.com",
    "microsoft": "smtp.office365.com",
}
WARMR_BASE_URL: str = os.getenv("WARMR_BASE_URL", "http://localhost:8000")

# Inter-send delay range per inbox (seconds) — prevents burst patterns
MIN_SEND_DELAY: float = 30.0
MAX_SEND_DELAY: float = 180.0  # 3 minutes max


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def load_inbox_credentials() -> dict[str, str]:
    """
    Build email → password mapping from INBOX_*_EMAIL / _PASSWORD env vars.

    Passwords are never stored in Supabase. This mapping is rebuilt on every
    run so credential rotations take effect without restarting the process.
    """
    creds: dict[str, str] = {}
    i = 1
    while True:
        email_addr = os.getenv(f"INBOX_{i}_EMAIL")
        if not email_addr:
            break
        password = os.getenv(f"INBOX_{i}_PASSWORD", "")
        creds[email_addr.strip().lower()] = password.strip()
        i += 1
    return creds


# ---------------------------------------------------------------------------
# Suppression & Unsubscribe helpers
# ---------------------------------------------------------------------------

def load_client_settings(supabase: Client, client_id: str) -> dict:
    """Fetch per-client global settings (booking_url, sender_name, signature)."""
    if not client_id:
        return {}
    try:
        resp = supabase.table("client_settings").select("*").eq("client_id", client_id).limit(1).execute()
        return (resp.data or [{}])[0] or {}
    except Exception:
        return {}


def is_suppressed(supabase: Client, client_id: str, email: str) -> bool:
    """Check if an email address is on the client's suppression list."""
    resp = (
        supabase.table("suppression_list")
        .select("id")
        .eq("client_id", client_id)
        .eq("email", email.lower().strip())
        .limit(1)
        .execute()
    )
    return bool(resp.data)


def generate_unsubscribe_link(supabase: Client, client_id: str, lead_id: str, lead_email: str, campaign_id: str | None = None) -> str:
    """Create a unique unsubscribe token and return the full URL."""
    import secrets
    token = secrets.token_urlsafe(32)
    supabase.table("unsubscribe_tokens").insert({
        "token": token,
        "client_id": client_id,
        "lead_id": lead_id,
        "lead_email": lead_email,
        "campaign_id": campaign_id,
    }).execute()
    return f"{WARMR_BASE_URL}/unsubscribe/{token}"


def append_unsubscribe_footer(body: str, unsub_url: str) -> str:
    """Append an unsubscribe line to the email body."""
    return body.rstrip() + "\n\n---\nNiet meer ontvangen? Uitschrijven: " + unsub_url


# ---------------------------------------------------------------------------
# Open & click tracking helpers
# ---------------------------------------------------------------------------

def _make_tracking_token(client_id: str, campaign_id: str, lead_id: str, lead_email: str) -> str:
    """Create an HMAC-signed tracking token from campaign/lead context."""
    import base64, hashlib, hmac
    secret = os.getenv("WARMR_API_TOKEN", "fallback-secret-change-me")
    raw = f"{client_id}|{campaign_id}|{lead_id}|{lead_email}"
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
    payload = f"{raw}|{sig}"
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def inject_tracking_pixel(html_body: str, tracking_token: str) -> str:
    """Inject a 1x1 tracking pixel before </body> or at the end of an HTML email."""
    pixel = f'<img src="{WARMR_BASE_URL}/t/{tracking_token}.gif" width="1" height="1" style="display:none" alt="">'
    if "</body>" in html_body.lower():
        idx = html_body.lower().rfind("</body>")
        return html_body[:idx] + pixel + html_body[idx:]
    return html_body + pixel


def check_step_condition(supabase: Client, step: dict, lead_id: str, campaign_id: str, current_step: int) -> bool:
    """
    Evaluate whether a conditional step should send.

    Returns True if the condition passes (or is 'always') and the step should be sent.
    Returns False if the condition fails and the step should be skipped.
    """
    cond_type = (step.get("condition_type") or "always").lower()
    if cond_type == "always":
        return True

    check_step = step.get("condition_step") or (current_step - 1)
    if check_step < 1:
        return True  # No previous step to check, default to send

    # Look up tracking events for this lead+campaign at the check_step
    # We use email_events (opened/clicked) which our tracking pixel feeds into
    event_type = "opened" if "opened" in cond_type else "clicked"
    resp = (
        supabase.table("email_events")
        .select("id")
        .eq("campaign_id", campaign_id)
        .eq("lead_id", lead_id)
        .eq("event_type", event_type)
        .limit(1)
        .execute()
    )
    has_event = bool(resp.data)

    if cond_type in ("if_opened", "if_clicked"):
        return has_event
    if cond_type in ("if_not_opened", "if_not_clicked"):
        return not has_event
    return True


def wrap_links_for_tracking(html_body: str, tracking_token: str) -> str:
    """Replace href URLs with tracked redirect URLs."""
    import re
    def _replace(match: re.Match) -> str:
        url = match.group(1)
        # Don't wrap unsubscribe links or mailto: or # anchors
        if "unsubscribe" in url.lower() or url.startswith("mailto:") or url.startswith("#"):
            return match.group(0)
        from urllib.parse import quote
        tracked = f"{WARMR_BASE_URL}/c/{tracking_token}?url={quote(url, safe='')}"
        return f'href="{tracked}"'
    return re.sub(r'href="(https?://[^"]+)"', _replace, html_body, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def load_active_campaigns(supabase: Client) -> list[dict]:
    """Fetch all campaigns with status='active'."""
    resp = (
        supabase.table("campaigns")
        .select("*")
        .eq("status", "active")
        .execute()
    )
    return resp.data or []


def load_due_campaign_leads(supabase: Client, campaign_id: str) -> list[dict]:
    """
    Fetch campaign_leads that are due to receive their next email.

    Criteria: status='active' AND next_send_at <= NOW() (UTC).
    Includes joined lead data via a select expansion.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    resp = (
        supabase.table("campaign_leads")
        .select("*, lead:lead_id(*)")
        .eq("campaign_id", campaign_id)
        .eq("status", "active")
        .lte("next_send_at", now_iso)
        .order("next_send_at")
        .execute()
    )
    return resp.data or []


def load_sequence_steps(supabase: Client, campaign_id: str) -> list[dict]:
    """
    Fetch all sequence steps for a campaign, ordered by step_number.

    All A/B variant rows for the same step_number are returned together;
    ab_test_engine.select_variant() picks which row to use per lead.
    """
    resp = (
        supabase.table("sequence_steps")
        .select("*")
        .eq("campaign_id", campaign_id)
        .order("step_number")
        .execute()
    )
    return resp.data or []


def get_steps_for_number(
    all_steps: list[dict],
    step_number: int,
) -> list[dict]:
    """Return all sequence_step rows that match the given step_number."""
    return [s for s in all_steps if s.get("step_number") == step_number]


def count_campaign_sends_today(supabase: Client, campaign_id: str) -> int:
    """Count emails already sent for this campaign today (UTC)."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    resp = (
        supabase.table("email_events")
        .select("id", count="exact")
        .eq("campaign_id", campaign_id)
        .eq("event_type", "sent")
        .gte("timestamp", today_start)
        .execute()
    )
    return resp.count or 0


def log_email_event(
    supabase: Client,
    campaign_id: str,
    lead_id: str,
    sequence_step_id: str,
    inbox_id: str,
    event_type: str,
    ab_variant: Optional[str] = None,
    message_id: str = "",
    notes: str = "",
) -> None:
    """Insert one row into email_events. Never raises."""
    try:
        row = {
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "sequence_step_id": sequence_step_id,
            "inbox_id": inbox_id,
            "event_type": event_type,
            "ab_variant": ab_variant,
            "message_id": message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table("email_events").insert(row).execute()
    except Exception as exc:
        logger.error("Failed to log email_event (%s): %s", event_type, exc)


def update_campaign_lead_after_send(
    supabase: Client,
    campaign_lead_id: str,
    next_step: int,
    next_send_at: datetime,
    thread_message_id: Optional[str],
    completed: bool,
    last_inbox_id: Optional[str] = None,
) -> None:
    """
    Update campaign_leads after a successful send.

    If completed=True the lead has exhausted all sequence steps and is marked done.
    Otherwise next_step and next_send_at are advanced.
    last_inbox_id is stored for inbox rotation on the next step.
    """
    update: dict = {
        "current_step": next_step,
        "next_send_at": next_send_at.isoformat(),
    }
    if completed:
        update["status"] = "completed"
    if thread_message_id:
        update["thread_message_id"] = thread_message_id
    if last_inbox_id:
        update["last_inbox_id"] = last_inbox_id

    try:
        supabase.table("campaign_leads").update(update).eq("id", campaign_lead_id).execute()
    except Exception as exc:
        logger.error("Failed to update campaign_lead %s: %s", campaign_lead_id, exc)


def mark_campaign_lead(
    supabase: Client,
    campaign_lead_id: str,
    status: str,
) -> None:
    """Set campaign_leads.status to an arbitrary value (paused, bounced, unsubscribed, etc.)."""
    try:
        supabase.table("campaign_leads").update({"status": status}).eq("id", campaign_lead_id).execute()
    except Exception as exc:
        logger.error("Failed to mark campaign_lead %s as %s: %s", campaign_lead_id, status, exc)


def mark_lead_status(supabase: Client, lead_id: str, status: str) -> None:
    """Update the global lead status on the leads table."""
    try:
        supabase.table("leads").update({"status": status}).eq("id", lead_id).execute()
    except Exception as exc:
        logger.error("Failed to update lead %s status to %s: %s", lead_id, status, exc)


def pause_campaign(supabase: Client, campaign_id: str, reason: str) -> None:
    """Set campaign status to 'paused' and log the reason."""
    try:
        supabase.table("campaigns").update({"status": "paused"}).eq("id", campaign_id).execute()
        logger.warning("Campaign %s auto-paused: %s", campaign_id, reason)
    except Exception as exc:
        logger.error("Failed to pause campaign %s: %s", campaign_id, exc)


def has_lead_replied(supabase: Client, campaign_id: str, lead_id: str) -> bool:
    """Check reply_inbox for an existing reply from this lead in this campaign."""
    resp = (
        supabase.table("reply_inbox")
        .select("id")
        .eq("campaign_id", campaign_id)
        .eq("lead_id", lead_id)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


# ---------------------------------------------------------------------------
# Bounce rate check
# ---------------------------------------------------------------------------

def calculate_bounce_rate(supabase: Client, campaign_id: str) -> float:
    """
    Calculate the current hard bounce rate for a campaign.

    bounce_rate = bounced_sends / total_sends (from email_events).
    Returns 0.0 if no sends yet.
    """
    total_resp = (
        supabase.table("email_events")
        .select("id", count="exact")
        .eq("campaign_id", campaign_id)
        .eq("event_type", "sent")
        .execute()
    )
    total: int = total_resp.count or 0
    if total == 0:
        return 0.0

    bounced_resp = (
        supabase.table("email_events")
        .select("id", count="exact")
        .eq("campaign_id", campaign_id)
        .eq("event_type", "bounced")
        .execute()
    )
    bounced: int = bounced_resp.count or 0
    return bounced / total


# ---------------------------------------------------------------------------
# Send window helpers
# ---------------------------------------------------------------------------

def _get_tz(campaign: dict) -> ZoneInfo:
    """Return a ZoneInfo for the campaign timezone, falling back to UTC."""
    try:
        return ZoneInfo(campaign.get("timezone") or "Europe/Amsterdam")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _is_send_day(campaign: dict, tz: ZoneInfo) -> bool:
    """
    Return True if today (in the campaign timezone) is a configured send day.

    send_days is stored as a comma-separated string of ISO weekday numbers:
    1=Monday, 2=Tuesday, … 7=Sunday.
    datetime.isoweekday() returns 1–7 consistently.
    """
    raw = campaign.get("send_days") or "1,2,3,4,5"
    allowed = {int(d.strip()) for d in raw.split(",") if d.strip().isdigit()}
    today_local = datetime.now(tz)
    return today_local.isoweekday() in allowed


def _is_within_send_window(campaign: dict, tz: ZoneInfo) -> bool:
    """
    Return True if the current local time falls within the campaign send window.

    send_window_start and send_window_end are stored as TIME in Supabase
    and come back as 'HH:MM:SS' strings.
    """
    now_local = datetime.now(tz)
    now_str = now_local.strftime("%H:%M")

    start = str(campaign.get("send_window_start") or "08:00")[:5]
    end = str(campaign.get("send_window_end") or "17:00")[:5]

    return start <= now_str <= end


def _next_send_at(
    wait_days: int,
    campaign: dict,
    tz: ZoneInfo,
) -> datetime:
    """
    Calculate the next_send_at timestamp for a lead's subsequent step.

    Adds wait_days to now (UTC), then clamps to the next valid send window
    opening. If the resulting day is not a send day, advance to the next one.
    Returns a UTC datetime.
    """
    candidate = datetime.now(timezone.utc) + timedelta(days=wait_days)
    candidate_local = candidate.astimezone(tz)

    start_str = str(campaign.get("send_window_start") or "08:00")[:5]
    start_h, start_m = int(start_str[:2]), int(start_str[3:5])

    send_days_raw = campaign.get("send_days") or "1,2,3,4,5"
    allowed_days = {int(d.strip()) for d in send_days_raw.split(",") if d.strip().isdigit()}

    # Advance until we land on a valid send day
    for _ in range(14):  # safety cap: max 2 weeks forward
        if candidate_local.isoweekday() in allowed_days:
            break
        candidate_local += timedelta(days=1)

    # Set time to send window start + a random jitter of 0–60 minutes
    jitter_minutes = random.randint(0, 60)
    candidate_local = candidate_local.replace(
        hour=start_h,
        minute=start_m + jitter_minutes if start_m + jitter_minutes < 60 else start_m,
        second=random.randint(0, 59),
        microsecond=0,
    )

    return candidate_local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def _smtp_host(provider: str) -> str:
    """Return the SMTP hostname for the given provider."""
    return SMTP_SERVERS.get((provider or "google").lower(), "smtp.gmail.com")


def _generate_message_id(domain: str) -> str:
    """Generate a unique RFC 2822 Message-ID for this email."""
    unique = uuid.uuid4().hex
    return f"<{unique}@{domain}>"


def send_campaign_email(
    inbox: dict,
    password: str,
    lead: dict,
    subject: str,
    body: str,
    reply_to_message_id: Optional[str],
    is_reply_thread: bool,
    tracking_token: Optional[str] = None,
) -> str:
    """
    Send one campaign email via SMTP SSL and return the Message-ID.

    If is_reply_thread=True and reply_to_message_id is set, the email is
    sent as a reply on the same thread (In-Reply-To + References headers).

    Raises on any SMTP failure so the caller can catch, log, and continue.

    Returns:
        The RFC 2822 Message-ID of the sent email.
    """
    inbox_email: str = inbox["email"]
    provider: str = inbox.get("provider") or "google"
    domain = inbox_email.split("@")[-1]
    message_id = _generate_message_id(domain)

    sender_name = inbox_email.split("@")[0].replace(".", " ").title()
    recipient_name = (lead.get("first_name") or "").strip() or lead["email"]

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{sender_name} <{inbox_email}>"
    msg["To"] = f"{recipient_name} <{lead['email']}>"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=False)

    if is_reply_thread and reply_to_message_id:
        msg["In-Reply-To"] = reply_to_message_id
        msg["References"] = reply_to_message_id

    # Plain text version
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # HTML version with tracking pixel and click tracking
    if tracking_token:
        html_body = "<html><body><p>" + body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        html_body = wrap_links_for_tracking(html_body, tracking_token)
        html_body = inject_tracking_pixel(html_body, tracking_token)
        html_body += "</body></html>"
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    smtp_host = _smtp_host(provider)
    with smtplib.SMTP_SSL(smtp_host, SMTP_PORT) as server:
        server.login(inbox_email, password)
        server.sendmail(inbox_email, [lead["email"]], msg.as_string())

    return message_id


# ---------------------------------------------------------------------------
# Per-lead processing
# ---------------------------------------------------------------------------

def process_lead(
    supabase: Client,
    campaign: dict,
    campaign_lead: dict,
    all_steps: list[dict],
    inbox_credentials: dict[str, str],
) -> bool:
    """
    Process one due campaign_lead: select step, render content, send, log, advance.

    Returns True if the email was sent successfully, False otherwise.
    Exceptions are caught per-lead; failures never abort the campaign loop.
    """
    campaign_id: str = campaign["id"]
    campaign_lead_id: str = campaign_lead["id"]
    lead: dict = campaign_lead.get("lead") or {}
    lead_id: str = lead.get("id") or campaign_lead.get("lead_id") or ""
    current_step: int = campaign_lead.get("current_step") or 1
    thread_message_id: Optional[str] = campaign_lead.get("thread_message_id")
    stop_on_reply: bool = campaign.get("stop_on_reply", True)
    client_id: str = campaign.get("client_id") or ""

    if not lead_id or not lead.get("email"):
        logger.warning("campaign_lead %s has no lead data — skipping.", campaign_lead_id)
        return False

    # ── Timezone-aware sending: skip if outside recipient's business hours ──
    lead_country = (lead.get("country") or "").upper()
    if lead_country and lead_country != "NL":
        _COUNTRY_TZ = {
            "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "LU": "Europe/Luxembourg",
            "DE": "Europe/Berlin", "FR": "Europe/Paris", "GB": "Europe/London",
            "US": "America/New_York", "CA": "America/Toronto",
        }
        recipient_tz_name = _COUNTRY_TZ.get(lead_country)
        if recipient_tz_name:
            try:
                recipient_tz = ZoneInfo(recipient_tz_name)
                recipient_hour = datetime.now(recipient_tz).hour
                if recipient_hour < 8 or recipient_hour >= 18:
                    # Outside recipient business hours — defer to tomorrow 9:00 their time
                    tomorrow_9 = (datetime.now(recipient_tz) + timedelta(days=1)).replace(hour=9, minute=random.randint(0, 30), second=0)
                    tomorrow_utc = tomorrow_9.astimezone(timezone.utc).isoformat()
                    supabase.table("campaign_leads").update({"next_send_at": tomorrow_utc}).eq("id", campaign_lead_id).execute()
                    logger.info("Lead %s: outside recipient hours (%s, %d:00) — deferred to tomorrow.", lead.get("email"), lead_country, recipient_hour)
                    return False
            except Exception:
                pass

    # ── Company-level dedup: max 1 email per company per day ────────────
    lead_domain = (lead.get("domain") or lead.get("email", "").split("@")[-1]).lower()
    if lead_domain and lead_domain not in ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com"):
        try:
            today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
            domain_sends = (
                supabase.table("email_events")
                .select("id", count="exact")
                .eq("campaign_id", campaign_id)
                .eq("event_type", "sent")
                .gte("created_at", today_iso)
                .execute()
            )
            # Check if any sent leads share this domain today
            if domain_sends.data:
                sent_lead_ids = [e.get("lead_id") for e in domain_sends.data if e.get("lead_id")]
                if sent_lead_ids:
                    domain_check = (
                        supabase.table("leads")
                        .select("id")
                        .in_("id", sent_lead_ids[:50])
                        .eq("domain", lead_domain)
                        .limit(1)
                        .execute()
                    )
                    if domain_check.data:
                        logger.info("Lead %s: company domain %s already contacted today — deferring.", lead.get("email"), lead_domain)
                        # Push next_send_at to tomorrow
                        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=8, minute=0).isoformat()
                        supabase.table("campaign_leads").update({"next_send_at": tomorrow}).eq("id", campaign_lead_id).execute()
                        return False
        except Exception as exc:
            logger.debug("Company dedup check failed for %s: %s", lead_domain, exc)

    # ── Check suppression list ───────────────────────────────────────────
    if is_suppressed(supabase, client_id, lead["email"]):
        mark_campaign_lead(supabase, campaign_lead_id, "unsubscribed")
        logger.info("Lead %s is on suppression list — skipping.", lead.get("email"))
        return False

    # ── Check if lead already replied and stop_on_reply is enabled ────────
    if stop_on_reply and has_lead_replied(supabase, campaign_id, lead_id):
        mark_campaign_lead(supabase, campaign_lead_id, "completed")
        mark_lead_status(supabase, lead_id, "replied")
        logger.info("Lead %s already replied — marked completed.", lead.get("email"))
        return False

    # ── Get step rows for current_step ────────────────────────────────────
    step_rows = get_steps_for_number(all_steps, current_step)
    if not step_rows:
        # No more steps — sequence complete
        mark_campaign_lead(supabase, campaign_lead_id, "completed")
        mark_lead_status(supabase, lead_id, "contacted")
        logger.info("Lead %s completed all sequence steps.", lead.get("email"))
        return False

    # ── Select A/B variant ────────────────────────────────────────────────
    step: dict = select_variant(step_rows, lead_id)
    step_id: str = step["id"]
    ab_variant: Optional[str] = step.get("ab_variant")
    wait_days: int = int(step.get("wait_days") or 3)
    is_reply_thread: bool = bool(step.get("is_reply_thread"))
    spintax_enabled: bool = bool(step.get("spintax_enabled", True))

    # ── Evaluate conditional sending ──────────────────────────────────────
    if not check_step_condition(supabase, step, lead_id, campaign_id, current_step):
        skip_to = step.get("condition_skip_to")
        if skip_to:
            # Jump to a later step
            tz = _get_tz(campaign)
            update_campaign_lead_after_send(
                supabase,
                campaign_lead_id=campaign_lead_id,
                next_step=int(skip_to),
                next_send_at=_next_send_at(0, campaign, tz),
                thread_message_id=thread_message_id,
                completed=False,
            )
            logger.info("Step %d skipped for lead %s (condition), jumping to step %d.", current_step, lead.get("email"), skip_to)
        else:
            # Skip the lead entirely
            mark_campaign_lead(supabase, campaign_lead_id, "completed")
            logger.info("Step %d skipped for lead %s (condition failed, no skip_to) — marking complete.", current_step, lead.get("email"))
        return False

    # ── Load client settings (booking_url, sender_name, signature) ────────
    client_settings = load_client_settings(supabase, client_id)

    # ── Render content ────────────────────────────────────────────────────
    raw_subject: str = step.get("subject") or ""
    raw_body: str = step.get("body") or ""

    subject = process_content(raw_subject, lead, step_number=current_step, spintax_enabled=spintax_enabled, client_settings=client_settings)
    body = process_content(raw_body, lead, step_number=current_step, spintax_enabled=spintax_enabled, client_settings=client_settings)

    if not subject.strip() or not body.strip():
        logger.error(
            "Step %s for lead %s produced empty subject or body after rendering.",
            step_id, lead.get("email"),
        )
        return False

    # ── Append unsubscribe link ──────────────────────────────────────────
    try:
        unsub_url = generate_unsubscribe_link(supabase, client_id, lead_id, lead["email"], campaign_id)
        body = append_unsubscribe_footer(body, unsub_url)
    except Exception as exc:
        logger.warning("Could not generate unsubscribe link for %s: %s", lead.get("email"), exc)

    # ── Select inbox (rotate: avoid same inbox as previous step) ─────────
    previous_inbox_id = campaign_lead.get("last_inbox_id")
    exclude = [previous_inbox_id] if previous_inbox_id else None
    inbox = select_inbox(supabase, client_id, exclude_inbox_ids=exclude)
    if inbox is None and exclude:
        # Fallback: allow same inbox if no other is available
        inbox = select_inbox(supabase, client_id)
    if inbox is None:
        logger.warning("No eligible inbox available for client %s — deferring lead %s.", client_id, lead.get("email"))
        return False

    inbox_id: str = inbox["id"]
    inbox_email: str = inbox["email"]
    password = inbox_credentials.get(inbox_email.lower())
    if not password:
        logger.error("No SMTP password found in env for inbox %s — skipping.", inbox_email)
        return False

    # ── Generate tracking token ──────────────────────────────────────────
    tracking_token = _make_tracking_token(client_id, campaign_id, lead_id, lead.get("email", ""))

    # ── Send ──────────────────────────────────────────────────────────────
    try:
        message_id = send_campaign_email(
            inbox=inbox,
            password=password,
            lead=lead,
            subject=subject,
            body=body,
            reply_to_message_id=thread_message_id,
            is_reply_thread=is_reply_thread,
            tracking_token=tracking_token,
        )
    except Exception as exc:
        logger.error("SMTP send failed for lead %s via %s: %s", lead.get("email"), inbox_email, exc)
        log_email_event(
            supabase, campaign_id, lead_id, step_id, inbox_id, "bounced",
            ab_variant=ab_variant, notes=str(exc),
        )
        return False

    logger.info(
        "Sent step %d to %s via %s (variant=%s) | subject: %s",
        current_step, lead.get("email"), inbox_email, ab_variant or "—", subject,
    )

    # ── Log success ───────────────────────────────────────────────────────
    log_email_event(
        supabase, campaign_id, lead_id, step_id, inbox_id, "sent",
        ab_variant=ab_variant, message_id=message_id,
    )

    # ── Determine next state ──────────────────────────────────────────────
    next_step = current_step + 1
    has_more_steps = bool(get_steps_for_number(all_steps, next_step))
    tz = _get_tz(campaign)

    if not has_more_steps:
        # Last step sent — complete the lead
        update_campaign_lead_after_send(
            supabase,
            campaign_lead_id=campaign_lead_id,
            next_step=next_step,
            next_send_at=datetime.now(timezone.utc),
            thread_message_id=message_id if current_step == 1 else thread_message_id,
            completed=True,
            last_inbox_id=inbox_id,
        )
        mark_lead_status(supabase, lead_id, "contacted")
        # Funnel: sequence done without reply → nurture
        try:
            from funnel_engine import on_sequence_complete
            on_sequence_complete(supabase, lead_id)
        except Exception:
            pass
    else:
        next_send = _next_send_at(wait_days, campaign, tz)
        update_campaign_lead_after_send(
            supabase,
            campaign_lead_id=campaign_lead_id,
            next_step=next_step,
            next_send_at=next_send,
            thread_message_id=message_id if current_step == 1 else thread_message_id,
            completed=False,
            last_inbox_id=inbox_id,
        )
        # Funnel: check stage progression based on engagement
        try:
            from funnel_engine import check_stage_progression
            check_stage_progression(supabase, lead_id, current_step, len(all_steps))
        except Exception:
            pass

    # Short delay between sends within the same run to avoid burst patterns
    time.sleep(random.uniform(MIN_SEND_DELAY, MAX_SEND_DELAY))

    return True


# ---------------------------------------------------------------------------
# Per-campaign processing
# ---------------------------------------------------------------------------

def process_campaign(
    supabase: Client,
    campaign: dict,
    inbox_credentials: dict[str, str],
) -> None:
    """
    Process all due leads for one campaign in a single scheduler run.

    Respects daily_limit: stops sending once the campaign's daily quota is reached.
    Checks bounce_threshold after all sends and auto-pauses if exceeded.
    """
    campaign_id: str = campaign["id"]
    campaign_name: str = campaign.get("name") or campaign_id
    daily_limit: int = int(campaign.get("daily_limit") or 50)
    bounce_threshold: float = float(campaign.get("bounce_threshold") or 0.03)
    client_id: str = campaign.get("client_id") or ""

    tz = _get_tz(campaign)

    # Validate send window
    if not _is_send_day(campaign, tz):
        logger.info("Campaign '%s': today is not a send day — skipping.", campaign_name)
        return
    if not _is_within_send_window(campaign, tz):
        logger.info("Campaign '%s': outside send window — skipping.", campaign_name)
        return

    # How many have we already sent today?
    sent_today = count_campaign_sends_today(supabase, campaign_id)
    remaining_today = daily_limit - sent_today
    if remaining_today <= 0:
        logger.info("Campaign '%s': daily limit (%d) reached — skipping.", campaign_name, daily_limit)
        return

    # Load steps once for this campaign (shared across all leads this run)
    all_steps = load_sequence_steps(supabase, campaign_id)
    if not all_steps:
        logger.warning("Campaign '%s': no sequence steps configured — skipping.", campaign_name)
        return

    # Load due leads
    due_leads = load_due_campaign_leads(supabase, campaign_id)
    if not due_leads:
        logger.info("Campaign '%s': no leads due for sending.", campaign_name)
        return

    logger.info(
        "Campaign '%s': %d lead(s) due, %d remaining today (limit %d).",
        campaign_name, len(due_leads), remaining_today, daily_limit,
    )

    sends_this_run = 0

    for campaign_lead in due_leads:
        if sends_this_run >= remaining_today:
            logger.info("Campaign '%s': hit daily limit during this run.", campaign_name)
            break

        try:
            sent = process_lead(
                supabase=supabase,
                campaign=campaign,
                campaign_lead=campaign_lead,
                all_steps=all_steps,
                inbox_credentials=inbox_credentials,
            )
            if sent:
                sends_this_run += 1
        except Exception as exc:
            lead_email = (campaign_lead.get("lead") or {}).get("email", "unknown")
            logger.error("Unhandled error processing lead %s in campaign '%s': %s", lead_email, campaign_name, exc)

    logger.info("Campaign '%s': sent %d email(s) this run.", campaign_name, sends_this_run)

    # ── Bounce rate check ─────────────────────────────────────────────────
    if sends_this_run > 0:
        bounce_rate = calculate_bounce_rate(supabase, campaign_id)
        if bounce_rate > bounce_threshold:
            pause_campaign(
                supabase,
                campaign_id,
                reason=(
                    f"Bounce rate {bounce_rate:.1%} exceeds threshold {bounce_threshold:.1%}. "
                    "Investigate and resume manually."
                ),
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Main entry point for the campaign scheduler.

    Loads all active campaigns and processes each one sequentially.
    Each campaign's errors are contained; one bad campaign never stops others.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Aborting.")
        return

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    inbox_credentials = load_inbox_credentials()

    campaigns = load_active_campaigns(supabase)

    if not campaigns:
        logger.info("No active campaigns found.")
        return

    logger.info("Processing %d active campaign(s).", len(campaigns))

    for campaign in campaigns:
        try:
            process_campaign(supabase, campaign, inbox_credentials)
        except Exception as exc:
            logger.error(
                "Unhandled error in campaign '%s': %s",
                campaign.get("name") or campaign.get("id"),
                exc,
            )

    logger.info("Campaign scheduler run complete.")


if __name__ == "__main__":
    main()
