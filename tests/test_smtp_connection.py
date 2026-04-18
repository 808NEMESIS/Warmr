"""
tests/test_smtp_connection.py — Test SMTP SSL connection for all configured inboxes.
Does NOT send any email — only tests authentication.
"""
import os
import sys
import smtplib
import ssl

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

print("=== Step 4 — SMTP connection test ===")
print(f"Host: {SMTP_HOST}:{SMTP_PORT} (SSL)\n")

i = 1
any_failed = False
while True:
    email = os.getenv(f"INBOX_{i}_EMAIL")
    password = os.getenv(f"INBOX_{i}_PASSWORD")
    if not email:
        break

    print(f"Inbox {i}: {email}")
    print(f"  Password loaded: {'YES (' + str(len(password)) + ' chars)' if password else 'NO'}")

    if not password:
        print(f"  RESULT: FAIL — no password set\n")
        any_failed = True
        i += 1
        continue

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=15) as server:
            server.ehlo()
            server.login(email, password)
            caps = server.esmtp_features
            print(f"  SMTP EHLO: OK")
            print(f"  Login:     OK")
            print(f"  RESULT: PASS\n")
    except smtplib.SMTPAuthenticationError as e:
        print(f"  RESULT: FAIL — Authentication error: {e}")
        print(f"  → Check that this is a Google App Password (not the account password)")
        print(f"    Generate at: myaccount.google.com → Security → 2-Step Verification → App passwords\n")
        any_failed = True
    except smtplib.SMTPException as e:
        print(f"  RESULT: FAIL — SMTP error: {e}\n")
        any_failed = True
    except Exception as e:
        print(f"  RESULT: FAIL — {type(e).__name__}: {e}\n")
        any_failed = True

    i += 1

if i == 1:
    print("No inboxes configured (INBOX_1_EMAIL not set)")
else:
    print(f"Tested {i - 1} inbox(es). Overall: {'PASS' if not any_failed else 'SOME FAILED'}")
