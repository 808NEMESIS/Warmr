"""
tests/test_reply_features.py — tests for the new reply features:

  - Feature #14: classify_reply_with_signals (meeting_intent + urgency + category)
  - Feature #3:  POST /replies/{id}/send route registration
  - Feature #1:  POST /replies/{id}/analyze-intent route registration

Mostly unit-level — the classifier tests use monkey-patched Anthropic client
responses so they never hit the live API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── classify_reply_with_signals ───────────────────────────────────────────

def _fake_message(text: str):
    """Build a minimal object that mimics anthropic.types.Message."""
    msg = MagicMock()
    content = MagicMock()
    content.text = text
    msg.content = [content]
    return msg


def test_signal_classifier_parses_valid_response(monkeypatch):
    import reply_classifier

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_message(json.dumps({
        "category": "interested",
        "meeting_intent": True,
        "urgency": "high",
        "rationale": "Explicit availability offer",
    }))
    monkeypatch.setattr(reply_classifier.anthropic, "Anthropic", lambda api_key: fake_client)

    result = reply_classifier.classify_reply_with_signals(
        "Ik heb donderdag 14:00 vrij, kun je dan bellen?",
        "Re: Samenwerking",
    )
    assert result["category"] == "interested"
    assert result["meeting_intent"] is True
    assert result["urgency"] == "high"
    assert "availability" in result["rationale"].lower()


def test_signal_classifier_normalizes_invalid_category(monkeypatch):
    import reply_classifier

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_message(json.dumps({
        "category": "NOT_A_VALID_CATEGORY",
        "meeting_intent": False,
        "urgency": "unknown",
        "rationale": "",
    }))
    monkeypatch.setattr(reply_classifier.anthropic, "Anthropic", lambda api_key: fake_client)

    result = reply_classifier.classify_reply_with_signals("iets onduidelijks", "")
    assert result["category"] == "other"
    assert result["urgency"] == "low"  # "unknown" falls back


def test_signal_classifier_strips_markdown_fences(monkeypatch):
    import reply_classifier

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_message(
        "```json\n"
        + json.dumps({
            "category": "question",
            "meeting_intent": False,
            "urgency": "low",
            "rationale": "Neutral question",
        })
        + "\n```"
    )
    monkeypatch.setattr(reply_classifier.anthropic, "Anthropic", lambda api_key: fake_client)

    result = reply_classifier.classify_reply_with_signals("Wat is jullie prijsmodel?", "")
    assert result["category"] == "question"
    assert result["meeting_intent"] is False


def test_signal_classifier_falls_back_on_error(monkeypatch):
    """If the Claude call errors, we fall back to the legacy classifier."""
    import reply_classifier

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("API down")
    monkeypatch.setattr(reply_classifier.anthropic, "Anthropic", lambda api_key: fake_client)

    # Legacy classifier also hits Anthropic; stub it to return "other"
    monkeypatch.setattr(reply_classifier, "classify_reply", lambda *a, **kw: "other")

    result = reply_classifier.classify_reply_with_signals("...", "")
    assert result["category"] == "other"
    assert result["meeting_intent"] is False
    assert result["urgency"] == "low"


def test_signal_classifier_no_api_key_falls_back(monkeypatch):
    import reply_classifier

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(reply_classifier, "classify_reply", lambda *a, **kw: "other")

    result = reply_classifier.classify_reply_with_signals("x", "y")
    assert result["meeting_intent"] is False


# ── Route registration checks ────────────────────────────────────────────

def test_reply_send_route_is_registered():
    from api.main import app
    paths = {(tuple(sorted(r.methods)), r.path) for r in app.routes if hasattr(r, "methods") and r.methods}
    assert (("POST",), "/replies/{reply_id}/send") in paths


def test_analyze_intent_route_is_registered():
    from api.main import app
    paths = {(tuple(sorted(r.methods)), r.path) for r in app.routes if hasattr(r, "methods") and r.methods}
    assert (("POST",), "/replies/{reply_id}/analyze-intent") in paths


# ── Prospect-reply capture (imap_processor.scan_client_inbox_for_prospect_replies) ──

def _mk_raw_email(
    from_addr: str,
    subject: str = "Re: Samenwerking",
    body: str = "Klinkt interessant, kunnen we volgende week bellen?",
    message_id: str = "<abc123@prospect.example>",
    in_reply_to: str = "<parent@warmr.example>",
    references: str = "<root@warmr.example> <parent@warmr.example>",
) -> bytes:
    """Build a minimal RFC-822 email as bytes."""
    lines = [
        f"From: {from_addr}",
        f"To: sender@example.nl",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
        f"In-Reply-To: {in_reply_to}",
        f"References: {references}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    return ("\r\n".join(lines)).encode("utf-8")


class _FakeSbTable:
    """Minimal Supabase query-builder stub with record/replay of writes."""

    def __init__(self, store: dict):
        self._store = store
        self._table = None
        self._filters: list[tuple[str, str, object]] = []
        self._select_cols = "*"

    def __call__(self, table: str):
        self._table = table
        self._filters = []
        self._select_cols = "*"
        return self

    def select(self, cols="*", **kwargs):
        self._select_cols = cols
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def limit(self, n):
        return self

    def execute(self):
        class _R:
            def __init__(self, data):
                self.data = data
        # Reads: leads table + reply_inbox dedup check
        if self._table == "leads":
            rows = [r for r in self._store.get("leads", [])
                    if all(r.get(c) == v for op, c, v in self._filters)]
            return _R(rows)
        if self._table == "reply_inbox":
            # Dedup check
            rows = [r for r in self._store.get("reply_inbox_rows", [])
                    if all(r.get(c) == v for op, c, v in self._filters)]
            return _R(rows)
        return _R([])

    def insert(self, row):
        key = "reply_inbox_rows" if self._table == "reply_inbox" else (
            "webhook_events_rows" if self._table == "webhook_events" else "other_rows"
        )
        self._store.setdefault(key, []).append(row)
        class _R:
            def __init__(self, data):
                self.data = data
        return _Stub(_R([row]))

    def update(self, patch):
        # record update
        self._store.setdefault("updates", []).append({"table": self._table, "patch": patch, "filters": list(self._filters)})
        class _R:
            def __init__(self, data):
                self.data = data
        return _Stub(_R([{}]))


class _Stub:
    """Helper: wraps an object so `.eq(...).execute()` returns it."""
    def __init__(self, result):
        self._result = result
    def eq(self, *a, **kw):
        return self
    def execute(self):
        return self._result


class _FakeSb:
    def __init__(self, store: dict):
        self._store = store
        self._qb = _FakeSbTable(store)
    def table(self, name):
        return self._qb(name)


class _FakeMail:
    """Minimal imaplib.IMAP4_SSL stub with one UNSEEN msg."""

    def __init__(self, raw_bytes: bytes):
        self._raw = raw_bytes

    def login(self, u, p): return ("OK", [b"Logged in"])
    def select(self, mailbox): return ("OK", [b"1"])
    def search(self, charset, *criteria):
        return ("OK", [b"1"])
    def fetch(self, msg_id, spec):
        return ("OK", [(b"1 (RFC822 {N}", self._raw)])
    def store(self, *a, **kw): return ("OK", [b""])
    def logout(self): return ("BYE", [b""])


def test_scan_stores_known_lead_reply(monkeypatch):
    import imap_processor

    store = {
        "leads": [{"id": "lead-1", "email": "prospect@example.nl", "client_id": "client-a"}],
        "reply_inbox_rows": [],
    }
    sb = _FakeSb(store)

    monkeypatch.setattr(imap_processor, "imaplib",
        type("I", (), {"IMAP4_SSL": lambda host, port: _FakeMail(_mk_raw_email("prospect@example.nl"))}))
    # Skip Claude classification — forces the fallback defaults
    monkeypatch.setattr(imap_processor, "get_imap_server", lambda p: "imap.gmail.com")

    def _stub_classify(*a, **kw):
        return {"category": "interested", "meeting_intent": True, "urgency": "high", "rationale": ""}
    import reply_classifier
    monkeypatch.setattr(reply_classifier, "classify_reply_with_signals", _stub_classify)

    inbox = {"id": "inbox-1", "email": "sender@example.nl", "provider": "google", "client_id": "client-a"}
    saved = imap_processor.scan_client_inbox_for_prospect_replies(inbox, "pw", sb, warmup_emails=set())
    assert saved == 1
    rows = store["reply_inbox_rows"]
    assert len(rows) == 1
    assert rows[0]["from_email"] == "prospect@example.nl"
    assert rows[0]["classification"] == "interested"
    assert rows[0]["meeting_intent"] is True
    assert rows[0]["urgency"] == "high"
    assert rows[0]["message_id"] == "<abc123@prospect.example>"
    # Threading chain: References + In-Reply-To
    assert "<parent@warmr.example>" in (rows[0]["references_header"] or "")


def test_scan_ignores_warmup_network_sender(monkeypatch):
    import imap_processor
    store = {
        "leads": [{"id": "lead-1", "email": "warmup1@gmail.com", "client_id": "client-a"}],
        "reply_inbox_rows": [],
    }
    sb = _FakeSb(store)
    monkeypatch.setattr(imap_processor, "imaplib",
        type("I", (), {"IMAP4_SSL": lambda host, port: _FakeMail(_mk_raw_email("warmup1@gmail.com"))}))
    monkeypatch.setattr(imap_processor, "get_imap_server", lambda p: "imap.gmail.com")

    inbox = {"id": "inbox-1", "email": "sender@example.nl", "provider": "google", "client_id": "client-a"}
    saved = imap_processor.scan_client_inbox_for_prospect_replies(
        inbox, "pw", sb, warmup_emails={"warmup1@gmail.com"},
    )
    # Warmup-network sender must not be captured even if erroneously in leads
    assert saved == 0
    assert store["reply_inbox_rows"] == []


def test_scan_skips_unknown_sender(monkeypatch):
    import imap_processor
    store = {
        "leads": [{"id": "lead-1", "email": "prospect@example.nl", "client_id": "client-a"}],
        "reply_inbox_rows": [],
    }
    sb = _FakeSb(store)
    monkeypatch.setattr(imap_processor, "imaplib",
        type("I", (), {"IMAP4_SSL": lambda host, port: _FakeMail(_mk_raw_email("stranger@example.com"))}))
    monkeypatch.setattr(imap_processor, "get_imap_server", lambda p: "imap.gmail.com")

    inbox = {"id": "inbox-1", "email": "sender@example.nl", "provider": "google", "client_id": "client-a"}
    saved = imap_processor.scan_client_inbox_for_prospect_replies(inbox, "pw", sb, warmup_emails=set())
    assert saved == 0


def test_scan_deduplicates_on_message_id(monkeypatch):
    import imap_processor
    already_stored_mid = "<abc123@prospect.example>"
    store = {
        "leads": [{"id": "lead-1", "email": "prospect@example.nl", "client_id": "client-a"}],
        "reply_inbox_rows": [{"id": "existing", "client_id": "client-a", "message_id": already_stored_mid}],
    }
    sb = _FakeSb(store)
    monkeypatch.setattr(imap_processor, "imaplib",
        type("I", (), {"IMAP4_SSL": lambda host, port: _FakeMail(_mk_raw_email("prospect@example.nl", message_id=already_stored_mid))}))
    monkeypatch.setattr(imap_processor, "get_imap_server", lambda p: "imap.gmail.com")

    inbox = {"id": "inbox-1", "email": "sender@example.nl", "provider": "google", "client_id": "client-a"}
    saved = imap_processor.scan_client_inbox_for_prospect_replies(inbox, "pw", sb, warmup_emails=set())
    assert saved == 0
    # No new insert beyond the pre-existing one
    assert len(store["reply_inbox_rows"]) == 1


# ── Migration content sanity ─────────────────────────────────────────────

def test_migration_adds_required_columns():
    sql = (Path(__file__).resolve().parent.parent / "api" / "reply_features_migration.sql").read_text()
    for required in (
        "message_id",
        "references_header",
        "intent_analysis",
        "intent_analyzed_at",
        "meeting_intent",
        "urgency",
        "reply_sent_at",
        "reply_sent_body",
        "idx_reply_inbox_meeting",
    ):
        assert required in sql, f"Migration missing: {required}"


if __name__ == "__main__":
    # Manual pytest-free runner so this fits into tests/run_all.py
    failed = 0
    total = 0
    # Run monkeypatched tests via pytest when available; otherwise skip them
    # with a clear message — those require monkeypatch fixture.
    import inspect
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        total += 1
        sig = inspect.signature(fn)
        if "monkeypatch" in sig.parameters:
            # Emulate a minimal monkeypatch
            class _Mp:
                def __init__(self):
                    self._env_undo = []
                    self._attr_undo = []
                import os as _os
                def setenv(self, k, v):
                    self._env_undo.append((k, self._os.environ.get(k)))
                    self._os.environ[k] = v
                def delenv(self, k, raising=True):
                    self._env_undo.append((k, self._os.environ.get(k)))
                    self._os.environ.pop(k, None)
                def setattr(self, target, name, value=None, raising=True):
                    # target,name form only
                    self._attr_undo.append((target, name, getattr(target, name, None)))
                    setattr(target, name, value)
                def undo(self):
                    for k, v in self._env_undo:
                        if v is None:
                            self._os.environ.pop(k, None)
                        else:
                            self._os.environ[k] = v
                    for t, n, v in self._attr_undo:
                        setattr(t, n, v)
            mp = _Mp()
            try:
                fn(mp)
                print(f"  \u2713 {name}")
            except AssertionError as e:
                failed += 1
                print(f"  \u2717 {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  \u2717 {name}: {type(e).__name__}: {e}")
            finally:
                mp.undo()
        else:
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
