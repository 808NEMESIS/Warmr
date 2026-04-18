"""
placement_tester.py

Seed account inbox placement tester for Warmr.

Sends a test email from a client inbox to all configured seed accounts,
waits 5 minutes, then checks which folder each seed account received it in.

Seed accounts are read from environment variables:
    SEED_GMAIL_1_EMAIL, SEED_GMAIL_1_PASSWORD
    SEED_GMAIL_2_EMAIL, SEED_GMAIL_2_PASSWORD
    SEED_GMAIL_3_EMAIL, SEED_GMAIL_3_PASSWORD
    SEED_OUTLOOK_1_EMAIL, SEED_OUTLOOK_1_PASSWORD
    SEED_OUTLOOK_2_EMAIL, SEED_OUTLOOK_2_PASSWORD
    SEED_OUTLOOK_3_EMAIL, SEED_OUTLOOK_3_PASSWORD
    SEED_YAHOO_1_EMAIL, SEED_YAHOO_1_PASSWORD
    SEED_ICLOUD_1_EMAIL, SEED_ICLOUD_1_PASSWORD

IMAP settings per provider:
    Gmail:   imap.gmail.com:993
    Outlook: outlook.office365.com:993
    Yahoo:   imap.mail.yahoo.com:993
    iCloud:  imap.mail.me.com:993
"""

import imaplib
import logging
import os
import smtplib
import time
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

PLACEMENT_WAIT_SECONDS: int = 300  # 5 minutes
IMAP_SEARCH_TIMEOUT:    int = 10

# Folders to check per provider, in order of priority
_FOLDER_PRIORITY: dict[str, list[str]] = {
    "gmail": [
        "INBOX",
        "[Gmail]/Primary",
        "[Gmail]/Promotions",
        "[Gmail]/Social",
        "[Gmail]/Spam",
    ],
    "outlook": [
        "INBOX",
        "Junk",
        "Junk Email",
    ],
    "yahoo": [
        "INBOX",
        "Bulk Mail",
        "Spam",
    ],
    "icloud": [
        "INBOX",
        "Junk",
    ],
}

_SPAM_FOLDERS: set[str] = {
    "[Gmail]/Spam",
    "Junk",
    "Junk Email",
    "Bulk Mail",
    "Spam",
}

_PROMO_FOLDERS: set[str] = {
    "[Gmail]/Promotions",
    "[Gmail]/Social",
}

# IMAP hosts per provider
_IMAP_HOSTS: dict[str, str] = {
    "gmail":   "imap.gmail.com",
    "outlook": "outlook.office365.com",
    "yahoo":   "imap.mail.yahoo.com",
    "icloud":  "imap.mail.me.com",
}


