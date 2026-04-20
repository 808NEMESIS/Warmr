"""
bounce_handler.py — Process bounces and spam complaints.

Inspects the IMAP INBOX of each client inbox for bounce / delivery-failure
messages (DSN, SMTP rejections, mailer-daemon replies) and spam-complaint
feedback loops. For each one:

  - Classifies as hard | soft | spam_complaint
  - Logs to bounce_log
  - Updates reputation_score (hard -5, soft -2, complaint -20)
  - Marks lead/campaign_lead as bounced or unsubscribed
  - Retires the sending inbox if bounce rate > 3% in last 7 days
  - 3-strike soft-bounce retry: after 3 soft bounces we treat as hard

Called every 30 min via launchd / cron. Idempotent: a bounce message is only
processed once (tracked via IMAP Message-ID in the bounce_log).

Safety: never raises. Each inbox processed in isolation. Logs everything.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

IMAP_HOSTS = {
    "google": "imap.gmail.com",
    "microsoft": "outlook.office365.com",
}
IMAP_PORT = 993

BOUNCE_RATE_THRESHOLD = 0.03   # 3% in 7 days → pause inbox
SOFT_BOUNCE_MAX_RETRIES = 3

# Reputation deltas per CLAUDE.md
REPUTATION_DELTA = {
    "hard": -5.0,
    "soft": -2.0,
    "spam_complaint": -20.0,
}

# Sender addresses that produce bounce messages
BOUNCE_SENDERS = (
    "mailer-daemon@",
    "postmaster@",
    "<>",
)

# Subject markers that indicate a bounce
BOUNCE_SUBJECT_MARKERS = (
    "delivery status notification",
    "mail delivery failed",
    "undelivered mail",
    "returned mail",
    "undeliverable",
    "failure notice",
    "delivery failure",
)

# SMTP permanent failure codes → hard bounce
HARD_BOUNCE_CODES = re.compile(r"\b5\.\d\.\d\b|\b55[0-9]\s")
# SMTP transient failure codes → soft bounce
SOFT_BOUNCE_CODES = re.compile(r"\b4\.\d\.\d\b|\b45[0-9]\s")
# Spam complaint via feedback loop (ARF format)
SPAM_FEEDBACK_MARKERS = ("abuse report", "feedback report", "x-arf:")

# Extract the original recipient from a DSN
FINAL_RECIPIENT_RE = re.compile(r"Final-Recipient:[^;]*;\s*([^\s]+)", re.IGNORECASE)
ORIG_RCPT_RE = re.compile(r"Original-Recipient:[^;]*;\s*([^\s]+)", re.IGNORECASE)
TO_IN_BODY_RE = re.compile(r"<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)>")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Credentials loaders (mirrors imap_processor.py style)
# ---------------------------------------------------------------------------

def load_inbox_passwords() -> dict[str, str]:
    out: dict[str, str] = {}
    i = 1
    while True:
        email = os.getenv(f"INBOX_{i}_EMAIL")
        if not email:
            break
        pw = os.getenv(f"INBOX_{i}_PASSWORD", "")
        if pw:
            out[email.lower()] = pw
        i += 1
    return out


def load_active_inboxes(sb: Client) -> list[dict]:
    resp = sb.table("inboxes").select("*").neq("status", "retired").execute()
    return resp.data or []


# ---------------------------------------------------------------------------
# Bounce classification
# ---------------------------------------------------------------------------

def is_bounce_message(parsed) -> bool:
    """Return True if this looks like a DSN / delivery-failure notification."""
    sender = (parsed.get("From", "") or "").lower()
    for s in BOUNCE_SENDERS:
        if s in sender:
            return True
    subject = (parsed.get("Subject", "") or "").lower()
    for m in BOUNCE_SUBJECT_MARKERS:
        if m in subject:
            return True
    # Content-Type: multipart/report is the DSN standard
    ct = (parsed.get("Content-Type", "") or "").lower()
    if "multipart/report" in ct and "delivery-status" in ct:
        return True
    return False


def is_spam_complaint(parsed) -> bool:
    """Detect ARF feedback-loop messages (Gmail, Yahoo, etc.)."""
    ct = (parsed.get("Content-Type", "") or "").lower()
    if "feedback-report" in ct:
        return True
    subject = (parsed.get("Subject", "") or "").lower()
    return any(m in subject for m in SPAM_FEEDBACK_MARKERS)


def classify_bounce(body: str) -> str:
    """Return 'hard' | 'soft' based on SMTP codes in the DSN body."""
    if HARD_BOUNCE_CODES.search(body):
        return "hard"
    if SOFT_BOUNCE_CODES.search(body):
        return "soft"
    # Heuristic fallback on common phrases
    bl = body.lower()
    if any(p in bl for p in ("user unknown", "mailbox unavailable", "address rejected", "does not exist", "no such user")):
        return "hard"
    if any(p in bl for p in ("mailbox full", "temporarily", "try again", "deferred")):
        return "soft"
    return "hard"  # When in doubt, treat as hard to protect reputation


def extract_original_recipient(parsed, raw_body: str) -> Optional[str]:
    """Pull the original recipient email from a DSN."""
    for part in parsed.walk() if parsed.is_multipart() else [parsed]:
        # message/delivery-status parts don't decode via get_payload(decode=True)
        # because Python's email lib treats them as container-messages. Use
        # as_string() to get the raw text representation instead.
        ct = (part.get_content_type() or "").lower()
        if ct == "message/delivery-status":
            txt = part.as_string()
        else:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                txt = payload.decode("utf-8", errors="replace")
        for regex in (FINAL_RECIPIENT_RE, ORIG_RCPT_RE):
            m = regex.search(txt)
            if m:
                addr = m.group(1).strip().strip("<>").lower()
                if "@" in addr:
                    return addr
    # Fallback: any <email@…> in the body
    m = TO_IN_BODY_RE.search(raw_body)
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def already_processed(sb: Client, message_id: str) -> bool:
    """Skip bounces we've already recorded."""
    if not message_id:
        return False
    try:
        resp = (
            sb.table("bounce_log")
            .select("id")
            .eq("raw_response", f"MID:{message_id}")
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception:
        return False


def count_soft_bounces(sb: Client, inbox_id: str, lead_email: str) -> int:
    """Return how many soft bounces we already logged for this recipient from this inbox."""
    try:
        resp = (
            sb.table("bounce_log")
            .select("id", count="exact")
            .eq("inbox_id", inbox_id)
            .eq("lead_email", lead_email)
            .eq("bounce_type", "soft")
            .execute()
        )
        return resp.count or 0
    except Exception:
        return 0


def log_bounce(
    sb: Client,
    inbox_id: str,
    lead_email: str,
    bounce_type: str,
    message_id: str,
    raw_body_snippet: str,
    soft_count: int = 0,
) -> None:
    try:
        sb.table("bounce_log").insert({
            "inbox_id": inbox_id,
            "lead_email": lead_email,
            "bounce_type": bounce_type,
            "raw_response": f"MID:{message_id}\n{raw_body_snippet[:1500]}",
            "soft_bounce_count": soft_count,
            "resolved": False,
            "timestamp": _now(),
        }).execute()
    except Exception as exc:
        logger.error("Failed to write bounce_log for %s: %s", lead_email, exc)


def apply_reputation_delta(sb: Client, inbox_id: str, bounce_type: str) -> None:
    """Adjust reputation_score on the inbox. Floor at 0, cap at 100."""
    delta = REPUTATION_DELTA.get(bounce_type, 0)
    if not delta:
        return
    try:
        row = sb.table("inboxes").select("reputation_score, spam_complaints").eq("id", inbox_id).limit(1).execute()
        current = float((row.data or [{}])[0].get("reputation_score") or 50)
        new = max(0.0, min(100.0, current + delta))
        update: dict = {"reputation_score": round(new, 2)}
        if bounce_type == "spam_complaint":
            complaints = int((row.data or [{}])[0].get("spam_complaints") or 0) + 1
            update["spam_complaints"] = complaints
            update["last_spam_incident"] = _now()
        sb.table("inboxes").update(update).eq("id", inbox_id).execute()
    except Exception as exc:
        logger.error("Failed to apply reputation delta for inbox %s: %s", inbox_id, exc)


def mark_lead_and_campaign_bounced(sb: Client, lead_email: str, bounce_type: str) -> None:
    """When a lead hard-bounces or their mailbox unsubscribes, stop sending to them."""
    try:
        lead_resp = sb.table("leads").select("id, client_id").eq("email", lead_email).limit(1).execute()
        if not lead_resp.data:
            return
        lead_id = lead_resp.data[0]["id"]

        # Update lead.status
        new_status = "bounced" if bounce_type in ("hard", "spam_complaint") else "soft_bounced"
        sb.table("leads").update({"status": new_status}).eq("id", lead_id).execute()

        # Halt active campaign_leads for this lead
        if bounce_type in ("hard", "spam_complaint"):
            sb.table("campaign_leads").update({
                "status": "bounced",
            }).eq("lead_id", lead_id).in_("status", ["active", "pending"]).execute()
    except Exception as exc:
        logger.error("Failed to mark lead %s as %s: %s", lead_email, bounce_type, exc)


def check_inbox_bounce_rate(sb: Client, inbox_id: str) -> None:
    """If the inbox's 7-day bounce rate > 3%, pause it and notify."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        sent = (
            sb.table("email_events")
            .select("id", count="exact")
            .eq("inbox_id", inbox_id)
            .eq("event_type", "sent")
            .gte("created_at", since)
            .execute()
        )
        bounced = (
            sb.table("email_events")
            .select("id", count="exact")
            .eq("inbox_id", inbox_id)
            .eq("event_type", "bounced")
            .gte("created_at", since)
            .execute()
        )
        sent_count = sent.count or 0
        bounce_count = bounced.count or 0
        if sent_count < 30:
            return  # Not enough sample size
        rate = bounce_count / sent_count
        if rate > BOUNCE_RATE_THRESHOLD:
            inbox_row = sb.table("inboxes").select("email, client_id, status").eq("id", inbox_id).limit(1).execute()
            email = (inbox_row.data or [{}])[0].get("email", inbox_id)
            client_id = (inbox_row.data or [{}])[0].get("client_id", "")
            status = (inbox_row.data or [{}])[0].get("status", "")
            if status == "paused":
                return
            sb.table("inboxes").update({
                "status": "paused",
                "warmup_active": False,
                "notes": f"Auto-paused {datetime.now(timezone.utc).date().isoformat()}: bounce rate {rate:.1%} > {BOUNCE_RATE_THRESHOLD:.0%}",
            }).eq("id", inbox_id).execute()
            try:
                sb.table("notifications").insert({
                    "client_id": client_id,
                    "type": "inbox_bounce_pause",
                    "entity_id": inbox_id,
                    "entity_type": "inbox",
                    "message": f"Inbox {email} is gepauzeerd: bounce rate {rate:.1%} overschrijdt de 3% limiet (7 dagen).",
                    "priority": "urgent",
                }).execute()
            except Exception:
                pass
            logger.warning("Auto-paused inbox %s (bounce rate %.1f%%)", email, rate * 100)
    except Exception as exc:
        logger.error("Bounce-rate check failed for inbox %s: %s", inbox_id, exc)


# ---------------------------------------------------------------------------
# Per-inbox processing
# ---------------------------------------------------------------------------

def process_inbox(inbox: dict, password: str, sb: Client) -> int:
    """Process bounces/complaints in one client inbox. Returns count processed."""
    inbox_email = inbox["email"]
    inbox_id = inbox["id"]
    provider = (inbox.get("provider") or "google").lower()
    host = IMAP_HOSTS.get(provider, "imap.gmail.com")
    processed = 0

    try:
        mail = imaplib.IMAP4_SSL(host, IMAP_PORT)
        mail.login(inbox_email, password)
    except Exception as exc:
        logger.error("Bounce handler: IMAP login failed for %s: %s", inbox_email, exc)
        return 0

    try:
        mail.select("INBOX")
        # Search last 24 hours of unseen mail FROM mailer-daemon/postmaster
        since_date = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%d-%b-%Y")
        queries = [
            f'(SINCE {since_date} FROM "mailer-daemon")',
            f'(SINCE {since_date} FROM "postmaster")',
            f'(SINCE {since_date} SUBJECT "delivery status")',
            f'(SINCE {since_date} SUBJECT "undelivered")',
            f'(SINCE {since_date} SUBJECT "abuse report")',
        ]
        seen_ids: set[bytes] = set()
        for q in queries:
            try:
                status, data = mail.search(None, q)
                if status == "OK" and data and data[0]:
                    for mid in data[0].split():
                        seen_ids.add(mid)
            except Exception as exc:
                logger.debug("Search %s failed on %s: %s", q, inbox_email, exc)

        for mid in seen_ids:
            try:
                status, msg_data = mail.fetch(mid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
                parsed = email_lib.message_from_bytes(raw)

                message_id = (parsed.get("Message-ID", "") or "").strip()
                if already_processed(sb, message_id):
                    continue

                is_complaint = is_spam_complaint(parsed)
                is_bounce = is_bounce_message(parsed)
                if not (is_complaint or is_bounce):
                    continue

                raw_body = raw.decode("utf-8", errors="replace")
                recipient = extract_original_recipient(parsed, raw_body)
                if not recipient:
                    logger.debug("Could not extract recipient from bounce on %s — skipping", inbox_email)
                    continue

                if is_complaint:
                    bounce_type = "spam_complaint"
                else:
                    bounce_type = classify_bounce(raw_body)
                    # 3-strike: promote soft to hard after N strikes
                    if bounce_type == "soft":
                        prev = count_soft_bounces(sb, inbox_id, recipient)
                        if prev + 1 >= SOFT_BOUNCE_MAX_RETRIES:
                            bounce_type = "hard"
                        soft_count = prev + 1
                    else:
                        soft_count = 0

                log_bounce(
                    sb,
                    inbox_id=inbox_id,
                    lead_email=recipient,
                    bounce_type=bounce_type,
                    message_id=message_id,
                    raw_body_snippet=raw_body[:2000],
                    soft_count=soft_count if bounce_type == "soft" else 0,
                )

                apply_reputation_delta(sb, inbox_id, bounce_type)
                mark_lead_and_campaign_bounced(sb, recipient, bounce_type)

                # Also write an email_events row so analytics + circuit breakers see it
                try:
                    sb.table("email_events").insert({
                        "inbox_id": inbox_id,
                        "client_id": inbox.get("client_id"),
                        "lead_email": recipient,
                        "event_type": "bounced",
                        "created_at": _now(),
                    }).execute()
                except Exception:
                    pass

                # Mark the bounce message as read so we don't reprocess
                try:
                    mail.store(mid, "+FLAGS", "\\Seen")
                except Exception:
                    pass

                processed += 1
                logger.info(
                    "Bounce: %s → %s (%s) via %s",
                    bounce_type, recipient, message_id[:40], inbox_email,
                )

            except Exception as exc:
                logger.warning("Error processing bounce message on %s: %s", inbox_email, exc)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    # After scanning, re-check the 7-day bounce rate
    if processed > 0:
        check_inbox_bounce_rate(sb, inbox_id)

    return processed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL/KEY not set.")
        return 1

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    passwords = load_inbox_passwords()
    inboxes = load_active_inboxes(sb)

    total = 0
    for inbox in inboxes:
        pw = passwords.get(inbox["email"].lower())
        if not pw:
            logger.debug("No password for %s — skipping bounce scan.", inbox["email"])
            continue
        try:
            total += process_inbox(inbox, pw, sb)
        except Exception as exc:
            logger.error("Unhandled error on inbox %s: %s", inbox["email"], exc)

    logger.info("Bounce handler complete. Processed %d new bounce(s).", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
