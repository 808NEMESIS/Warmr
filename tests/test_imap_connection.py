"""
tests/test_imap_connection.py — Test IMAP SSL connection for all configured inboxes.
Lists folders per inbox. Does NOT modify any mail.
"""
import os
import sys
import imaplib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

print("=== Step 5 — IMAP connection test ===")
print(f"Host: {IMAP_HOST}:{IMAP_PORT} (SSL)\n")

i = 1
any_failed = False
while True:
    email = os.getenv(f"INBOX_{i}_EMAIL")
    password = os.getenv(f"INBOX_{i}_PASSWORD")
    if not email:
        break

    print(f"Inbox {i}: {email}")

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(email, password)
        print(f"  Login: OK")

        status, folders = mail.list()
        folder_names = []
        for f in (folders or []):
            decoded = f.decode() if isinstance(f, bytes) else f
            # Extract the folder name after the last delimiter
            parts = decoded.split('"/"')
            name = parts[-1].strip().strip('"') if len(parts) > 1 else decoded
            folder_names.append(name)

        print(f"  Folders ({len(folder_names)}):")
        for fn in folder_names:
            print(f"    {fn}")

        # Check for expected folders
        folder_str = " ".join(folder_names).lower()
        has_inbox = any("inbox" in fn.lower() for fn in folder_names)
        has_sent  = any("sent" in fn.lower() for fn in folder_names)
        has_spam  = any("spam" in fn.lower() or "junk" in fn.lower() for fn in folder_names)
        print(f"  INBOX: {'✓' if has_inbox else '✗'}  Sent: {'✓' if has_sent else '✗'}  Spam/Junk: {'✓' if has_spam else '✗'}")

        mail.logout()
        print(f"  RESULT: PASS\n")
    except imaplib.IMAP4.error as e:
        print(f"  RESULT: FAIL — IMAP error: {e}")
        print(f"  → Check App Password and that IMAP is enabled in Gmail settings\n")
        any_failed = True
    except Exception as e:
        print(f"  RESULT: FAIL — {type(e).__name__}: {e}\n")
        any_failed = True

    i += 1

if i == 1:
    print("No inboxes configured (INBOX_1_EMAIL not set)")
else:
    print(f"Tested {i - 1} inbox(es). Overall: {'PASS' if not any_failed else 'SOME FAILED'}")
