"""
warmup_engine.py

Sends warmup emails from all active client inboxes to warmup network accounts.
Called by the n8n warm-up-sender workflow every 20 minutes during the send window.

Flow per run:
  1. Load active inboxes from Supabase (warmup_active=true, daily_sent < target)
  2. For each inbox: calculate week, set daily target, pick recipient, generate content, send
  3. Log every send (and every error) to warmup_logs
  4. Increment daily_sent counter in inboxes table
"""
from __future__ import annotations

import logging
import os
import random
import smtplib
import time
from datetime import date, datetime, timedelta
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
MAX_DAILY_WARMUP: int  = int(os.getenv("MAX_DAILY_WARMUP", "80"))
MAX_HOURLY_WARMUP: int = int(os.getenv("MAX_HOURLY_WARMUP", "15"))
SEND_WINDOW_START: str = os.getenv("SEND_WINDOW_START", "07:00")
SEND_WINDOW_END: str = os.getenv("SEND_WINDOW_END", "19:00")

# Daily warmup targets per week (midpoints of the ranges in CLAUDE.md)
WEEKLY_TARGETS: dict[int, int] = {
    1: 10,
    2: 20,
    3: 35,
    4: 45,
}
WEEK_5_PLUS_TARGET: int = 60  # Week 5 and beyond

# SMTP servers per provider
SMTP_SERVERS: dict[str, str] = {
    "google": "smtp.gmail.com",
    "microsoft": "smtp.office365.com",
}
SMTP_PORT: int = 465  # SSL


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_warmup_network() -> list[dict]:
    """
    Load all warmup network accounts from numbered environment variables.

    Reads WARMUP_NETWORK_1_EMAIL / _PASSWORD, WARMUP_NETWORK_2_EMAIL / _PASSWORD, …
    until a gap is found. Returns a list of dicts with 'email' and 'password' keys.
    """
    accounts: list[dict] = []
    i = 1
    while True:
        email = os.getenv(f"WARMUP_NETWORK_{i}_EMAIL")
        if not email:
            break
        password = os.getenv(f"WARMUP_NETWORK_{i}_PASSWORD", "")
        accounts.append({"email": email.strip(), "password": password.strip()})
        i += 1

    if not accounts:
        logger.warning("No warmup network accounts found in environment. Check WARMUP_NETWORK_*_EMAIL vars.")
    else:
        logger.info("Loaded %d warmup network accounts.", len(accounts))

    return accounts


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def load_active_inboxes(supabase: Client) -> list[dict]:
    """
    Fetch all inboxes from Supabase that are eligible for warmup sends this run.

    Eligibility: warmup_active=true AND daily_sent < daily_warmup_target AND status != 'retired'.
    The daily_warmup_target may be stale; the engine updates it after calculating the correct week.
    We fetch all warmup_active inboxes and filter locally after recalculating targets.
    """
    response = (
        supabase.table("inboxes")
        .select("*")
        .eq("warmup_active", True)
        .neq("status", "retired")
        .execute()
    )
    inboxes = response.data or []
    logger.info("Fetched %d active inboxes from Supabase.", len(inboxes))
    return inboxes


def get_used_recipients_today(supabase: Client, inbox_id: str) -> set[str]:
    """
    Return the set of counterpart_email addresses already sent to by this inbox today.

    Prevents sending warmup emails to the same recipient more than once per day.
    """
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    response = (
        supabase.table("warmup_logs")
        .select("counterpart_email")
        .eq("inbox_id", inbox_id)
        .eq("action", "sent")
        .gte("timestamp", today_start)
        .execute()
    )
    return {row["counterpart_email"] for row in (response.data or []) if row.get("counterpart_email")}


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
    Insert one row into warmup_logs.

    Always call this — even on errors — so there is a complete audit trail.
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
        # Never let a logging failure crash the engine
        logger.error("Failed to write to warmup_logs for inbox %s: %s", inbox_id, exc)


