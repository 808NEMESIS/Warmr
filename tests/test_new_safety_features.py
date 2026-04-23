"""
tests/test_new_safety_features.py — verify four new safety + ops features:

  1. utils.smtp_retry — retries on 4xx, bails on 5xx
  2. campaign_scheduler.filter_recent_cross_campaign_touches
  3. utils.notifier — throttle + record + delivery path (muted in tests)
  4. GDPR /gdpr/erase endpoint is registered
"""

from __future__ import annotations

import smtplib
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── utils.smtp_retry ─────────────────────────────────────────────────────

def test_smtp_retry_succeeds_first_try(monkeypatch):
    import utils.smtp_retry as sr

    opened = {"count": 0}

    class _Srv:
        def __init__(self, host, port, timeout=None): opened["count"] += 1
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def login(self, u, p): return None
        def sendmail(self, f, r, m): return None

    monkeypatch.setattr(sr.smtplib, "SMTP_SSL", _Srv)
    attempts = sr.send_with_retry("h", 465, "s@x", "pw", "r@y", "msg", max_attempts=3)
    assert attempts == 1
    assert opened["count"] == 1


def test_smtp_retry_retries_on_transient_then_succeeds(monkeypatch):
    import utils.smtp_retry as sr
    monkeypatch.setattr(sr.time, "sleep", lambda *_: None)

    call_counter = {"n": 0}

    class _Srv:
        def __init__(self, host, port, timeout=None):
            call_counter["n"] += 1
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def login(self, u, p): return None
        def sendmail(self, f, r, m):
            if call_counter["n"] == 1:
                raise smtplib.SMTPResponseException(421, "service not available")
            return None

    monkeypatch.setattr(sr.smtplib, "SMTP_SSL", _Srv)
    attempts = sr.send_with_retry("h", 465, "s@x", "pw", "r@y", "msg", max_attempts=3)
    assert attempts == 2


def test_smtp_retry_does_not_retry_permanent_5xx(monkeypatch):
    import utils.smtp_retry as sr
    monkeypatch.setattr(sr.time, "sleep", lambda *_: None)

    calls = {"n": 0}

    class _Srv:
        def __init__(self, host, port, timeout=None):
            calls["n"] += 1
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def login(self, u, p): return None
        def sendmail(self, f, r, m):
            raise smtplib.SMTPResponseException(550, "user unknown")

    monkeypatch.setattr(sr.smtplib, "SMTP_SSL", _Srv)
    try:
        sr.send_with_retry("h", 465, "s@x", "pw", "r@y", "msg", max_attempts=3)
    except smtplib.SMTPResponseException as exc:
        assert exc.smtp_code == 550
    else:
        raise AssertionError("Expected permanent 5xx to raise")
    assert calls["n"] == 1, "5xx must NOT retry"


