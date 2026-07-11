"""
tests/test_security_hardening.py — security hardening features.

  1. utils.url_safety — SSRF protection (private IP rejection, scheme check,
     literal IP handling, DNS resolution failure handling).
  2. spintax_engine — variable-value length cap.
  3. webhook_dispatcher — refuses to deliver to URLs that fail SSRF check.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 1. SSRF guard ────────────────────────────────────────────────────────

def test_https_public_url_is_safe():
    from utils.url_safety import is_url_safe
    # example.com — public IP. Don't network-mock; rely on real DNS.
    assert is_url_safe("https://example.com/hook") is True


def test_http_scheme_rejected_by_default():
    from utils.url_safety import UnsafeUrlError, assert_url_safe
    try:
        assert_url_safe("http://example.com/")
    except UnsafeUrlError as exc:
        assert "scheme" in str(exc).lower()
    else:
        raise AssertionError("Expected http:// to be rejected")


def test_file_scheme_rejected():
    from utils.url_safety import is_url_safe
    assert is_url_safe("file:///etc/passwd") is False


def test_ipv6_loopback_still_rejected():
    """::1 is not in the localhost dev-exception list — must still fail."""
    from utils.url_safety import is_url_safe
    assert is_url_safe("https://[::1]/") is False


# ── Localhost dev exception (added 2026-05-08, gated behind
#    WARMR_URL_ALLOW_HTTP as of Fase 2 item 5) ────────────────────────────
# A scoped carve-out for local Heatr development. Only `localhost` and
# `127.0.0.1` are exempt from the http-scheme + private-IP guards, and only
# when local-dev mode is on. Every other private host stays rejected.

def test_localhost_http_allowed_when_dev_flag_set(monkeypatch):
    """http://localhost:<port> is the canonical local Heatr webhook URL —
    but only when WARMR_URL_ALLOW_HTTP is explicitly enabled."""
    from utils import url_safety
    monkeypatch.setattr(url_safety, "_ALLOW_HTTP", True)
    assert url_safety.is_url_safe("http://localhost:8001/webhooks/warmr") is True


def test_127_0_0_1_http_allowed_when_dev_flag_set(monkeypatch):
    """http://127.0.0.1:<port> mirrors the localhost case."""
    from utils import url_safety
    monkeypatch.setattr(url_safety, "_ALLOW_HTTP", True)
    assert url_safety.is_url_safe("http://127.0.0.1:8001/webhooks/warmr") is True


def test_localhost_http_rejected_without_dev_flag():
    """Regression: previously this carve-out was ungated, so ANY tenant
    could point a production webhook at 127.0.0.1/localhost and reach
    whatever's listening on the server's own loopback interface. With
    WARMR_URL_ALLOW_HTTP unset (the production default), these hosts must
    now be rejected exactly like any other loopback address."""
    from utils.url_safety import is_url_safe
    assert is_url_safe("http://localhost:8001/webhooks/warmr") is False


def test_127_0_0_1_http_rejected_without_dev_flag():
    from utils.url_safety import is_url_safe
    assert is_url_safe("http://127.0.0.1:8001/webhooks/warmr") is False


def test_http_other_host_still_rejected():
    """The localhost carve-out must NOT widen to arbitrary http:// hosts."""
    from utils.url_safety import is_url_safe
    assert is_url_safe("http://example.com/hook") is False


def test_http_subdomain_of_localhost_label_not_implicitly_allowed():
    """A hostname that merely contains 'localhost' as a substring is NOT exempt.
    e.g. 'localhost.attacker.com' must resolve via DNS and go through SSRF
    checks like any other public host."""
    from utils.url_safety import is_url_safe
    # If DNS resolves to a public IP, this might pass the SSRF check; if it
    # doesn't resolve at all, it fails. Either way it must NOT be silently
    # bypassed via the localhost carve-out.
    assert is_url_safe("http://localhost.example.com/") is False


def test_private_ranges_rejected():
    from utils.url_safety import is_url_safe
    for bad in [
        "https://10.0.0.5/x",          # RFC1918 class A
        "https://192.168.1.1/x",       # RFC1918 class C
        "https://172.16.0.1/x",        # RFC1918 class B
        "https://169.254.169.254/x",   # AWS metadata link-local
        "https://0.0.0.0/x",           # unspecified
    ]:
        assert is_url_safe(bad) is False, f"Should have rejected {bad}"


