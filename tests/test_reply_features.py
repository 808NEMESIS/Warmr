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
