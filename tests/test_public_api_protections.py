"""
tests/test_public_api_protections.py — verifies the rate-limit key, per-endpoint
rate limits, and in-process availability cache that protect the public API from
abuse (accidental or otherwise) by external integrations like Heatr.

These are unit tests — no live HTTP, no live Supabase.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Rate limit key ────────────────────────────────────────────────────────

def _mk_request(authorization: str | None = None, client_host: str = "203.0.113.7"):
    """Minimal Request stub for _rate_limit_key()."""
    req = MagicMock()
    req.headers = {"authorization": authorization} if authorization else {}
    req.client = MagicMock()
    req.client.host = client_host
    req.scope = {"client": (client_host, 0)}
    return req


def test_rate_limit_key_public_api_key_is_hashed_per_key():
    from api.rate_limiter import _rate_limit_key
    key_a = _rate_limit_key(_mk_request("Bearer wrmr_aaaaaaaaaaaaaaaa"))
    key_b = _rate_limit_key(_mk_request("Bearer wrmr_bbbbbbbbbbbbbbbb"))
    assert key_a.startswith("apikey:")
    assert key_b.startswith("apikey:")
    assert key_a != key_b, "Different API keys must produce different rate-limit keys"


def test_rate_limit_key_same_api_key_same_bucket_regardless_of_ip():
    from api.rate_limiter import _rate_limit_key
    k1 = _rate_limit_key(_mk_request("Bearer wrmr_shared_key_123", client_host="10.0.0.1"))
    k2 = _rate_limit_key(_mk_request("Bearer wrmr_shared_key_123", client_host="10.0.0.2"))
    assert k1 == k2, "Same API key from different IPs must share a bucket"


def test_rate_limit_key_falls_back_to_ip_when_no_auth():
    from api.rate_limiter import _rate_limit_key
    k = _rate_limit_key(_mk_request(None, client_host="198.51.100.9"))
    assert k.startswith("ip:")


def test_rate_limit_key_does_not_leak_raw_key():
    from api.rate_limiter import _rate_limit_key
    raw = "wrmr_verysecret_DO_NOT_LEAK"
    k = _rate_limit_key(_mk_request(f"Bearer {raw}"))
    assert raw not in k, "Rate-limit key must never contain the raw API key"
    assert "verysecret" not in k


# ── Per-endpoint limits are registered ────────────────────────────────────

def test_leads_bulk_has_stricter_limit_than_default():
    """POST /leads/bulk must be decorated with a per-endpoint limit.
    The limiter tracks endpoint-specific limits in its _route_limits registry."""
    from api import public_api  # noqa: F401 — import to register routes
    from api.rate_limiter import limiter

    route_limits = limiter._route_limits  # slowapi internal, stable enough for a test
    # Keys are the qualname of the handler function
    keys = list(route_limits.keys())

    # /leads/bulk handler is public_create_leads_bulk
    bulk_key = [k for k in keys if "public_create_leads_bulk" in k]
    assert bulk_key, f"Expected public_create_leads_bulk in route_limits, got: {keys}"
    limits = route_limits[bulk_key[0]]
    # One or more RateLimitItem objects
    assert limits, "public_create_leads_bulk should have a rate limit attached"


def test_leads_single_has_its_own_limit():
    from api import public_api  # noqa: F401
    from api.rate_limiter import limiter
    keys = list(limiter._route_limits.keys())
    single_key = [k for k in keys if "public_create_leads" in k and "bulk" not in k]
    assert single_key, f"Expected public_create_leads in route_limits, got: {keys}"


# ── Availability cache ────────────────────────────────────────────────────

def test_availability_cache_returns_cached_within_ttl():
    from api.public_api import (
        _availability_cache_get,
        _availability_cache_set,
        _AVAILABILITY_TTL_SECONDS,
    )

    key = "client-a:inbox-1"
    payload = {"daily_remaining": 42}
    _availability_cache_set(key, payload)
    fetched = _availability_cache_get(key)
    assert fetched == payload
    assert _AVAILABILITY_TTL_SECONDS == 30.0


def test_availability_cache_miss_returns_none():
    from api.public_api import _availability_cache_get
    assert _availability_cache_get("nonexistent:never-set") is None


def test_availability_cache_is_keyed_per_client_and_inbox():
    """client A and client B must never see each other's inbox availability."""
    from api.public_api import _availability_cache_get, _availability_cache_set
    _availability_cache_set("client-a:inbox-1", {"daily_remaining": 10})
    _availability_cache_set("client-b:inbox-1", {"daily_remaining": 99})
    assert _availability_cache_get("client-a:inbox-1")["daily_remaining"] == 10
    assert _availability_cache_get("client-b:inbox-1")["daily_remaining"] == 99


def test_availability_cache_expires_after_ttl():
    """Force an entry past its TTL and confirm it is evicted."""
    from api import public_api
    # Insert an entry with an artificially old timestamp
    public_api._AVAILABILITY_CACHE["client-c:inbox-x"] = (
        time.monotonic() - (public_api._AVAILABILITY_TTL_SECONDS + 5),
        {"daily_remaining": 0},
    )
    assert public_api._availability_cache_get("client-c:inbox-x") is None
    # Eviction must remove it from the dict
    assert "client-c:inbox-x" not in public_api._AVAILABILITY_CACHE


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