def test_public_literal_ip_accepted():
    from utils.url_safety import is_url_safe
    # 8.8.8.8 — public IP literal, no DNS needed
    assert is_url_safe("https://8.8.8.8/") is True


def test_unresolvable_hostname_rejected():
    """Hostnames that don't resolve cannot be verified — treated as unsafe."""
    from utils.url_safety import is_url_safe
    # this domain has been engineered to never resolve via getaddrinfo
    assert is_url_safe("https://this-host-definitely-does-not-exist-warmr-test.invalid/") is False


def test_empty_url_rejected():
    from utils.url_safety import is_url_safe
    assert is_url_safe("") is False
    assert is_url_safe(None) is False


def test_dns_resolves_to_private_rejected(monkeypatch):
    """A public-looking hostname that resolves to a private IP must be rejected.
    This is the DNS-rebinding attack vector. We monkeypatch getaddrinfo so the
    test is hermetic."""
    from utils import url_safety
    # Mock the address resolver so a public-looking hostname returns a 10.x address
    monkeypatch.setattr(url_safety, "_resolve_addresses", lambda h: ["10.0.0.5"])
    assert url_safety.is_url_safe("https://attacker.example/") is False


# ── 2. Variable length cap ───────────────────────────────────────────────

def test_oversized_first_name_is_truncated():
    """A 10MB first_name would otherwise blow up the email body."""
    from spintax_engine import substitute_variables
    huge = "x" * 50000
    out = substitute_variables("Hi {{first_name}},", {"first_name": huge}, {"language": "en"})
    # Cap at _MAX_VAR_LENGTH (200) plus the surrounding "Hi ,"
    from spintax_engine import _MAX_VAR_LENGTH
    assert len(out) <= _MAX_VAR_LENGTH + 10
    assert out.endswith(",")
    assert out.startswith("Hi ")
    # Truncation marker present
    assert "…" in out


def test_short_first_name_passes_through():
    from spintax_engine import substitute_variables
    out = substitute_variables("Hi {{first_name}},", {"first_name": "Sami"}, {"language": "en"})
    assert out == "Hi Sami,"
    assert "…" not in out


def test_custom_field_truncation_applies():
    from spintax_engine import substitute_variables
    out = substitute_variables(
        "{{custom:summary}}",
        {"custom_fields": {"summary": "y" * 10000}},
        {"language": "en"},
    )
    assert len(out) <= 200
    assert out.endswith("…")


# ── 3. Dispatcher refuses unsafe URLs ────────────────────────────────────

def test_dispatcher_refuses_private_url(monkeypatch):
    """webhook_dispatcher.deliver must short-circuit on unsafe URL."""
    import webhook_dispatcher as wd

    # Build a minimal webhook dict that would otherwise pass through.
    webhook = {
        "id":    "wh-1",
        "url":   "https://10.0.0.5/hook",
        "secret": "plaintext-test-secret",
        "active": True,
    }

    success, status, body = wd.deliver(
        webhook=webhook,
        event_type="lead.replied",
        payload={"hello": "world"},
        attempt_count=1,
    )
    assert success is False
    assert status == 0
    assert "unsafe" in body.lower()


if __name__ == "__main__":
    import inspect, os as _os

    class _Mp:
        def __init__(self):
            self._undo_attr = []
        def setattr(self, t, n, v=None, raising=True):
            self._undo_attr.append((t, n, getattr(t, n, None)))
            setattr(t, n, v)
        def setenv(self, k, v):
            self._undo_attr.append(("__env__", k, _os.environ.get(k)))
            _os.environ[k] = v
        def delenv(self, k, raising=True):
            self._undo_attr.append(("__env__", k, _os.environ.get(k)))
            _os.environ.pop(k, None)
        def undo(self):
            for t, n, v in self._undo_attr:
                if t == "__env__":
                    if v is None: _os.environ.pop(n, None)
                    else: _os.environ[n] = v
                else:
                    setattr(t, n, v)

    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        total += 1
        mp = _Mp() if "monkeypatch" in inspect.signature(fn).parameters else None
        try:
            fn(mp) if mp else fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
        finally:
            if mp: mp.undo()
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
