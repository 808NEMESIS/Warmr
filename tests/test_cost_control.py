"""
tests/test_cost_control.py — Critical 6 (cost control) regression tests.

Covers:
  - get_daily_spend uses the get_daily_api_spend aggregate RPC, not a full
    table scan summed in Python.
  - A budget-exceeded warmup send falls back to a template (no exception,
    email still produced, zero live LLM).
  - The template bank renders valid, name-substituted, brace-free content.

Style matches the repo: top-level test_* functions, hand-rolled fakes,
runnable via `python tests/run_all.py` or pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils.cost_tracker as ct
import warmup_engine as we
from utils.cost_tracker import BudgetExceededError


# ── Fakes ────────────────────────────────────────────────────────────────

class _Exec:
    def __init__(self, data):
        self.data = data


class RpcRecordingSupabase:
    """Records rpc() calls and returns a canned scalar for get_daily_api_spend."""

    def __init__(self, aggregate_value=0.0):
        self.aggregate_value = aggregate_value
        self.rpc_calls = []
        self.select_calls = 0

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, params))
        val = self.aggregate_value if name == "get_daily_api_spend" else None
        return _RpcBuilder(_Exec(val))

    def table(self, name):
        # If get_daily_spend still full-scans, it will call .table().select();
        # count that so the test can assert it does NOT happen.
        self.select_calls += 1
        return _TableBuilder()


class _RpcBuilder:
    def __init__(self, exec_result):
        self._exec = exec_result

    def execute(self):
        return self._exec


class _TableBuilder:
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): return _Exec([])


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50


class _FakeContentBlock:
    def __init__(self, text): self.text = text


class _FakeMessage:
    def __init__(self, text):
        self.content = [_FakeContentBlock(text)]
        self.usage = _FakeUsage()


class ExplodingClaude:
    """A Claude client whose messages.create must never be called in these tests."""
    class messages:
        @staticmethod
        def create(**kwargs):
            raise AssertionError("Live Claude call made when it must not be")


# ── get_daily_spend uses the aggregate RPC, not a full scan ──────────────

def test_get_daily_spend_uses_aggregate_rpc_not_full_scan():
    sb = RpcRecordingSupabase(aggregate_value=1.2345)
    spend = ct.get_daily_spend(sb, client_id="client-abc")

    assert spend == 1.2345, f"expected aggregate value, got {spend}"
    # Exactly one rpc call, to the aggregate function, scoped to the client.
    assert sb.rpc_calls == [("get_daily_api_spend", {"p_client": "client-abc"})], sb.rpc_calls
    # And it must NOT have fallen back to a full-table select scan.
    assert sb.select_calls == 0, "get_daily_spend still full-scans api_cost_log"


def test_get_daily_spend_no_client_passes_null():
    sb = RpcRecordingSupabase(aggregate_value=0.0)
    spend = ct.get_daily_spend(sb, client_id=None)
    assert spend == 0.0
    assert sb.rpc_calls == [("get_daily_api_spend", {"p_client": None})], sb.rpc_calls


# ── Budget exceeded → warmup content degrades to a template ──────────────

def test_budget_exceeded_falls_back_to_template(monkeypatch):
    """
    When the daily budget is exhausted, generate_email_content must NOT raise
    and must still return a usable (subject, body) rendered from templates,
    without hitting the live model.
    """
    # Force the "would call live model" branch to raise BudgetExceededError.
    def _raise_budget(*a, **k):
        raise BudgetExceededError("Daily API budget exhausted (test).")

    monkeypatch.setattr(we, "tracked_claude_call", _raise_budget, raising=False)
    # Force sampling to choose the live path so the failover is exercised.
    monkeypatch.setenv("WARMUP_LLM_SAMPLE_PCT", "100")

    sb = RpcRecordingSupabase()
    subject, body = we.generate_email_content(
        ExplodingClaude(),          # would explode on any create()
        sender_name="Sanne",
        recipient_name="Tom",
        supabase=sb,
        client_id="client-abc",
        inbox_id="inbox-1",
    )

    assert subject and subject.strip(), "empty subject after budget fallback"
    assert body and body.strip(), "empty body after budget fallback"
    assert "Tom" in body, "recipient name not substituted in template fallback"
    assert "{" not in subject and "{" not in body, "unresolved spintax braces"


def test_zero_sample_pct_never_calls_live_model(monkeypatch):
    """With WARMUP_LLM_SAMPLE_PCT=0 the engine is templates-only, no LLM."""
    monkeypatch.setenv("WARMUP_LLM_SAMPLE_PCT", "0")

    def _explode(*a, **k):
        raise AssertionError("tracked_claude_call invoked at 0% sample")

    monkeypatch.setattr(we, "tracked_claude_call", _explode, raising=False)

    subject, body = we.generate_email_content(
        ExplodingClaude(), sender_name="Sanne", recipient_name="Tom",
        supabase=RpcRecordingSupabase(), client_id="c1", inbox_id="i1",
    )
    assert subject.strip() and body.strip()


# ── Template bank sanity ─────────────────────────────────────────────────

def test_template_bank_renders_all_languages():
    from warmup_templates import render_warmup_email
    for lang in ("nl", "en", "fr"):
        s, b = render_warmup_email(lang, "Sanne", "Tom")
        assert s.strip() and b.strip()
        assert "Tom" in b and "Sanne" in b
        assert "{" not in s and "{" not in b


if __name__ == "__main__":
    # Minimal standalone runner (mirrors tests/run_all.py behaviour).
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "monkeypatch" in inspect.signature(fn).parameters:
                import pytest  # type: ignore
                pytest.main([__file__])
                break
            fn()
            print("ok", name)
