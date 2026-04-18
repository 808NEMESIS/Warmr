"""
imap_processor.py

Processes all inboxes (client inboxes + warmup network accounts) via IMAP SSL.
Called by the n8n warm-up-receiver workflow every 10 minutes.

Two distinct processing paths:

1. CLIENT INBOXES (from Supabase):
   - Connect via IMAP SSL
   - Find and rescue all emails in the spam/junk folder → move to inbox
   - Log spam_rescued events to warmup_logs

2. WARMUP NETWORK ACCOUNTS (from env vars):
   - Connect via IMAP SSL
   - Find unread emails FROM any client inbox
   - Mark as read (simulates an open)
   - With REPLY_RATE probability: generate a reply via Claude Haiku and send it
   - Log received / replied events to warmup_logs against the originating client inbox
   - Update reputation_score for the originating inbox
"""
from __future__ import annotations

import email as email_lib
import imaplib
import logging
import os
import random
import smtplib
import time
from datetime import datetime
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import anthropic
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
WARMUP_LANGUAGE: str = os.getenv("WARMUP_LANGUAGE", "nl")
REPLY_RATE: float = float(os.getenv("REPLY_RATE", "0.35"))

IMAP_PORT: int = 993  # SSL
SMTP_PORT: int = 465  # SSL

# Reputation score deltas per event (from CLAUDE.md)
REPUTATION_DELTA: dict[str, float] = {
    "sent": 0.2,
    "received": 0.5,
    "spam_rescued": 1.0,
    "opened": 0.3,
    "soft_bounce": -2.0,
    "hard_bounce": -5.0,
    "spam_complaint": -20.0,
}

# Candidate spam/junk folder names across providers
SPAM_FOLDER_CANDIDATES: list[str] = [
    "[Gmail]/Spam",
    "Spam",
    "SPAM",
    "Junk",
    "Junk Email",
    "Junk E-mail",
    "[Gmail]/Junk",
    "Bulk Mail",
]

# IMAP / SMTP server hostnames per provider
IMAP_SERVERS: dict[str, str] = {
    "google": "imap.gmail.com",
    "microsoft": "outlook.office365.com",
}
SMTP_SERVERS: dict[str, str] = {
    "google": "smtp.gmail.com",
    "microsoft": "smtp.office365.com",
}


# ---------------------------------------------------------------------------
# Server resolution
# ---------------------------------------------------------------------------

def get_imap_server(provider: str) -> str:
    """Return IMAP hostname for the given provider. Defaults to Gmail."""
    return IMAP_SERVERS.get((provider or "google").lower(), "imap.gmail.com")


def get_smtp_server(provider: str) -> str:
    """Return SMTP hostname for the given provider. Defaults to Gmail."""
    return SMTP_SERVERS.get((provider or "google").lower(), "smtp.gmail.com")


# ---------------------------------------------------------------------------
# Env loading helpers
# ---------------------------------------------------------------------------

def load_warmup_network() -> list[dict]:
    """
    Load all warmup network accounts from WARMUP_NETWORK_*_EMAIL / _PASSWORD env vars.

    Returns a list of dicts with keys: email, password.
    Provider is always 'google' for warmup network accounts (Gmail only).
    """
    accounts: list[dict] = []
    i = 1
    while True:
        email = os.getenv(f"WARMUP_NETWORK_{i}_EMAIL")
        if not email:
            break
        password = os.getenv(f"WARMUP_NETWORK_{i}_PASSWORD", "")
        accounts.append({"email": email.strip(), "password": password.strip(), "provider": "google"})
        i += 1
    logger.info("Loaded %d warmup network accounts.", len(accounts))
    return accounts


