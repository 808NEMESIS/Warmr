"""
tests/test_bounce_handler.py — unit tests for bounce classification + DSN parsing.

Pure-function tests. No real IMAP or DB.
"""

import email as email_lib
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounce_handler as bh


# ── classify_bounce ────────────────────────────────────────────────────────

def test_classify_hard_by_550_code():
    body = "The following message could not be delivered.\n550 5.1.1 User unknown"
    assert bh.classify_bounce(body) == "hard"


def test_classify_hard_by_user_unknown_phrase():
    body = "Mail delivery failed: user unknown"
    assert bh.classify_bounce(body) == "hard"


def test_classify_hard_by_does_not_exist():
    body = "Recipient address does not exist."
    assert bh.classify_bounce(body) == "hard"


def test_classify_soft_by_452_code():
    body = "Temporary problem: 4.5.2 Mailbox is full. Try again later."
    assert bh.classify_bounce(body) == "soft"


def test_classify_soft_by_mailbox_full():
    # The classifier uses the literal substring "mailbox full" — common DSN text
    body = "Delivery failed: mailbox full on recipient server."
    assert bh.classify_bounce(body) == "soft"


def test_classify_soft_by_temporarily():
    body = "Delivery temporarily deferred; will retry."
    assert bh.classify_bounce(body) == "soft"


def test_classify_defaults_hard_when_ambiguous():
    # When nothing matches, err on the side of hard to protect reputation
    body = "Something went wrong but without any clear signal."
    assert bh.classify_bounce(body) == "hard"


# ── is_bounce_message / is_spam_complaint ─────────────────────────────────

def _msg(headers: dict, body: str = "body") -> EmailMessage:
    m = EmailMessage()
    for k, v in headers.items():
        m[k] = v
    m.set_content(body)
    return m


def test_is_bounce_message_from_mailer_daemon():
    msg = _msg({"From": "MAILER-DAEMON@google.com", "Subject": "Delivery failure"})
    assert bh.is_bounce_message(msg) is True


def test_is_bounce_message_from_postmaster():
    msg = _msg({"From": "postmaster@outlook.com", "Subject": "Undeliverable"})
    assert bh.is_bounce_message(msg) is True


def test_is_bounce_message_delivery_status_subject():
    msg = _msg({"From": "some@other.com", "Subject": "Delivery Status Notification"})
    assert bh.is_bounce_message(msg) is True


def test_is_bounce_message_rejects_normal_mail():
    msg = _msg({"From": "friend@example.com", "Subject": "hello"})
    assert bh.is_bounce_message(msg) is False


def test_is_spam_complaint_by_feedback_report():
    # is_spam_complaint checks raw Content-Type header + subject — no need for
    # a valid multipart body; parse a hand-crafted raw message instead.
    raw = b"""From: abuse@aol.com
Subject: Abuse report
Content-Type: multipart/report; report-type=feedback-report

test"""
    parsed = email_lib.message_from_bytes(raw)
    assert bh.is_spam_complaint(parsed) is True


def test_is_spam_complaint_ignores_normal_mail():
    msg = _msg({"From": "friend@example.com", "Subject": "hi"})
    assert bh.is_spam_complaint(msg) is False


# ── extract_original_recipient ─────────────────────────────────────────────

def test_extract_from_final_recipient_header():
    raw = b"""From: MAILER-DAEMON@google.com
Subject: Delivery Status Notification
Content-Type: multipart/report; boundary=BOUNDARY

--BOUNDARY
Content-Type: text/plain

The following message could not be delivered.

--BOUNDARY
Content-Type: message/delivery-status

Final-Recipient: rfc822; prospect@example.nl
Action: failed

--BOUNDARY--
"""
    parsed = email_lib.message_from_bytes(raw)
    result = bh.extract_original_recipient(parsed, raw.decode())
    assert result == "prospect@example.nl"


def test_extract_falls_back_to_body_regex():
    raw = b"""From: postmaster@outlook.com
Subject: Undeliverable

Your message to <prospect@example.nl> could not be delivered.
"""
    parsed = email_lib.message_from_bytes(raw)
    result = bh.extract_original_recipient(parsed, raw.decode())
    assert result == "prospect@example.nl"


def test_extract_returns_none_when_no_address():
    raw = b"""From: postmaster@outlook.com
Subject: Something

No addresses mentioned here at all."""
    parsed = email_lib.message_from_bytes(raw)
    assert bh.extract_original_recipient(parsed, raw.decode()) is None


# ── Reputation delta constants match CLAUDE.md contract ───────────────────

def test_reputation_delta_hard():
    assert bh.REPUTATION_DELTA["hard"] == -5.0


def test_reputation_delta_soft():
    assert bh.REPUTATION_DELTA["soft"] == -2.0


def test_reputation_delta_spam_complaint():
    assert bh.REPUTATION_DELTA["spam_complaint"] == -20.0


def test_bounce_rate_threshold_is_3_percent():
    assert bh.BOUNCE_RATE_THRESHOLD == 0.03


def test_soft_bounce_retry_limit():
    assert bh.SOFT_BOUNCE_MAX_RETRIES == 3


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"  \u2713 {name}")
            except AssertionError as e:
                failed += 1
                print(f"  \u2717 {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  \u2717 {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
