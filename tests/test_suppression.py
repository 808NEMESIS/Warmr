"""
tests/test_suppression.py — tests for campaign_scheduler suppression check
and unsubscribe link generation.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import campaign_scheduler as cs


class FakeTable:
    def __init__(self, data=None):
        self.data = data or []
        self.insert_calls = []
        self._pending_insert = None

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def limit(self, n): return self
    def insert(self, payload):
        self.insert_calls.append(payload)
        self._pending_insert = payload
        return self
    def execute(self):
        if self._pending_insert is not None:
            p = self._pending_insert
            self._pending_insert = None
            return _Exec([p])
        return _Exec(self.data)


class _Exec:
    def __init__(self, data): self.data = data


class FakeSupabase:
    def __init__(self, suppressed=None):
        self.suppressed = suppressed or []
        self._tables = {}

    def table(self, name):
        if name == "suppression_list":
            return FakeTable(self.suppressed)
        if name == "unsubscribe_tokens":
            t = self._tables.setdefault(name, FakeTable())
            return t
        return FakeTable()


# ── is_suppressed ──────────────────────────────────────────────────────────

def test_is_suppressed_returns_true_when_listed():
    sb = FakeSupabase(suppressed=[{"id": "s1"}])
    assert cs.is_suppressed(sb, "client-1", "alice@example.com") is True


def test_is_suppressed_returns_false_when_not_listed():
    sb = FakeSupabase(suppressed=[])
    assert cs.is_suppressed(sb, "client-1", "alice@example.com") is False


def test_is_suppressed_normalizes_case_and_whitespace():
    # The check lowercases + strips the input
    sb = FakeSupabase(suppressed=[{"id": "s1"}])
    assert cs.is_suppressed(sb, "client-1", "  Alice@EXAMPLE.com  ") is True


# ── Unsubscribe link generation ────────────────────────────────────────────

def test_generate_unsubscribe_link_creates_token():
    sb = FakeSupabase()
    url = cs.generate_unsubscribe_link(sb, "client-1", "lead-1", "alice@example.com", "camp-1")
    assert url.startswith("http")
    assert "/unsubscribe/" in url

    # Token was inserted
    unsub_table = sb._tables["unsubscribe_tokens"]
    assert len(unsub_table.insert_calls) == 1
    rec = unsub_table.insert_calls[0]
    assert rec["client_id"] == "client-1"
    assert rec["lead_id"] == "lead-1"
    assert rec["lead_email"] == "alice@example.com"
    assert rec["campaign_id"] == "camp-1"
    assert len(rec["token"]) >= 20  # token_urlsafe(32) base64 → ~43 chars


def test_append_unsubscribe_footer_adds_line():
    body = "Hi Jan,\n\nHope you're well."
    url = "http://example.com/unsubscribe/abc"
    out = cs.append_unsubscribe_footer(body, url)
    assert body.rstrip() in out
    assert "Uitschrijven" in out
    assert url in out


def test_append_unsubscribe_footer_preserves_trailing_whitespace_handling():
    body = "Hi Jan,\n\n\n"
    out = cs.append_unsubscribe_footer(body, "http://x")
    # Should not double up on blank lines
    assert out.count("---") == 1


# ── Tracking token signing ────────────────────────────────────────────────

def test_make_tracking_token_is_deterministic():
    import os
    os.environ["WARMR_API_TOKEN"] = "test-secret"
    t1 = cs._make_tracking_token("c1", "camp1", "lead1", "a@b.com")
    t2 = cs._make_tracking_token("c1", "camp1", "lead1", "a@b.com")
    assert t1 == t2


def test_make_tracking_token_differs_per_input():
    import os
    os.environ["WARMR_API_TOKEN"] = "test-secret"
    t1 = cs._make_tracking_token("c1", "camp1", "lead1", "a@b.com")
    t2 = cs._make_tracking_token("c1", "camp1", "lead2", "a@b.com")
    assert t1 != t2


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