def update_daily_sent(supabase: Client, inbox_id: str, new_count: int) -> int:
    """
    Atomically increment daily_sent by 1 via Supabase RPC.

    Falls back to non-atomic update if the RPC doesn't exist.
    Returns the new count.
    """
    try:
        resp = supabase.rpc("increment_daily_sent", {"inbox_uuid": inbox_id}).execute()
        return resp.data if isinstance(resp.data, int) else new_count
    except Exception:
        # Fallback: non-atomic update (for backwards compatibility)
        try:
            supabase.table("inboxes").update(
                {"daily_sent": new_count, "updated_at": datetime.utcnow().isoformat()}
            ).eq("id", inbox_id).execute()
        except Exception as exc:
            logger.error("Failed to update daily_sent for inbox %s: %s", inbox_id, exc)
        return new_count


def auto_reset_stale_counters(supabase: Client, inboxes: list[dict]) -> int:
    """
    Self-healing: if an inbox's `daily_sent > 0` but the last `sent` action
    on that inbox was on a different calendar day (UTC), reset `daily_sent` to 0.

    This recovers from missed daily_reset cron runs (Mac asleep overnight,
    server downtime, etc.) without waiting for the next midnight.

    Returns the number of inboxes that were reset.
    """
    reset_count = 0
    today_iso = datetime.utcnow().strftime("%Y-%m-%d")
    for inbox in inboxes:
        current = int(inbox.get("daily_sent") or 0)
        if current <= 0:
            continue  # Nothing to reset
        try:
            # Find the most recent `sent` action for this inbox
            resp = (
                supabase.table("warmup_logs")
                .select("timestamp")
                .eq("inbox_id", inbox["id"])
                .eq("action", "sent")
                .order("timestamp", desc=True)
                .limit(1)
                .execute()
            )
            if not resp.data:
                # Counter > 0 but zero sends in log — stale. Reset.
                supabase.table("inboxes").update({"daily_sent": 0}).eq("id", inbox["id"]).execute()
                inbox["daily_sent"] = 0
                reset_count += 1
                logger.info("Auto-reset stale counter (no sends in log) for %s.", inbox.get("email"))
                continue

            last_sent_day = str(resp.data[0]["timestamp"])[:10]
            if last_sent_day != today_iso:
                supabase.table("inboxes").update({"daily_sent": 0}).eq("id", inbox["id"]).execute()
                inbox["daily_sent"] = 0
                reset_count += 1
                logger.info(
                    "Auto-reset stale counter for %s (last send on %s, today is %s).",
                    inbox.get("email"), last_sent_day, today_iso,
                )
        except Exception as exc:
            logger.warning("Self-healing reset failed for inbox %s: %s", inbox.get("email"), exc)

    if reset_count:
        logger.info("Self-healing: reset %d stale inbox counter(s).", reset_count)
    return reset_count


