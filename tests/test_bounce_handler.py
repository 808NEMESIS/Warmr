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
# bh.BOUNCE_TYPE_TO_EVENT maps classify_bounce()'s output strings to the
# canonical event names in utils.reputation.REPUTATION_DELTA (Fase 3
# consolidation — bounce_handler.py no longer keeps its own delta dict).

def test_reputation_delta_hard():
    from utils.reputation import REPUTATION_DELTA
    assert REPUTATION_DELTA[bh.BOUNCE_TYPE_TO_EVENT["hard"]] == -5.0


def test_reputation_delta_soft():
    from utils.reputation import REPUTATION_DELTA
    assert REPUTATION_DELTA[bh.BOUNCE_TYPE_TO_EVENT["soft"]] == -2.0


def test_reputation_delta_spam_complaint():
    from utils.reputation import REPUTATION_DELTA
    assert REPUTATION_DELTA[bh.BOUNCE_TYPE_TO_EVENT["spam_complaint"]] == -20.0


def test_bounce_rate_threshold_is_3_percent():
    assert bh.BOUNCE_RATE_THRESHOLD == 0.03


def test_soft_bounce_retry_limit():
    assert bh.SOFT_BOUNCE_MAX_RETRIES == 3


# ── check_inbox_bounce_rate: the 7-day 3% kill switch ─────────────────────
# Regression coverage for the bug where both counts filtered on
# email_events.created_at (doesn't exist — the real column is `timestamp`),
# so this always raised and the kill switch never fired.

from datetime import datetime, timedelta, timezone


class _Exec:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _BounceRateQuery:
    def __init__(self, store, table):
        self.store = store
        self.table_name = table
        self._op = "select"
        self._payload = None
        self._filters = []
        self._gte = None
        self._count_mode = None

    def select(self, *a, count=None, **k):
        self._count_mode = count
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def gte(self, col, val):
        self._gte = (col, val)
        return self

    def limit(self, n):
        return self

    def _match(self, row):
        for col, val in self._filters:
            if row.get(col) != val:
                return False
        if self._gte:
            col, cutoff = self._gte
            if not (row.get(col) or "") >= cutoff:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.table_name, [])
        if self._op == "insert":
            rows.append(self._payload)
            return _Exec([self._payload])
        matched = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in matched:
                r.update(self._payload)
        count = len(matched) if self._count_mode else None
        return _Exec(matched, count=count)


class _FakeBounceRateSb:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name):
        return _BounceRateQuery(self.store, name)


def _events(inbox_id: str, sent: int, bounced: int, days_ago: float = 1) -> list[dict]:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    rows = [{"inbox_id": inbox_id, "event_type": "sent", "timestamp": ts} for _ in range(sent)]
    rows += [{"inbox_id": inbox_id, "event_type": "bounced", "timestamp": ts} for _ in range(bounced)]
    return rows


def test_check_inbox_bounce_rate_pauses_inbox_over_threshold():
    sb = _FakeBounceRateSb()
    sb.store["email_events"] = _events("inbox-1", sent=40, bounced=3)  # 7.5% > 3%
    sb.store["inboxes"] = [{"id": "inbox-1", "email": "a@b.nl", "client_id": "c1", "status": "warmup"}]

    bh.check_inbox_bounce_rate(sb, "inbox-1")

    updated = next(r for r in sb.store["inboxes"] if r["id"] == "inbox-1")
    assert updated["status"] == "paused"
    assert updated["warmup_active"] is False


def test_check_inbox_bounce_rate_leaves_inbox_alone_under_threshold():
    sb = _FakeBounceRateSb()
    sb.store["email_events"] = _events("inbox-1", sent=40, bounced=1)  # 2.5% < 3%
    sb.store["inboxes"] = [{"id": "inbox-1", "email": "a@b.nl", "client_id": "c1", "status": "warmup"}]

    bh.check_inbox_bounce_rate(sb, "inbox-1")

    updated = next(r for r in sb.store["inboxes"] if r["id"] == "inbox-1")
    assert updated["status"] == "warmup"


def test_check_inbox_bounce_rate_skips_below_sample_size():
    """Under 30 sends, even a 100% bounce rate must not trigger a pause —
    not enough sample size to trust the ratio."""
    sb = _FakeBounceRateSb()
    sb.store["email_events"] = _events("inbox-1", sent=10, bounced=10)
    sb.store["inboxes"] = [{"id": "inbox-1", "email": "a@b.nl", "client_id": "c1", "status": "warmup"}]

    bh.check_inbox_bounce_rate(sb, "inbox-1")

    updated = next(r for r in sb.store["inboxes"] if r["id"] == "inbox-1")
    assert updated["status"] == "warmup"


def test_check_inbox_bounce_rate_ignores_events_outside_the_7day_window():
    sb = _FakeBounceRateSb()
    sb.store["email_events"] = _events("inbox-1", sent=40, bounced=10, days_ago=30)  # stale, outside window
    sb.store["inboxes"] = [{"id": "inbox-1", "email": "a@b.nl", "client_id": "c1", "status": "warmup"}]

    bh.check_inbox_bounce_rate(sb, "inbox-1")

    updated = next(r for r in sb.store["inboxes"] if r["id"] == "inbox-1")
    assert updated["status"] == "warmup"  # below sample size within the window (0 counted)


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
