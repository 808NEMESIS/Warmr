"""
tests/test_pre_launch_hardening.py — pre-launch hardening features.

  1. warmup_engine.resolve_sender_name + load_client_settings + append_signature
  2. campaign_scheduler.is_email_hard_bounced
  3. /health endpoint shape (route registered)
  4. imap_processor SPAM_RESCUE_HOURLY_CAP env override
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 1. warmup_engine helpers ─────────────────────────────────────────────

def test_resolve_sender_name_prefers_settings():
    from warmup_engine import resolve_sender_name
    out = resolve_sender_name("info@aeryssolution.nl", {"sender_name": "Sami Jansema"})
    assert out == "Sami Jansema"


def test_resolve_sender_name_falls_back_to_local_part():
    from warmup_engine import resolve_sender_name
    out = resolve_sender_name("info.support@aeryssolution.nl", {})
    # extract_display_name turns local-part into title case
    assert "Info" in out


def test_resolve_sender_name_empty_settings_string_falls_back():
    """An empty/whitespace sender_name should NOT win over the auto-derived name."""
    from warmup_engine import resolve_sender_name
    out = resolve_sender_name("info@aeryssolution.nl", {"sender_name": "   "})
    assert out != "   "


def test_append_signature_idempotent():
    """A body that already contains the signature does not get a second copy."""
    from warmup_engine import append_signature
    sig = "— Sami\nAerys"
    body = "Hi there\n\nGreat chat.\n\n" + sig
    out = append_signature(body, {"email_signature": sig})
    assert out.count("Aerys") == 1


def test_append_signature_appends_when_missing():
    from warmup_engine import append_signature
    out = append_signature("Hi there", {"email_signature": "— Sami"})
    assert out.endswith("— Sami")
    assert "Hi there" in out


def test_append_signature_noop_without_setting():
    from warmup_engine import append_signature
    out = append_signature("Hi there", {})
    assert out == "Hi there"


# ── 2. campaign_scheduler.is_email_hard_bounced ──────────────────────────

class _FakeBounceSb:
    def __init__(self, hits: list[dict]):
        self._hits = hits
        self._table = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def in_(self, *a, **kw): return self
    def limit(self, *a, **kw): return self

    def execute(self):
        class R:
            def __init__(self, d): self.data = d
        return R(self._hits if self._table == "bounce_log" else [])


def test_hard_bounce_blocks_known_bad_email():
    from campaign_scheduler import is_email_hard_bounced
    sb = _FakeBounceSb([{"id": "b1"}])
    assert is_email_hard_bounced(sb, "dead@example.nl") is True


def test_hard_bounce_allows_clean_email():
    from campaign_scheduler import is_email_hard_bounced
    sb = _FakeBounceSb([])
    assert is_email_hard_bounced(sb, "fresh@example.nl") is False


def test_hard_bounce_returns_false_on_db_error():
    """DB errors must not block sends — daily caps + bounce_handler are fallbacks."""
    from campaign_scheduler import is_email_hard_bounced

    class _Broken:
        def table(self, *a, **kw): raise RuntimeError("supabase down")

    assert is_email_hard_bounced(_Broken(), "x@y.nl") is False


def test_hard_bounce_empty_email_returns_false():
    from campaign_scheduler import is_email_hard_bounced
    assert is_email_hard_bounced(_FakeBounceSb([]), "") is False


# ── 3. /health endpoint ──────────────────────────────────────────────────

def test_health_endpoint_registered():
    from api.main import app
    routes = {(tuple(sorted(r.methods)), r.path) for r in app.routes if hasattr(r, "methods") and r.methods}
    assert (("GET",), "/health") in routes


# ── 4. spam-rescue cap ───────────────────────────────────────────────────

def test_spam_rescue_cap_is_env_configurable():
    import os, importlib
    os.environ["WARMR_SPAM_RESCUE_HOURLY_CAP"] = "9"
    import imap_processor
    importlib.reload(imap_processor)
    assert imap_processor.SPAM_RESCUE_HOURLY_CAP == 9
    os.environ.pop("WARMR_SPAM_RESCUE_HOURLY_CAP", None)
    importlib.reload(imap_processor)


def test_spam_rescue_default_cap_is_5():
    import os, importlib
    os.environ.pop("WARMR_SPAM_RESCUE_HOURLY_CAP", None)
    import imap_processor
    importlib.reload(imap_processor)
    assert imap_processor.SPAM_RESCUE_HOURLY_CAP == 5


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        total += 1
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