def update_warmup_target(supabase: Client, inbox_id: str, target: int) -> None:
    """
    Persist the recalculated daily_warmup_target back to the inboxes row.

    Called at the start of each run so the dashboard always reflects the current week's target.
    """
    try:
        supabase.table("inboxes").update(
            {"daily_warmup_target": target, "updated_at": datetime.utcnow().isoformat()}
        ).eq("id", inbox_id).execute()
    except Exception as exc:
        logger.error("Failed to update daily_warmup_target for inbox %s: %s", inbox_id, exc)


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def calculate_warmup_week(warmup_start_date: date) -> int:
    """
    Return the current warmup week number (1-indexed) based on the start date.

    Week 1 = days 0–6, Week 2 = days 7–13, etc.
    Returns at least 1 even if the start date is in the future (edge case protection).
    """
    days_elapsed = (date.today() - warmup_start_date).days
    week = max(1, (days_elapsed // 7) + 1)
    return week


def get_weekly_target(week: int) -> int:
    """
    Return the daily warmup email target for the given week number.

    Caps at MAX_DAILY_WARMUP regardless of week.
    """
    target = WEEKLY_TARGETS.get(week, WEEK_5_PLUS_TARGET)
    return min(target, MAX_DAILY_WARMUP)


def select_recipient(
    network: list[dict],
    sender_email: str,
    used_today: set[str],
) -> Optional[dict]:
    """
    Pick a random warmup network account that:
      - Is not the sender (avoids self-send)
      - Has not already received a warmup email from this inbox today

    Returns None if all accounts are exhausted for today.
    """
    available = [
        acc for acc in network
        if acc["email"].lower() != sender_email.lower()
        and acc["email"].lower() not in {e.lower() for e in used_today}
    ]
    if not available:
        return None
    return random.choice(available)


def extract_display_name(email_address: str) -> str:
    """
    Derive a plausible first name from an email address local part.

    Examples: john.doe@example.com → John, warmup3@gmail.com → Warmup3
    """
    local = email_address.split("@")[0]
    # Take the first segment split by . or _ or - and capitalise it
    first = local.replace("_", ".").replace("-", ".").split(".")[0]
    return first.capitalize()


def generate_email_content(
    claude_client: anthropic.Anthropic,
    sender_name: str,
    recipient_name: str,
    supabase: Optional[Client] = None,
    client_id: Optional[str] = None,
    inbox_id: Optional[str] = None,
) -> tuple[str, str]:
    """
    Generate a unique warmup email via Claude Haiku.

    Returns a (subject, body) tuple. The prompt instructs Claude to output the
    subject on the first line prefixed with 'Subject:' and the body after a blank line,
    so we can parse both from one API call.

    If supabase is provided, uses tracked_claude_call for cost tracking + budget enforcement.
    """
    topics = [
        "project update",
        "meeting follow-up",
        "quick question",
        "feedback request",
        "brief check-in",
        "resource share",
    ]
    topic = random.choice(topics)

    prompt = (
        f"Generate a short professional business email in {WARMUP_LANGUAGE} (80–120 words).\n"
        f"From: {sender_name}, To: {recipient_name}.\n"
        f"Topic: {topic}.\n"
        "Sound completely natural and human. No marketing language. No template-like phrases.\n"
        "Vary sentence length. Use a natural greeting and sign-off.\n"
        "Format your response EXACTLY as follows — no other text:\n"
        "Subject: <one-line subject>\n"
        "\n"
        "<email body only, no subject line repeated>"
    )

    messages = [{"role": "user", "content": prompt}]

    if supabase:
        from utils.cost_tracker import tracked_claude_call
        message = tracked_claude_call(
            claude_client, supabase,
            model="claude-haiku-4-5-20251001",
            messages=messages,
            max_tokens=300,
            context="warmup_content",
            client_id=client_id,
            inbox_id=inbox_id,
        )
    else:
        message = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=messages,
        )

    raw = message.content[0].text.strip()
    lines = raw.split("\n")

    # Parse subject from first line
    subject = ""
    body_lines: list[str] = []
    found_subject = False
    for i, line in enumerate(lines):
        if not found_subject and line.lower().startswith("subject:"):
            subject = line[len("subject:"):].strip()
            found_subject = True
        elif found_subject:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    # Fallback in case Claude deviates from the format
    if not subject:
        subject = f"Re: {topic.capitalize()}"
    if not body:
        body = raw

    return subject, body


def is_within_send_window() -> bool:
    """
    Return True if the current time falls within SEND_WINDOW_START–SEND_WINDOW_END.

    Uses the local server clock. n8n also enforces the window, but this provides
    a defensive guard if the script is invoked directly.
    """
    now_str = datetime.now().strftime("%H:%M")
    return SEND_WINDOW_START <= now_str <= SEND_WINDOW_END


def get_smtp_server(provider: str) -> str:
    """Return the SMTP hostname for the given provider. Defaults to Gmail."""
    return SMTP_SERVERS.get(provider.lower() if provider else "google", "smtp.gmail.com")


def send_via_smtp(
    smtp_host: str,
    sender_email: str,
    sender_password: str,
    sender_name: str,
    recipient_email: str,
    recipient_name: str,
    subject: str,
    body: str,
) -> None:
    """
    Send one warmup email via SMTP SSL on port 465.

    Raises on any SMTP failure so the caller can catch, log, and continue.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = f"{recipient_name} <{recipient_email}>"
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(smtp_host, SMTP_PORT) as server:
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, [recipient_email], msg.as_string())


# ---------------------------------------------------------------------------
# Per-inbox processing
# ---------------------------------------------------------------------------

def process_inbox(
    inbox: dict,
    supabase: Client,
    claude_client: anthropic.Anthropic,
    warmup_network: list[dict],
) -> None:
    """
    Execute one warmup send cycle for a single inbox.

    Steps:
      1. Parse warmup_start_date and calculate current week + daily target
      2. Update daily_warmup_target in DB
      3. Skip if daily_sent already meets target
      4. Get recipients already used today
      5. Pick a new recipient from the warmup network
      6. Generate email content via Claude Haiku
      7. Apply a random delay (simulates human send timing)
      8. Send via SMTP SSL
      9. Log to warmup_logs and increment daily_sent
    """
    inbox_id: str = inbox["id"]
    inbox_email: str = inbox["email"]
    provider: str = inbox.get("provider") or "google"
    daily_sent: int = inbox.get("daily_sent") or 0
    reputation_score: float = inbox.get("reputation_score") or 50.0

    # ── Step 1: calculate warmup week and daily target ─────────────────────
    warmup_start_raw = inbox.get("warmup_start_date")
    if not warmup_start_raw:
        logger.warning("Inbox %s has no warmup_start_date — skipping.", inbox_email)
        return

    try:
        warmup_start = date.fromisoformat(str(warmup_start_raw))
    except ValueError:
        logger.error("Inbox %s has invalid warmup_start_date '%s' — skipping.", inbox_email, warmup_start_raw)
        return

    week = calculate_warmup_week(warmup_start)
    daily_target = get_weekly_target(week)

    # ── Step 2: persist updated target ────────────────────────────────────
    update_warmup_target(supabase, inbox_id, daily_target)

    # ── Step 3: check remaining capacity ──────────────────────────────────
    if daily_sent >= daily_target:
        logger.info("Inbox %s already at daily target (%d/%d) — skipping.", inbox_email, daily_sent, daily_target)
        return

    # ── Step 3b: per-hour burst cap ────────────────────────────────────────
    # Gmail throws 421 "temporary failure" if we send too many in a short
    # window. Enforce MAX_HOURLY_WARMUP regardless of daily headroom.
    try:
        from datetime import datetime, timedelta, timezone
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        hour_resp = (
            supabase.table("warmup_logs")
            .select("id", count="exact")
            .eq("inbox_id", inbox_id)
            .eq("action", "sent")
            .gte("timestamp", hour_ago)
            .execute()
        )
        sent_last_hour = hour_resp.count or 0
        if sent_last_hour >= MAX_HOURLY_WARMUP:
            logger.info(
                "Inbox %s at hourly cap (%d/%d in last 60 min) — skipping this run.",
                inbox_email, sent_last_hour, MAX_HOURLY_WARMUP,
            )
            return
    except Exception as exc:
        # If the cap check itself fails, proceed — daily cap still protects us
        logger.debug("Hourly cap check failed for %s (continuing): %s", inbox_email, exc)

    # ── Step 4: find recipients used today ────────────────────────────────
    used_today = get_used_recipients_today(supabase, inbox_id)

    # ── Step 5: pick recipient ─────────────────────────────────────────────
    recipient = select_recipient(warmup_network, inbox_email, used_today)
    if recipient is None:
        logger.warning(
            "Inbox %s: all %d warmup network accounts already used today.",
            inbox_email,
            len(warmup_network),
        )
        return

    # ── Step 6: generate email content ────────────────────────────────────
    sender_name = extract_display_name(inbox_email)
    recipient_name = extract_display_name(recipient["email"])

    try:
        subject, body = generate_email_content(
            claude_client, sender_name, recipient_name,
            supabase=supabase, client_id=inbox.get("client_id"), inbox_id=inbox_id,
        )
    except Exception as exc:
        logger.error("Inbox %s: Claude content generation failed: %s", inbox_email, exc)
        log_action(
            supabase,
            inbox_id=inbox_id,
            action="error",
            reputation_score=reputation_score,
            warmup_week=week,
            daily_volume=daily_sent,
            counterpart_email=recipient["email"],
            notes=f"Claude generation failed: {exc}",
        )
        return

    # ── Step 7: random delay (1–30 seconds) ───────────────────────────────
    delay = random.uniform(1, 30)
    logger.debug("Inbox %s: sleeping %.1fs before send.", inbox_email, delay)
    time.sleep(delay)

    # ── Step 8: send via SMTP ─────────────────────────────────────────────
    smtp_host = get_smtp_server(provider)
    try:
        send_via_smtp(
            smtp_host=smtp_host,
            sender_email=inbox_email,
            sender_password=inbox.get("password", ""),  # password fetched from env, not DB
            sender_name=sender_name,
            recipient_email=recipient["email"],
            recipient_name=recipient_name,
            subject=subject,
            body=body,
        )
    except Exception as exc:
        logger.error("Inbox %s: SMTP send to %s failed: %s", inbox_email, recipient["email"], exc)
        log_action(
            supabase,
            inbox_id=inbox_id,
            action="error",
            reputation_score=reputation_score,
            warmup_week=week,
            daily_volume=daily_sent,
            counterpart_email=recipient["email"],
            subject=subject,
            notes=f"SMTP error: {exc}",
        )
        return

    # ── Step 9: log success and update counter ─────────────────────────────
    new_daily_sent = daily_sent + 1
    log_action(
        supabase,
        inbox_id=inbox_id,
        action="sent",
        reputation_score=reputation_score,
        warmup_week=week,
        daily_volume=new_daily_sent,
        counterpart_email=recipient["email"],
        subject=subject,
    )
    update_daily_sent(supabase, inbox_id, new_daily_sent)

    logger.info(
        "Inbox %s → %s | week %d | %d/%d sent today | subject: %s",
        inbox_email,
        recipient["email"],
        week,
        new_daily_sent,
        daily_target,
        subject,
    )


# ---------------------------------------------------------------------------
# Inbox credential injection
# ---------------------------------------------------------------------------

def inject_inbox_passwords(inboxes: list[dict]) -> list[dict]:
    """
    Attach the SMTP password for each inbox from environment variables.

    Passwords are never stored in Supabase — they live in numbered env vars
    (INBOX_1_EMAIL / INBOX_1_PASSWORD, etc.). This function matches by email
    address and injects the 'password' key into each inbox dict.

    Inboxes with no matching env var are logged and excluded from processing.
    """
    # Build a lookup: email → password from env
    env_credentials: dict[str, str] = {}
    i = 1
    while True:
        email = os.getenv(f"INBOX_{i}_EMAIL")
        if not email:
            break
        password = os.getenv(f"INBOX_{i}_PASSWORD", "")
        env_credentials[email.strip().lower()] = password.strip()
        i += 1

    enriched: list[dict] = []
    for inbox in inboxes:
        email_lower = inbox["email"].lower()
        if email_lower not in env_credentials:
            logger.warning(
                "No env credentials found for inbox %s — excluding from this run.",
                inbox["email"],
            )
            continue
        inbox["password"] = env_credentials[email_lower]
        enriched.append(inbox)

    return enriched


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def dry_run_preview(
    supabase: Client,
    claude_client: anthropic.Anthropic,
    warmup_network: list[dict],
    inboxes: list[dict],
) -> None:
    """
    Print what the warmup engine would send without actually sending anything.
    Generates real Claude content so the preview is representative.
    """
    print("\n" + "=" * 60)
    print("DRY RUN — no emails will be sent")
    print("=" * 60)

    if not warmup_network:
        print("WARNING: No warmup network accounts in .env — cannot select recipients.")
        print("         Add WARMUP_NETWORK_1_EMAIL / _PASSWORD to .env to continue.")

    if not inboxes:
        print("WARNING: No active inboxes in Supabase.")
        print("         Add inbox rows to the 'inboxes' table to continue.")
        print("\nWhat would happen with configured inboxes from .env:")

        # Simulate using env-configured inboxes directly
        i = 1
        while True:
            email = os.getenv(f"INBOX_{i}_EMAIL")
            if not email:
                break
            provider = os.getenv(f"INBOX_{i}_PROVIDER", "google")
            domain = os.getenv(f"INBOX_{i}_DOMAIN", email.split("@")[-1])
            inboxes.append({
                "id": f"env-inbox-{i}",
                "email": email,
                "provider": provider,
                "domain": domain,
                "warmup_active": True,
                "warmup_start_date": date.today().isoformat(),
                "daily_sent": 0,
                "daily_warmup_target": 10,
                "reputation_score": 50.0,
                "status": "warmup",
                "password": os.getenv(f"INBOX_{i}_PASSWORD", ""),
            })
            i += 1

    if not warmup_network:
        # Simulate with placeholder recipients
        warmup_network = [{"email": "warmup-example@gmail.com", "password": ""}]

    for inbox in inboxes:
        inbox_email = inbox.get("email", "unknown")
        warmup_start_raw = inbox.get("warmup_start_date") or date.today().isoformat()
        try:
            warmup_start = date.fromisoformat(str(warmup_start_raw))
        except ValueError:
            warmup_start = date.today()

        week = calculate_warmup_week(warmup_start)
        daily_target = get_weekly_target(week)
        daily_sent = inbox.get("daily_sent") or 0
        used_today: set[str] = set()

        recipient = select_recipient(warmup_network, inbox_email, used_today)
        if recipient is None:
            print(f"\n[{inbox_email}] No available recipient — warmup network exhausted for today.")
            continue

        sender_name = extract_display_name(inbox_email)
        recipient_name = extract_display_name(recipient["email"])

        print(f"\n{'─' * 60}")
        print(f"FROM:      {sender_name} <{inbox_email}>")
        print(f"TO:        {recipient_name} <{recipient['email']}>")
        print(f"SMTP:      {get_smtp_server(inbox.get('provider') or 'google')}:465")
        print(f"Week:      {week}  |  Target: {daily_target}/day  |  Sent today: {daily_sent}")
        print(f"Generating content via Claude Haiku...")

        try:
            subject, body = generate_email_content(
                claude_client, sender_name, recipient_name,
                supabase=supabase, client_id=inbox.get("client_id"), inbox_id=inbox.get("id"),
            )
            print(f"SUBJECT:   {subject}")
            print(f"BODY:\n{body}")
        except Exception as exc:
            print(f"Content generation failed: {exc}")

    print(f"\n{'=' * 60}")
    print("DRY RUN complete. No emails sent, no DB writes made.")


def main() -> None:
    """
    Main entry point for the warmup engine.

    Validates the send window, loads all dependencies, then processes each
    eligible inbox in a random order (to avoid predictable patterns).
    All exceptions are caught per inbox so a single failure never stops the run.

    Pass --dry-run to preview sends without sending anything.
    """
    import sys
    dry_run = "--dry-run" in sys.argv

    if not dry_run and not is_within_send_window():
        logger.info("Outside send window (%s–%s). Exiting.", SEND_WINDOW_START, SEND_WINDOW_END)
        return

    # Validate required config
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Aborting.")
        return
    if not ANTHROPIC_API_KEY:
        logger.critical("ANTHROPIC_API_KEY not set. Aborting.")
        return

    # Initialise clients
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load warmup network accounts from env
    warmup_network = load_warmup_network()

    # Load active inboxes from Supabase and inject passwords from env
    raw_inboxes = load_active_inboxes(supabase)
    inboxes = inject_inbox_passwords(raw_inboxes)

    # Self-healing: reset stale daily_sent counters before processing.
    # Protects against missed daily_reset cron runs (laptop slept overnight).
    auto_reset_stale_counters(supabase, inboxes)

    if dry_run:
        dry_run_preview(supabase, claude_client, warmup_network, inboxes)
        return

    if not warmup_network:
        logger.critical("No warmup network accounts configured. Aborting.")
        return

    if not inboxes:
        logger.info("No eligible inboxes to process this run.")
        return

    # Randomise order so no inbox always gets priority
    random.shuffle(inboxes)

    logger.info("Processing %d inbox(es) this run.", len(inboxes))
    for inbox in inboxes:
        try:
            process_inbox(inbox, supabase, claude_client, warmup_network)
        except Exception as exc:
            # Final safety net — log and continue to next inbox
            logger.error("Unhandled error for inbox %s: %s", inbox.get("email"), exc)
            try:
                log_action(
                    supabase,
                    inbox_id=inbox["id"],
                    action="error",
                    reputation_score=inbox.get("reputation_score") or 50.0,
                    notes=f"Unhandled exception in process_inbox: {exc}",
                )
            except Exception:
                pass  # If logging itself fails, we still continue

    logger.info("Warmup engine run complete.")


if __name__ == "__main__":
    main()
