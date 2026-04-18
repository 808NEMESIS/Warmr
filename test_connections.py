"""
test_connections.py

Verify SMTP + IMAP connectivity for all configured inboxes and warmup network accounts.
Logs in, lists folders, and logs out — does NOT send any email.

Usage:
    source .venv/bin/activate
    python test_connections.py
"""

import imaplib
import os
import smtplib
import sys

from dotenv import load_dotenv

load_dotenv()

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

SMTP_PORT = 465
IMAP_PORT = 993

PROVIDER_SMTP = {
    "google": "smtp.gmail.com",
    "microsoft": "smtp.office365.com",
}
PROVIDER_IMAP = {
    "google": "imap.gmail.com",
    "microsoft": "outlook.office365.com",
}


def test_smtp(email: str, password: str, provider: str) -> tuple[bool, str]:
    host = PROVIDER_SMTP.get(provider, "smtp.gmail.com")
    try:
        with smtplib.SMTP_SSL(host, SMTP_PORT, timeout=10) as server:
            server.login(email, password)
        return True, "OK"
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"Auth failed: {exc.smtp_error.decode() if isinstance(exc.smtp_error, bytes) else exc.smtp_error}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def test_imap(email: str, password: str, provider: str) -> tuple[bool, str]:
    host = PROVIDER_IMAP.get(provider, "imap.gmail.com")
    try:
        mail = imaplib.IMAP4_SSL(host, IMAP_PORT)
        mail.login(email, password)
        status, folders = mail.list()
        mail.logout()
        if status == "OK":
            return True, f"OK ({len(folders)} folders)"
        return False, f"Folder list failed: {status}"
    except imaplib.IMAP4.error as exc:
        return False, f"IMAP error: {exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def collect_client_inboxes() -> list[dict]:
    out = []
    i = 1
    while True:
        email = os.getenv(f"INBOX_{i}_EMAIL")
        if not email:
            break
        out.append({
            "email": email,
            "password": os.getenv(f"INBOX_{i}_PASSWORD", ""),
            "provider": os.getenv(f"INBOX_{i}_PROVIDER", "google"),
            "label": f"INBOX_{i}",
        })
        i += 1
    return out


def collect_warmup_network() -> list[dict]:
    out = []
    i = 1
    while True:
        email = os.getenv(f"WARMUP_NETWORK_{i}_EMAIL")
        if not email:
            break
        out.append({
            "email": email,
            "password": os.getenv(f"WARMUP_NETWORK_{i}_PASSWORD", ""),
            "provider": "google",
            "label": f"WARMUP_NETWORK_{i}",
        })
        i += 1
    return out


def main() -> int:
    client_inboxes = collect_client_inboxes()
    warmup_network = collect_warmup_network()

    print(f"\n{BOLD}Warmr — Connection Test{RESET}")
    print("=" * 60)
    print(f"Client inboxes:    {len(client_inboxes)}")
    print(f"Warmup network:    {len(warmup_network)}")
    print("=" * 60)

    failures = 0

    print(f"\n{BOLD}── CLIENT INBOXES ──{RESET}")
    for inbox in client_inboxes:
        label = inbox["label"]
        email = inbox["email"]
        pw = inbox["password"]
        provider = inbox["provider"]
        if not pw or pw.startswith("placeholder") or pw.startswith("<"):
            print(f"  {YELLOW}⚠{RESET} {label} {email}: no password set ({pw[:30]}...)")
            failures += 1
            continue
        smtp_ok, smtp_msg = test_smtp(email, pw, provider)
        imap_ok, imap_msg = test_imap(email, pw, provider)
        smtp_icon = f"{GREEN}✓{RESET}" if smtp_ok else f"{RED}✗{RESET}"
        imap_icon = f"{GREEN}✓{RESET}" if imap_ok else f"{RED}✗{RESET}"
        print(f"  {label} {email} ({provider})")
        print(f"     SMTP {smtp_icon} {smtp_msg}")
        print(f"     IMAP {imap_icon} {imap_msg}")
        if not smtp_ok or not imap_ok:
            failures += 1

    print(f"\n{BOLD}── WARMUP NETWORK ──{RESET}")
    for acct in warmup_network:
        label = acct["label"]
        email = acct["email"]
        pw = acct["password"]
        if not pw or pw.startswith("placeholder") or pw.startswith("<"):
            print(f"  {YELLOW}⚠{RESET} {label} {email}: no password set ({pw[:30]}...)")
            failures += 1
            continue
        smtp_ok, smtp_msg = test_smtp(email, pw, "google")
        imap_ok, imap_msg = test_imap(email, pw, "google")
        smtp_icon = f"{GREEN}✓{RESET}" if smtp_ok else f"{RED}✗{RESET}"
        imap_icon = f"{GREEN}✓{RESET}" if imap_ok else f"{RED}✗{RESET}"
        print(f"  {label} {email}")
        print(f"     SMTP {smtp_icon} {smtp_msg}")
        print(f"     IMAP {imap_icon} {imap_msg}")
        if not smtp_ok or not imap_ok:
            failures += 1

    print("\n" + "=" * 60)
    if failures == 0:
        print(f"{GREEN}{BOLD}✓ All connections OK{RESET}")
        return 0
    print(f"{RED}{BOLD}✗ {failures} connection(s) failed{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
