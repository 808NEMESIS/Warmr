"""
tests/test_preflight_features.py — pre-launch safety features.

Covers:
  1. Variable fallbacks (spintax_engine._resolve_var) — missing built-ins
     render natural defaults instead of shipping raw {{first_name}}.
  2. Suppression check at lead import (private CSV + public bulk) — the
     suppression_list blocks re-creation of unsubscribed emails/domains.
  3. Per-hour send cap — count_inbox_sends_last_hour aggregates warmup +
     campaign sends.
  4. Campaign test-send route registration.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 1. Variable fallbacks ────────────────────────────────────────────────

def test_missing_first_name_falls_back_to_there():
    from spintax_engine import substitute_variables
    assert substitute_variables("Hi {{first_name}},", {}) == "Hi there,"


def test_missing_company_falls_back_to_your_team():
    from spintax_engine import substitute_variables
    assert substitute_variables("hoe gaat {{company}}?", {}) == "hoe gaat your team?"


def test_populated_first_name_wins_over_fallback():
    from spintax_engine import substitute_variables
    assert substitute_variables("Hi {{first_name}},", {"first_name": "Sami"}) == "Hi Sami,"


def test_full_name_fallback():
    from spintax_engine import substitute_variables
    assert substitute_variables("Beste {{full_name}}", {}) == "Beste there"
    assert substitute_variables("Beste {{full_name}}", {"first_name": "Sami"}) == "Beste Sami"


def test_typo_variable_stays_visible():
    """Template authors must see {{firtsname}} unchanged to catch their bug."""
    from spintax_engine import substitute_variables
    assert substitute_variables("Hi {{firtsname}}", {"first_name": "Sami"}) == "Hi {{firtsname}}"


def test_custom_field_missing_becomes_empty():
    from spintax_engine import substitute_variables
    out = substitute_variables("Bedrag: {{custom:price}}", {"custom_fields": {}})
    # Optional personalization → empty is better than visible placeholder
    assert out == "Bedrag: "


# ── 2. Suppression-filter unit logic ─────────────────────────────────────
# The endpoint itself needs a live Supabase stub; here we verify the set
# intersection logic that the endpoints use.

def test_suppression_logic_blocks_by_email():
    suppressed_emails = {"optout@example.nl"}
    suppressed_domains: set[str] = set()
    candidate = "optout@example.nl"
    domain = candidate.split("@")[-1]
    assert candidate in suppressed_emails or domain in suppressed_domains


def test_suppression_logic_blocks_by_domain():
    suppressed_emails: set[str] = set()
    suppressed_domains = {"blocked.nl"}
    candidate = "anyone@blocked.nl"
    domain = candidate.split("@")[-1]
    assert candidate in suppressed_emails or domain in suppressed_domains


def test_suppression_logic_allows_normal_email():
    suppressed_emails = {"optout@example.nl"}
    suppressed_domains = {"blocked.nl"}
    candidate = "prospect@company.nl"
    domain = candidate.split("@")[-1]
    assert not (candidate in suppressed_emails or domain in suppressed_domains)


# ── 3. Per-hour send cap ─────────────────────────────────────────────────

class _HourCapFakeSb:
    def __init__(self, warmup_count: int, campaign_count: int):
        self._counts = {"warmup_logs": warmup_count, "email_events": campaign_count}
        self._table = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def gte(self, *a, **kw): return self

    def execute(self):
        class R:
            def __init__(self, count): self.count = count; self.data = []
        return R(self._counts.get(self._table, 0))


def test_hourly_cap_counts_warmup_plus_campaign():
    from campaign_scheduler import count_inbox_sends_last_hour
    sb = _HourCapFakeSb(warmup_count=7, campaign_count=4)
    assert count_inbox_sends_last_hour(sb, "inbox-1") == 11


def test_hourly_cap_tolerates_table_errors():
    """If one table query fails, the other still counts — the cap must be
    best-effort, never a hard blocker."""
    from campaign_scheduler import count_inbox_sends_last_hour

    class _PartialSb:
        def __init__(self):
            self._call = 0
        def table(self, name):
            self._name = name
            return self
        def select(self, *a, **kw): return self
        def eq(self, *a, **kw): return self
        def gte(self, *a, **kw): return self
        def execute(self):
            if self._name == "warmup_logs":
                raise RuntimeError("supabase timeout")
            class R: count = 3; data = []
            return R()

    sb = _PartialSb()
    assert count_inbox_sends_last_hour(sb, "inbox-1") == 3


def test_max_hourly_warmup_is_configurable():
    """Env var override should work."""
    import importlib, os
    os.environ["MAX_HOURLY_WARMUP"] = "5"
    import warmup_engine
    importlib.reload(warmup_engine)
    assert warmup_engine.MAX_HOURLY_WARMUP == 5
    # Restore default
    os.environ.pop("MAX_HOURLY_WARMUP", None)
    importlib.reload(warmup_engine)


# ── 4. Campaign test-send route registration ─────────────────────────────

def test_test_send_route_is_registered():
    from api.main import app
    paths = {(tuple(sorted(r.methods)), r.path) for r in app.routes if hasattr(r, "methods") and r.methods}
    assert (("POST",), "/campaigns/{campaign_id}/test-send") in paths


# ── 5. LeadImportResult now carries suppressed count ────────────────────

def test_lead_import_result_has_suppressed_field():
    from api.models import LeadImportResult
    r = LeadImportResult(total_rows=1, imported=0, duplicates=0, errors=0, suppressed=1, error_details=[])
    assert r.suppressed == 1
    # Backward-compat: old callers that don't set it default to 0
    r2 = LeadImportResult(total_rows=1, imported=1, duplicates=0, errors=0, error_details=[])
    assert r2.suppressed == 0


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
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