def load_client_inbox_credentials() -> dict[str, str]:
    """
    Build a mapping of inbox email → password from INBOX_*_EMAIL / _PASSWORD env vars.

    Used to inject passwords into inbox rows fetched from Supabase (passwords are
    never stored in the database).
    """
    creds: dict[str, str] = {}
    i = 1
    while True:
        email = os.getenv(f"INBOX_{i}_EMAIL")
        if not email:
            break
        password = os.getenv(f"INBOX_{i}_PASSWORD", "")
        creds[email.strip().lower()] = password.strip()
        i += 1
    return creds


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def load_active_inboxes(supabase: Client) -> list[dict]:
    """
    Fetch all client inboxes where warmup_active=true and status != 'retired'.
    """
    response = (
        supabase.table("inboxes")
        .select("*")
        .eq("warmup_active", True)
        .neq("status", "retired")
        .execute()
    )
    return response.data or []


def get_inbox_by_email(supabase: Client, email_address: str) -> Optional[dict]:
    """
    Look up a client inbox row by email address.

    Used when processing warmup network inboxes: we identify which client inbox
    sent a given email so we can log against the correct inbox_id.
    """
    response = (
        supabase.table("inboxes")
        .select("*")
        .eq("email", email_address.lower())
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def log_action(
    supabase: Client,
    inbox_id: str,
    action: str,
    reputation_score: float = 0.0,
    warmup_week: int = 1,
    daily_volume: int = 0,
    counterpart_email: str = "",
    subject: str = "",
    landed_in_spam: bool = False,
    was_rescued: bool = False,
    was_replied: bool = False,
    notes: str = "",
) -> None:
    """
    Insert one event row into warmup_logs.

    Never raises — failures are logged to stderr and swallowed so other
    inboxes continue processing.
    """
    try:
        supabase.table("warmup_logs").insert(
            {
                "inbox_id": inbox_id,
                "action": action,
                "counterpart_email": counterpart_email,
                "subject": subject,
                "warmup_week": warmup_week,
                "daily_volume": daily_volume,
                "reputation_score_at_time": reputation_score,
                "landed_in_spam": landed_in_spam,
                "was_rescued": was_rescued,
                "was_replied": was_replied,
                "notes": notes,
            }
        ).execute()
    except Exception as exc:
        logger.error("Failed to write warmup_log for inbox %s action=%s: %s", inbox_id, action, exc)


def update_reputation(
    supabase: Client,
    inbox_id: str,
    current_score: float,
    events: dict[str, int],
) -> float:
    """
    Apply reputation delta for each event type and persist the new score.

    Args:
        inbox_id: UUID of the client inbox.
        current_score: Current reputation_score value from the DB row.
        events: Dict mapping event name → count, e.g. {"received": 2, "spam_rescued": 1}.

    Returns:
        The updated reputation score after clamping to [0, 100].
    """
    delta = sum(REPUTATION_DELTA.get(event, 0.0) * count for event, count in events.items())
    new_score = max(0.0, min(100.0, current_score + delta))

    try:
        supabase.table("inboxes").update(
            {"reputation_score": round(new_score, 2), "updated_at": datetime.utcnow().isoformat()}
        ).eq("id", inbox_id).execute()
    except Exception as exc:
        logger.error("Failed to update reputation for inbox %s: %s", inbox_id, exc)

    return new_score


def increment_spam_rescues(supabase: Client, inbox_id: str, count: int) -> None:
    """Increment the lifetime spam_rescues counter on an inbox by count."""
    if count <= 0:
        return
    try:
        # Fetch current value then add — supabase-py doesn't support SQL increment directly
        resp = supabase.table("inboxes").select("spam_rescues").eq("id", inbox_id).single().execute()
        current = (resp.data or {}).get("spam_rescues") or 0
        supabase.table("inboxes").update(
            {"spam_rescues": current + count, "updated_at": datetime.utcnow().isoformat()}
        ).eq("id", inbox_id).execute()
    except Exception as exc:
        logger.error("Failed to increment spam_rescues for inbox %s: %s", inbox_id, exc)


# ---------------------------------------------------------------------------
# IMAP utility functions
# ---------------------------------------------------------------------------

def decode_header_value(raw: str) -> str:
    """
    Decode a potentially encoded email header value to a plain string.

    Handles RFC 2047 encoded-words (=?utf-8?b?...?=, =?iso-8859-1?q?...?=, etc.).
    """
    parts = decode_header(raw or "")
    decoded_parts: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_parts.append(str(part))
    return "".join(decoded_parts)


def find_spam_folder(mail: imaplib.IMAP4_SSL) -> Optional[str]:
    """
    Detect the spam/junk folder name by listing all IMAP folders.

    Different providers use different names: Gmail uses '[Gmail]/Spam',
    Outlook uses 'Junk Email', etc. Returns the first candidate that exists,
    or None if none is found.
    """
    try:
        status, folders = mail.list()
        if status != "OK":
            return None
        folder_names: list[str] = []
        for folder_bytes in (folders or []):
            if folder_bytes:
                # Folder list response format: (\Flags) "delimiter" "Name"
                parts = folder_bytes.decode("utf-8", errors="replace").split('"')
                if len(parts) >= 3:
                    folder_names.append(parts[-2].strip())
                elif len(parts) == 1:
                    # Unquoted folder name
                    folder_names.append(parts[0].split()[-1].strip())

        for candidate in SPAM_FOLDER_CANDIDATES:
            if candidate in folder_names:
                return candidate
            # Case-insensitive fallback
            for name in folder_names:
                if name.lower() == candidate.lower():
                    return name

    except Exception as exc:
        logger.error("Error listing IMAP folders: %s", exc)

    return None


def get_email_body(msg: email_lib.message.Message) -> str:
    """
    Extract the plain-text body from an email.Message object.

    Walks multipart messages and returns the first text/plain part.
    Falls back to the full payload decoded as UTF-8 if no text/plain part is found.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace") if payload else ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""
    return ""


def extract_sender_address(msg: email_lib.message.Message) -> str:
    """
    Extract just the email address from the From header (strips display name).

    e.g. 'John Doe <john@example.com>' → 'john@example.com'
    """
    from_raw = msg.get("From", "")
    if "<" in from_raw and ">" in from_raw:
        return from_raw.split("<")[1].split(">")[0].strip().lower()
    return from_raw.strip().lower()


# ---------------------------------------------------------------------------
# Claude Haiku — reply generation
# ---------------------------------------------------------------------------

def generate_reply(
    claude_client: anthropic.Anthropic,
    original_body: str,
    sender_name: str,
    recipient_name: str,
    supabase_client=None,
    client_id: str | None = None,
) -> str:
    """
    Generate a short, natural reply to a warmup email via Claude Haiku.

    The reply is in WARMUP_LANGUAGE and sounds like a real person responding.
    Returns only the reply body text (no subject line).

    If supabase_client is provided, uses tracked_claude_call for cost tracking.
    """
    prompt = (
        f"You are {recipient_name}. You received the following business email:\n\n"
        f"---\n{original_body[:800]}\n---\n\n"
        f"Write a brief, natural reply in {WARMUP_LANGUAGE} (40–80 words) from {recipient_name} to {sender_name}.\n"
        "Sound like a real person. No marketing language. No formal template phrases.\n"
        "Return only the reply body — no subject line, no metadata."
    )

    messages = [{"role": "user", "content": prompt}]

    if supabase_client:
        from utils.cost_tracker import tracked_claude_call
        message = tracked_claude_call(
            claude_client, supabase_client,
            model="claude-haiku-4-5-20251001",
            messages=messages,
            max_tokens=200,
            context="reply_generation",
            client_id=client_id,
        )
    else:
        message = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=messages,
        )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# SMTP send helper
# ---------------------------------------------------------------------------

def send_reply_via_smtp(
    smtp_host: str,
    sender_email: str,
    sender_password: str,
    sender_name: str,
    recipient_email: str,
    original_subject: str,
    reply_body: str,
) -> None:
    """
    Send a reply email via SMTP SSL on port 465.

    Prefixes 'Re: ' to the original subject if not already present.
    Raises on SMTP failure so the caller can catch and log.
    """
    subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = recipient_email
    msg["Subject"] = subject
    msg.attach(MIMEText(reply_body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(smtp_host, SMTP_PORT) as server:
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, [recipient_email], msg.as_string())


# ---------------------------------------------------------------------------
# Path 1: Client inbox spam rescue
# ---------------------------------------------------------------------------

def rescue_spam_for_inbox(
    inbox: dict,
    password: str,
    supabase: Client,
) -> int:
    """
    Connect to a client inbox via IMAP, find the spam/junk folder, and move
    all emails back to the inbox.

    Returns the number of emails rescued (0 if none or folder not found).
    Each rescued email is logged to warmup_logs and counts towards reputation.
    """
    inbox_id: str = inbox["id"]
    inbox_email: str = inbox["email"]
    provider: str = inbox.get("provider") or "google"
    reputation_score: float = inbox.get("reputation_score") or 50.0
    imap_host = get_imap_server(provider)
    rescued_count = 0

    try:
        mail = imaplib.IMAP4_SSL(imap_host, IMAP_PORT)
        mail.login(inbox_email, password)
    except Exception as exc:
        logger.error("Inbox %s: IMAP login failed: %s", inbox_email, exc)
        log_action(supabase, inbox_id, "error", reputation_score=reputation_score, notes=f"IMAP login failed: {exc}")
        return 0

    try:
        spam_folder = find_spam_folder(mail)
        if not spam_folder:
            logger.info("Inbox %s: no spam folder found — skipping rescue.", inbox_email)
            mail.logout()
            return 0

        status, _ = mail.select(f'"{spam_folder}"')
        if status != "OK":
            logger.warning("Inbox %s: could not select spam folder '%s'.", inbox_email, spam_folder)
            mail.logout()
            return 0

        status, message_ids = mail.search(None, "ALL")
        if status != "OK" or not message_ids or not message_ids[0]:
            mail.logout()
            return 0

        ids = message_ids[0].split()
        if not ids:
            mail.logout()
            return 0

        logger.info("Inbox %s: found %d email(s) in spam — rescuing.", inbox_email, len(ids))

        for msg_id in ids:
            try:
                # Fetch the message to get metadata for logging
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                subject = ""
                counterpart_email = ""
                if status == "OK" and msg_data and msg_data[0]:
                    raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
                    parsed = email_lib.message_from_bytes(raw)
                    subject = decode_header_value(parsed.get("Subject", ""))
                    counterpart_email = extract_sender_address(parsed)

                # Copy to INBOX, then delete from spam
                mail.copy(msg_id, "INBOX")
                mail.store(msg_id, "+FLAGS", "\\Deleted")
                rescued_count += 1

                log_action(
                    supabase,
                    inbox_id=inbox_id,
                    action="spam_rescued",
                    reputation_score=reputation_score,
                    counterpart_email=counterpart_email,
                    subject=subject,
                    landed_in_spam=True,
                    was_rescued=True,
                    daily_volume=inbox.get("daily_sent") or 0,
                )
            except Exception as exc:
                logger.error("Inbox %s: error rescuing message %s: %s", inbox_email, msg_id, exc)

        # Expunge deleted messages from spam
        mail.expunge()

    except Exception as exc:
        logger.error("Inbox %s: spam rescue failed: %s", inbox_email, exc)
        log_action(supabase, inbox_id, "error", reputation_score=reputation_score, notes=f"Spam rescue error: {exc}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return rescued_count


# ---------------------------------------------------------------------------
# Path 2: Warmup network account processing
# ---------------------------------------------------------------------------

def process_warmup_network_account(
    account: dict,
    supabase: Client,
    claude_client: anthropic.Anthropic,
    client_inbox_emails: set[str],
) -> None:
    """
    Process one warmup network Gmail account:
      - Find unread emails FROM any client inbox
      - Mark each as read (simulates an open event)
      - With REPLY_RATE probability: generate a reply and send it back
      - Log received / replied events against the originating client inbox

    Args:
        account: Dict with 'email' and 'password' for the warmup network account.
        client_inbox_emails: Set of lowercase client inbox email addresses (for filtering).
    """
    net_email: str = account["email"]
    net_password: str = account["password"]
    imap_host = get_imap_server("google")  # Warmup network is always Gmail
    smtp_host = get_smtp_server("google")

    try:
        mail = imaplib.IMAP4_SSL(imap_host, IMAP_PORT)
        mail.login(net_email, net_password)
    except Exception as exc:
        logger.error("Warmup account %s: IMAP login failed: %s", net_email, exc)
        return

    try:
        mail.select("INBOX")
        # Search ONLY for unread emails FROM our client inboxes.
        # Gmail-side filter — avoids fetching unrelated mail and prevents OVERQUOTA.
        # IMAP doesn't support OR with multiple FROM clauses cleanly across all servers,
        # so we run one SEARCH per client inbox and merge the results.
        all_ids: list[bytes] = []
        for client_email in client_inbox_emails:
            try:
                status, msg_ids = mail.search(None, "UNSEEN", "FROM", f'"{client_email}"')
                if status == "OK" and msg_ids and msg_ids[0]:
                    all_ids.extend(msg_ids[0].split())
            except Exception as exc:
                logger.debug("Warmup account %s: SEARCH FROM %s failed: %s", net_email, client_email, exc)

        # Deduplicate (an email could in theory match multiple senders — shouldn't happen but be safe)
        ids = list(dict.fromkeys(all_ids))
        if not ids:
            mail.logout()
            return

        logger.info(
            "Warmup account %s: found %d unread email(s) from client inboxes.",
            net_email, len(ids),
        )

        for msg_id in ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
                parsed = email_lib.message_from_bytes(raw)

                sender_address = extract_sender_address(parsed)
                subject = decode_header_value(parsed.get("Subject", ""))

                # Only process emails FROM our client inboxes
                if sender_address not in client_inbox_emails:
                    continue

                # Look up the client inbox row for logging and reputation updates
                client_inbox = get_inbox_by_email(supabase, sender_address)
                if not client_inbox:
                    logger.warning(
                        "Warmup account %s: received email from %s but no matching inbox row found.",
                        net_email,
                        sender_address,
                    )
                    continue

                inbox_id = client_inbox["id"]
                reputation_score: float = client_inbox.get("reputation_score") or 50.0

                # Mark as read (\\Seen flag) — simulates opening the email
                mail.store(msg_id, "+FLAGS", "\\Seen")

                log_action(
                    supabase,
                    inbox_id=inbox_id,
                    action="received",
                    reputation_score=reputation_score,
                    counterpart_email=net_email,
                    subject=subject,
                    was_replied=False,
                    daily_volume=client_inbox.get("daily_sent") or 0,
                )

                # Reputation: +0.5 for received/opened
                reputation_events: dict[str, int] = {"received": 1}

                # Decide whether to send a reply
                send_reply = random.random() < REPLY_RATE
                if send_reply:
                    body = get_email_body(parsed)
                    sender_name = net_email.split("@")[0].replace(".", " ").title()
                    recipient_name = sender_address.split("@")[0].replace(".", " ").title()

                    try:
                        reply_body = generate_reply(
                            claude_client, body, sender_name, recipient_name,
                            supabase_client=supabase, client_id=None,
                        )
                    except Exception as exc:
                        logger.error(
                            "Warmup account %s: Claude reply generation failed for msg from %s: %s",
                            net_email, sender_address, exc,
                        )
                        # Continue without reply — don't abort entire message processing
                        send_reply = False

                    if send_reply:
                        try:
                            send_reply_via_smtp(
                                smtp_host=smtp_host,
                                sender_email=net_email,
                                sender_password=net_password,
                                sender_name=sender_name,
                                recipient_email=sender_address,
                                original_subject=subject,
                                reply_body=reply_body,  # type: ignore[possibly-undefined]
                            )
                            log_action(
                                supabase,
                                inbox_id=inbox_id,
                                action="replied",
                                reputation_score=reputation_score,
                                counterpart_email=net_email,
                                subject=f"Re: {subject}",
                                was_replied=True,
                                daily_volume=client_inbox.get("daily_sent") or 0,
                            )
                            logger.info(
                                "Warmup account %s: replied to %s (subject: %s).",
                                net_email, sender_address, subject,
                            )
                        except Exception as exc:
                            logger.error(
                                "Warmup account %s: SMTP reply to %s failed: %s",
                                net_email, sender_address, exc,
                            )
                            log_action(
                                supabase,
                                inbox_id=inbox_id,
                                action="error",
                                reputation_score=reputation_score,
                                counterpart_email=net_email,
                                notes=f"Reply SMTP failed: {exc}",
                            )

                # Update reputation for this inbox
                update_reputation(supabase, inbox_id, reputation_score, reputation_events)

            except Exception as exc:
                logger.error("Warmup account %s: error processing message %s: %s", net_email, msg_id, exc)

    except Exception as exc:
        logger.error("Warmup account %s: unhandled IMAP error: %s", net_email, exc)
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Main entry point for the IMAP processor.

    1. Process all client inboxes: rescue spam, update reputation.
    2. Process all warmup network accounts: read emails, optionally reply.

    Each inbox/account is fully isolated — an error in one never stops others.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Aborting.")
        return
    if not ANTHROPIC_API_KEY:
        logger.critical("ANTHROPIC_API_KEY not set. Aborting.")
        return

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load credentials and account lists
    inbox_passwords = load_client_inbox_credentials()
    warmup_network = load_warmup_network()

    # ── Path 1: Client inbox spam rescue ──────────────────────────────────
    raw_inboxes = load_active_inboxes(supabase)
    logger.info("Processing spam rescue for %d client inbox(es).", len(raw_inboxes))

    for inbox in raw_inboxes:
        inbox_email = inbox["email"]
        password = inbox_passwords.get(inbox_email.lower())
        if not password:
            logger.warning("No password found in env for client inbox %s — skipping spam rescue.", inbox_email)
            continue

        try:
            rescued = rescue_spam_for_inbox(inbox, password, supabase)
            if rescued > 0:
                # Update lifetime spam_rescues counter and apply reputation delta
                increment_spam_rescues(supabase, inbox["id"], rescued)
                update_reputation(
                    supabase,
                    inbox["id"],
                    inbox.get("reputation_score") or 50.0,
                    {"spam_rescued": rescued},
                )
                logger.info("Inbox %s: rescued %d email(s) from spam.", inbox_email, rescued)
        except Exception as exc:
            logger.error("Inbox %s: unhandled spam rescue error: %s", inbox_email, exc)

    # ── Path 1b: Client inbox reply-back to warmup network replies ─────────
    # When a warmup Gmail replies to our client inbox, sometimes reply back
    # to simulate a natural multi-turn conversation (max 3 messages per thread).
    warmup_emails: set[str] = {a["email"].lower() for a in warmup_network} if warmup_network else set()
    REPLY_BACK_RATE = 0.50  # 50% chance to reply back to a warmup reply

    for inbox in raw_inboxes:
        inbox_email = inbox["email"]
        inbox_id = inbox["id"]
        password = inbox_passwords.get(inbox_email.lower())
        if not password:
            continue

        provider = inbox.get("provider", "google")
        imap_host = get_imap_server(provider)
        smtp_host = get_smtp_server(provider)

        try:
            mail = imaplib.IMAP4_SSL(imap_host, IMAP_PORT)
            mail.login(inbox_email, password)
            mail.select("INBOX")

            # Search for unread emails FROM warmup network accounts
            reply_ids: list[bytes] = []
            for warmup_email in warmup_emails:
                try:
                    status, msg_ids = mail.search(None, "UNSEEN", "FROM", f'"{warmup_email}"')
                    if status == "OK" and msg_ids and msg_ids[0]:
                        reply_ids.extend(msg_ids[0].split())
                except Exception:
                    pass

            reply_ids = list(dict.fromkeys(reply_ids))
            if not reply_ids:
                mail.logout()
                continue

            logger.info("Client inbox %s: found %d unread warmup reply(ies).", inbox_email, len(reply_ids))

            for msg_id in reply_ids:
                try:
                    status, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue

                    raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
                    parsed = email_lib.message_from_bytes(raw)
                    sender_address = extract_sender_address(parsed)
                    subject = decode_header_value(parsed.get("Subject", ""))

                    if sender_address not in warmup_emails:
                        continue

                    # Mark as read
                    mail.store(msg_id, "+FLAGS", "\\Seen")

                    # Count Re: depth — stop after 3 exchanges
                    re_depth = subject.lower().count("re:")
                    if re_depth >= 3:
                        logger.debug("Client inbox %s: thread depth %d for '%s' — not replying.", inbox_email, re_depth, subject[:40])
                        continue

                    # Decide whether to reply back
                    if random.random() >= REPLY_BACK_RATE:
                        continue

                    body = get_email_body(parsed)
                    sender_name = inbox_email.split("@")[0].replace(".", " ").title()
                    recipient_name = sender_address.split("@")[0].replace(".", " ").title()

                    try:
                        reply_body = generate_reply(
                            claude_client, body, sender_name, recipient_name,
                            supabase_client=supabase, client_id=inbox.get("client_id"),
                        )
                    except Exception as exc:
                        logger.error("Client inbox %s: reply generation failed: %s", inbox_email, exc)
                        continue

                    try:
                        send_reply_via_smtp(
                            smtp_host=smtp_host,
                            sender_email=inbox_email,
                            sender_password=password,
                            sender_name=sender_name,
                            recipient_email=sender_address,
                            original_subject=subject,
                            reply_body=reply_body,
                        )
                        log_action(
                            supabase,
                            inbox_id=inbox_id,
                            action="replied",
                            reputation_score=inbox.get("reputation_score") or 50.0,
                            counterpart_email=sender_address,
                            subject=f"Re: {subject}" if not subject.lower().startswith("re:") else subject,
                            was_replied=True,
                        )
                        update_reputation(supabase, inbox_id, inbox.get("reputation_score") or 50.0, {"replied": 1})
                        logger.info("Client inbox %s: replied back to %s (depth %d).", inbox_email, sender_address, re_depth + 1)
                    except Exception as exc:
                        logger.error("Client inbox %s: SMTP reply-back failed: %s", inbox_email, exc)

                except Exception as exc:
                    logger.error("Client inbox %s: error processing warmup reply: %s", inbox_email, exc)

            mail.logout()
        except Exception as exc:
            logger.error("Client inbox %s: reply-back processing error: %s", inbox_email, exc)

    # ── Path 2: Warmup network account processing ──────────────────────────
    if not warmup_network:
        logger.warning("No warmup network accounts configured — skipping network processing.")
    else:
        # Build a set of all client inbox emails for fast sender matching
        client_inbox_emails: set[str] = {inbox["email"].lower() for inbox in raw_inboxes}

        logger.info("Processing %d warmup network account(s).", len(warmup_network))
        for account in warmup_network:
            try:
                process_warmup_network_account(account, supabase, claude_client, client_inbox_emails)
                # Small delay between accounts to avoid rate-limit issues
                time.sleep(random.uniform(1, 3))
            except Exception as exc:
                logger.error("Warmup account %s: unhandled error: %s", account.get("email"), exc)

    logger.info("IMAP processor run complete.")


if __name__ == "__main__":
    main()