def _sb() -> Client:
    """Create and return a Supabase client."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now_utc() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Seed account loading
# ---------------------------------------------------------------------------

def load_seed_accounts() -> list[dict]:
    """
    Load seed account credentials from environment variables.

    Returns list of dicts with keys: email, password, provider, imap_host.
    """
    providers = [
        ("gmail",   3),
        ("outlook", 3),
        ("yahoo",   1),
        ("icloud",  1),
    ]
    accounts: list[dict] = []

    for provider, count in providers:
        for i in range(1, count + 1):
            prefix = f"SEED_{provider.upper()}_{i}"
            email    = os.getenv(f"{prefix}_EMAIL", "")
            password = os.getenv(f"{prefix}_PASSWORD", "")
            if email and password:
                accounts.append({
                    "email":     email,
                    "password":  password,
                    "provider":  provider,
                    "imap_host": _IMAP_HOSTS[provider],
                })

    logger.info("Loaded %d seed accounts.", len(accounts))
    return accounts


# ---------------------------------------------------------------------------
# SMTP sending
# ---------------------------------------------------------------------------

def _get_smtp_host(provider: str) -> tuple[str, int]:
    """Return (host, port) for SMTP SSL based on provider."""
    hosts = {
        "google":    ("smtp.gmail.com", 465),
        "microsoft": ("smtp.office365.com", 587),
    }
    return hosts.get(provider, ("smtp.gmail.com", 465))


def send_test_email(
    inbox: dict,
    seed_accounts: list[dict],
    subject: str,
    body: str,
) -> tuple[Optional[str], list[str]]:
    """
    Send a placement test email from inbox to all seed accounts.

    Returns (message_id, list_of_delivered_seed_emails).
    Returns (None, []) on failure.
    """
    if not seed_accounts:
        logger.warning("No seed accounts configured — cannot run placement test.")
        return None, []

    message_id = f"<warmr-placement-{uuid.uuid4()}@warmr.test>"
    provider   = (inbox.get("provider") or "google").lower()
    smtp_host, smtp_port = _get_smtp_host(provider)

    recipients = [a["email"] for a in seed_accounts]

    try:
        msg = MIMEMultipart("alternative")
        msg["From"]       = inbox["email"]
        msg["To"]         = ", ".join(recipients)
        msg["Subject"]    = subject
        msg["Message-ID"] = message_id

        msg.attach(MIMEText(body, "plain", "utf-8"))
        raw = msg.as_bytes()

        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as smtp:
                smtp.login(inbox["email"], inbox["password"])
                smtp.sendmail(inbox["email"], recipients, raw)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as smtp:
                smtp.starttls()
                smtp.login(inbox["email"], inbox["password"])
                smtp.sendmail(inbox["email"], recipients, raw)

        logger.info(
            "Placement test sent from %s to %d seed accounts (message_id=%s).",
            inbox["email"], len(recipients), message_id,
        )
        return message_id, recipients

    except Exception as exc:
        logger.error("Failed to send placement test from %s: %s", inbox["email"], exc)
        return None, []


# ---------------------------------------------------------------------------
# IMAP folder checking
# ---------------------------------------------------------------------------

def _imap_search_folder(
    imap: imaplib.IMAP4_SSL,
    folder: str,
    message_id: str,
) -> bool:
    """
    Search a specific IMAP folder for the test message by Message-ID header.

    Returns True if found.
    """
    try:
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            return False
        # Search by header
        status, data = imap.search(None, f'HEADER Message-ID "{message_id}"')
        if status == "OK" and data and data[0]:
            return bool(data[0].strip())
    except Exception:
        pass
    return False


def check_placement(seed_account: dict, message_id: str) -> str:
    """
    Connect to a seed account via IMAP and find which folder the test message landed in.

    Returns one of: 'primary' | 'promotions' | 'spam' | 'missing'
    """
    provider  = seed_account["provider"]
    imap_host = seed_account["imap_host"]
    folders   = _FOLDER_PRIORITY.get(provider, ["INBOX"])

    try:
        with imaplib.IMAP4_SSL(imap_host, 993) as imap:
            imap.login(seed_account["email"], seed_account["password"])

            for folder in folders:
                if _imap_search_folder(imap, folder, message_id):
                    # Classify folder
                    if folder in _SPAM_FOLDERS:
                        return "spam"
                    if folder in _PROMO_FOLDERS:
                        return "promotions"
                    return "primary"

    except imaplib.IMAP4.error as exc:
        logger.warning(
            "IMAP login failed for seed %s: %s", seed_account["email"], exc
        )
    except Exception as exc:
        logger.warning(
            "IMAP check failed for seed %s: %s", seed_account["email"], exc
        )

    return "missing"


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def create_test_record(
    sb: Client,
    client_id: str,
    inbox_id: str,
    subject: str,
    body: str,
) -> str:
    """
    Insert a placement_tests row with status='running'.

    Returns the new test UUID.
    """
    row = sb.table("placement_tests").insert({
        "client_id":    client_id,
        "inbox_id":     inbox_id,
        "subject":      subject,
        "body_preview": body[:200] if body else "",
        "status":       "running",
        "created_at":   _now_utc(),
    }).execute()
    return row.data[0]["id"]


def save_result(
    sb: Client,
    test_id: str,
    seed_account: dict,
    placement: str,
) -> None:
    """Write a single seed account result to placement_test_results."""
    try:
        sb.table("placement_test_results").insert({
            "test_id":       test_id,
            "seed_provider": seed_account["provider"],
            "seed_email":    seed_account["email"],
            "placement":     placement,
            "checked_at":    _now_utc(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to save placement result: %s", exc)


def mark_test_completed(sb: Client, test_id: str, status: str = "completed") -> None:
    """Update placement_tests.status and completed_at."""
    try:
        sb.table("placement_tests").update({
            "status":       status,
            "completed_at": _now_utc(),
        }).eq("id", test_id).execute()
    except Exception as exc:
        logger.warning("Failed to mark test %s as %s: %s", test_id, status, exc)


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run_placement_test(
    inbox: dict,
    client_id: str,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    sb: Optional[Client] = None,
    wait_seconds: int = PLACEMENT_WAIT_SECONDS,
) -> dict:
    """
    Run a full placement test for one inbox.

    1. Create DB record
    2. Send test email to all seed accounts
    3. Wait wait_seconds
    4. Check each seed account folder via IMAP
    5. Save results
    6. Return summary

    Args:
        inbox:        Inbox dict with id, email, password, provider fields.
        client_id:    Client UUID string.
        subject:      Test email subject (generated if not provided).
        body:         Test email body (generated if not provided).
        sb:           Supabase client (created if not provided).
        wait_seconds: How long to wait before checking (default 300s).

    Returns:
        {
            "test_id": str,
            "inbox_email": str,
            "status": "completed" | "failed",
            "results": [{"provider", "email", "placement"}, ...],
            "summary": {"primary": int, "promotions": int, "spam": int, "missing": int},
        }
    """
    if sb is None:
        sb = _sb()

    if not subject:
        subject = "Quick question about your team's workflow"
    if not body:
        body = (
            "Hi,\n\n"
            "I came across your company and wanted to reach out briefly.\n\n"
            "Would you be open to a short conversation this week?\n\n"
            "Best regards"
        )

    seed_accounts = load_seed_accounts()
    if not seed_accounts:
        logger.error("No seed accounts loaded — aborting placement test.")
        return {"status": "failed", "error": "No seed accounts configured"}

    test_id = create_test_record(sb, client_id, inbox["id"], subject, body)

    message_id, delivered_to = send_test_email(inbox, seed_accounts, subject, body)
    if not message_id:
        mark_test_completed(sb, test_id, "failed")
        return {"test_id": test_id, "status": "failed", "error": "SMTP send failed"}

    logger.info("Waiting %d seconds before checking placement...", wait_seconds)
    time.sleep(wait_seconds)

    results: list[dict] = []
    summary: dict[str, int] = {"primary": 0, "promotions": 0, "spam": 0, "missing": 0}

    for account in seed_accounts:
        if account["email"] not in delivered_to:
            continue
        placement = check_placement(account, message_id)
        summary[placement] = summary.get(placement, 0) + 1
        results.append({
            "provider":  account["provider"],
            "email":     account["email"],
            "placement": placement,
        })
        save_result(sb, test_id, account, placement)
        logger.info(
            "Seed %s (%s): %s",
            account["email"], account["provider"], placement,
        )

    mark_test_completed(sb, test_id)

    logger.info(
        "Placement test complete — primary: %d, promotions: %d, spam: %d, missing: %d",
        summary["primary"], summary["promotions"], summary["spam"], summary["missing"],
    )

    return {
        "test_id":     test_id,
        "inbox_email": inbox["email"],
        "status":      "completed",
        "results":     results,
        "summary":     summary,
    }


def process_pending_tests(sb: Optional[Client] = None) -> int:
    """
    Fetch placement_tests with status='pending' and run them.

    Called by the n8n placement-test-processor workflow every 2 minutes.
    Returns number of tests processed.
    """
    if sb is None:
        sb = _sb()

    pending = (
        sb.table("placement_tests")
        .select("*")
        .eq("status", "pending")
        .order("created_at")
        .limit(3)
        .execute()
    ).data or []

    if not pending:
        return 0

    processed = 0
    for test in pending:
        inbox_id = test.get("inbox_id")
        if not inbox_id:
            mark_test_completed(sb, test["id"], "failed")
            continue

        inbox_row = (
            sb.table("inboxes")
            .select("id, email, password, provider")
            .eq("id", inbox_id)
            .limit(1)
            .execute()
        ).data or []

        if not inbox_row:
            mark_test_completed(sb, test["id"], "failed")
            continue

        inbox = inbox_row[0]
        # Mark running before processing to prevent double-pick
        sb.table("placement_tests").update({"status": "running"}).eq("id", test["id"]).execute()

        run_placement_test(
            inbox      = inbox,
            client_id  = test["client_id"],
            subject    = test.get("subject"),
            body       = test.get("body_preview"),
            sb         = sb,
            wait_seconds = PLACEMENT_WAIT_SECONDS,
        )
        processed += 1

    return processed


if __name__ == "__main__":
    result = process_pending_tests()
    logger.info("Processed %d placement test(s).", result)