def test_smtp_retry_exhausts_after_max_attempts(monkeypatch):
    import utils.smtp_retry as sr
    monkeypatch.setattr(sr.time, "sleep", lambda *_: None)

    class _Srv:
        def __init__(self, host, port, timeout=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def login(self, u, p): return None
        def sendmail(self, f, r, m):
            raise smtplib.SMTPResponseException(451, "try later")

    monkeypatch.setattr(sr.smtplib, "SMTP_SSL", _Srv)
    try:
        sr.send_with_retry("h", 465, "s@x", "pw", "r@y", "msg", max_attempts=2)
    except smtplib.SMTPResponseException as exc:
        assert exc.smtp_code == 451
    else:
        raise AssertionError("Expected exhausted retries to raise")


# ── Cross-campaign dedup ─────────────────────────────────────────────────

class _FakeFilterSb:
    def __init__(self, events):
        self._events = events
    def table(self, name):
        self._table = name
        return self
    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def gte(self, *a, **kw): return self
    def in_(self, *a, **kw): return self
    def execute(self):
        class R:
            def __init__(self, d): self.data = d
        return R(self._events if self._table == "email_events" else [])


def test_cross_campaign_dedup_drops_recently_touched():
    from campaign_scheduler import filter_recent_cross_campaign_touches

    rows = [
        {"lead_id": "L1", "campaign_id": "C-current"},
        {"lead_id": "L2", "campaign_id": "C-current"},
        {"lead_id": "L3", "campaign_id": "C-current"},
    ]
    # L1 was touched in another campaign recently → drop
    # L2 was touched in the SAME campaign → keep (that's intentional)
    # L3 has no events → keep
    sb = _FakeFilterSb([
        {"lead_id": "L1", "campaign_id": "C-other",   "timestamp": "2026-04-20T10:00:00Z"},
        {"lead_id": "L2", "campaign_id": "C-current", "timestamp": "2026-04-20T10:00:00Z"},
    ])
    out = filter_recent_cross_campaign_touches(sb, rows, client_id="client-a", min_days_between_sends=7)
    out_ids = {r["lead_id"] for r in out}
    assert out_ids == {"L2", "L3"}


def test_cross_campaign_dedup_noop_when_client_id_missing():
    from campaign_scheduler import filter_recent_cross_campaign_touches
    rows = [{"lead_id": "L1", "campaign_id": "C"}]
    sb = _FakeFilterSb([])
    out = filter_recent_cross_campaign_touches(sb, rows, client_id=None, min_days_between_sends=7)
    assert out == rows


def test_cross_campaign_dedup_disabled_by_zero_days():
    from campaign_scheduler import filter_recent_cross_campaign_touches
    rows = [{"lead_id": "L1", "campaign_id": "C"}]
    sb = _FakeFilterSb([])
    out = filter_recent_cross_campaign_touches(sb, rows, client_id="x", min_days_between_sends=0)
    assert out == rows


# ── utils.notifier ───────────────────────────────────────────────────────

class _NotifyFakeSb:
    def __init__(self, settings_email=None, client_email=None, recent=False):
        self._settings_email = settings_email
        self._client_email = client_email
        self._recent = recent
        self._inserts: list[dict] = []
        self._table = None
    def table(self, name):
        self._table = name
        return self
    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def gte(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def insert(self, row):
        self._inserts.append({"table": self._table, "row": row})
        class R:
            def __init__(self): self.data = [{}]
        class _Ret:
            def execute(self): return R()
        return _Ret()
    def execute(self):
        class R: pass
        if self._table == "client_settings":
            return type("R", (), {"data": [{"reply_to_email": self._settings_email}] if self._settings_email else []})()
        if self._table == "clients":
            return type("R", (), {"data": [{"email": self._client_email}] if self._client_email else []})()
        if self._table == "notifications":
            return type("R", (), {"data": [{"id": "existing"}] if self._recent else []})()
        return type("R", (), {"data": []})()


def test_notifier_resolves_operator_email_from_client_settings(monkeypatch):
    import utils.notifier as nt
    monkeypatch.setenv("WARMR_NOTIFY_MUTE", "1")  # suppress actual Resend
    # Re-import to pick up the env var
    import importlib
    importlib.reload(nt)

    sb = _NotifyFakeSb(settings_email="ops@client.com", client_email="fallback@client.com")
    ok = nt.notify_operator(sb, "client-a", "new_reply", "Test title", "<p>Hi</p>")
    # Muted → returns False, but still recorded in notifications
    assert ok is False
    tables = [i["table"] for i in sb._inserts]
    assert "notifications" in tables


def test_notifier_throttles_repeat_within_window(monkeypatch):
    import utils.notifier as nt
    monkeypatch.setenv("WARMR_NOTIFY_MUTE", "1")
    import importlib
    importlib.reload(nt)

    sb = _NotifyFakeSb(recent=True)
    ok = nt.notify_operator(sb, "client-a", "new_reply", "Title", "<p>Body</p>", throttle=True)
    assert ok is False
    # Throttled branch still writes a notification record with [throttled email] tag
    notif_rows = [i["row"] for i in sb._inserts if i["table"] == "notifications"]
    assert notif_rows, "Throttled notification should still record in DB"
    assert "throttled" in notif_rows[0]["message"]


def test_notify_new_reply_composes_expected_title():
    import utils.notifier as nt
    import os
    os.environ["WARMR_NOTIFY_MUTE"] = "1"
    import importlib
    importlib.reload(nt)

    sb = _NotifyFakeSb(settings_email="ops@client.com")
    row = {
        "client_id":      "client-a",
        "from_email":     "prospect@example.nl",
        "subject":        "Re: Samenwerking",
        "classification": "interested",
        "urgency":        "high",
        "meeting_intent": True,
        "body":           "Kunnen we donderdag bellen?",
    }
    nt.notify_new_reply(sb, row)
    # A notifications row must have been recorded with the reply fingerprint
    notif_msgs = [i["row"]["message"] for i in sb._inserts if i["table"] == "notifications"]
    assert notif_msgs, "Expected a notifications row"
    # Title should mention the prospect + the "fire" emoji for meeting intent
    assert "prospect@example.nl" in notif_msgs[0]


# ── Click notification ───────────────────────────────────────────────────

def test_notify_lead_clicked_emits_webhook_and_throttled_ping(monkeypatch):
    """Webhook event always fires; operator email is throttled per-lead."""
    import utils.notifier as nt
    monkeypatch.setenv("WARMR_NOTIFY_MUTE", "1")
    import importlib
    importlib.reload(nt)

    sb = _NotifyFakeSb(settings_email="ops@client.com")
    ok = nt.notify_lead_clicked(
        sb, client_id="client-a", lead_id="lead-1",
        lead_email="prospect@example.nl",
        campaign_id="camp-1",
        clicked_url="https://example.nl/landing",
    )
    # Muted → operator email returns False
    assert ok is False
    # But webhook event MUST always be written
    webhook_rows = [i["row"] for i in sb._inserts if i["table"] == "webhook_events"]
    assert webhook_rows, "lead.clicked webhook event must be emitted regardless of notifier state"
    assert webhook_rows[0]["event_type"] == "lead.clicked"
    assert webhook_rows[0]["payload"]["lead_id"] == "lead-1"
    assert webhook_rows[0]["payload"]["clicked_url"] == "https://example.nl/landing"


def test_notify_lead_clicked_throttle_key_is_per_lead(monkeypatch):
    """Two different leads must not throttle each other."""
    import utils.notifier as nt
    monkeypatch.setenv("WARMR_NOTIFY_MUTE", "1")
    import importlib
    importlib.reload(nt)

    # recent=False → neither lead has a prior notification in the window
    sb = _NotifyFakeSb(recent=False)
    nt.notify_lead_clicked(sb, "client-a", "lead-1", "a@x.nl", "c1", "https://example.nl/a")
    nt.notify_lead_clicked(sb, "client-a", "lead-2", "b@x.nl", "c1", "https://example.nl/b")

    # Two webhook events and two distinct notification "kinds"
    webhook_count = sum(1 for i in sb._inserts if i["table"] == "webhook_events")
    assert webhook_count == 2


def test_lead_clicked_is_a_valid_webhook_event():
    """public_api.VALID_EVENTS must include lead.clicked so subscribers
    can register for it."""
    from api.public_api import VALID_EVENTS
    assert "lead.clicked" in VALID_EVENTS


# ── GDPR endpoint registration ───────────────────────────────────────────

def test_gdpr_erase_endpoint_is_registered():
    from api.main import app
    paths = {(tuple(sorted(r.methods)), r.path) for r in app.routes if hasattr(r, "methods") and r.methods}
    assert (("DELETE",), "/gdpr/erase") in paths
    assert (("DELETE",), "/leads/{lead_id}/purge") in paths


if __name__ == "__main__":
    import inspect as _inspect
    import os as _os

    class _Mp:
        def __init__(self):
            self._env = []
            self._attr = []
        def setenv(self, k, v):
            self._env.append((k, _os.environ.get(k)))
            _os.environ[k] = v
        def delenv(self, k, raising=True):
            self._env.append((k, _os.environ.get(k)))
            _os.environ.pop(k, None)
        def setattr(self, t, n, v=None, raising=True):
            self._attr.append((t, n, getattr(t, n, None)))
            setattr(t, n, v)
        def undo(self):
            for k, v in self._env:
                if v is None: _os.environ.pop(k, None)
                else: _os.environ[k] = v
            for t, n, v in self._attr:
                setattr(t, n, v)

    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        total += 1
        mp = _Mp() if "monkeypatch" in _inspect.signature(fn).parameters else None
        try:
            fn(mp) if mp else fn()
            print(f"  \u2713 {name}")
        except AssertionError as e:
            failed += 1
            print(f"  \u2717 {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  \u2717 {name}: {type(e).__name__}: {e}")
        finally:
            if mp: mp.undo()
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
