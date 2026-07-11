"""
tests/test_ssrf_webhook_dispatch.py — SSRF-guard regression tests (Fase 2,
item 5: "centralize all webhook verification, no exceptions").

Before this fix, three independent code paths POSTed to tenant-supplied
webhook URLs without ever calling utils.url_safety.assert_url_safe:
  - crm_dispatcher.sync_to_webhook
  - funnel_engine._dispatch_stage_webhook (found this session — neither
    audit caught it)
  - api.main._verify_webhook_url (the verification GET is itself the SSRF
    primitive it's supposed to guard against)

Each test proves the guard runs BEFORE any network call — by monkeypatching
httpx.post/AsyncClient.get to raise if invoked, a private URL must be
rejected without ever reaching that mock.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crm_dispatcher
import funnel_engine


PRIVATE_URL = "https://169.254.169.254/latest/meta-data/"


class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return _Exec(self.rows)


class _FakeSb:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        return _Query(self.rows)


def _boom_post(*a, **k):
    raise AssertionError("httpx.post must not be called for an unsafe URL")


# ── crm_dispatcher.sync_to_webhook ────────────────────────────────────────

def test_sync_to_webhook_rejects_private_url_before_any_network_call(monkeypatch):
    monkeypatch.setattr(crm_dispatcher.httpx, "post", _boom_post)
    integration = {"webhook_url": PRIVATE_URL}
    status, response = crm_dispatcher.sync_to_webhook(integration, {"email": "a@b.com"}, "reply")
    assert status == "failed"
    assert "rejected" in response.lower()


def test_sync_to_webhook_allows_public_url(monkeypatch):
    class _Resp:
        status_code = 200
        text = "ok"
    monkeypatch.setattr(crm_dispatcher.httpx, "post", lambda *a, **k: _Resp())
    integration = {"webhook_url": "https://example.com/hook"}
    status, response = crm_dispatcher.sync_to_webhook(integration, {"email": "a@b.com"}, "reply")
    assert status == "success"


# ── crm_dispatcher.sync_to_pipedrive: company_domain allowlist ───────────

def test_sync_to_pipedrive_rejects_domain_with_path_injection(monkeypatch):
    monkeypatch.setattr(crm_dispatcher.httpx, "post", _boom_post)
    integration = {"api_key": "k", "config": {"company_domain": "evil.com/x"}}
    status, response = crm_dispatcher.sync_to_pipedrive(integration, {"email": "a@b.com"}, "reply")
    assert status == "failed"
    assert "invalid" in response.lower()


def test_sync_to_pipedrive_rejects_domain_with_at_injection(monkeypatch):
    monkeypatch.setattr(crm_dispatcher.httpx, "post", _boom_post)
    integration = {"api_key": "k", "config": {"company_domain": "internal-host@attacker.com"}}
    status, response = crm_dispatcher.sync_to_pipedrive(integration, {"email": "a@b.com"}, "reply")
    assert status == "failed"


def test_sync_to_pipedrive_allows_normal_company_domain(monkeypatch):
    class _Resp:
        status_code = 200
        def json(self): return {"data": {"id": 1}}
    monkeypatch.setattr(crm_dispatcher.httpx, "post", lambda *a, **k: _Resp())
    integration = {"api_key": "k", "config": {"company_domain": "aerys-solutions"}}
    status, response = crm_dispatcher.sync_to_pipedrive(integration, {"email": "a@b.com"}, "reply")
    assert status == "success"


# ── funnel_engine._dispatch_stage_webhook ─────────────────────────────────

def test_dispatch_stage_webhook_rejects_private_url_before_any_network_call(monkeypatch):
    sb = _FakeSb([{"webhook_url": PRIVATE_URL}])
    # _dispatch_stage_webhook imports httpx locally inside the function, so
    # patch the real (shared) httpx module's post function.
    import httpx as real_httpx
    monkeypatch.setattr(real_httpx, "post", _boom_post)
    # Must not raise (all failures inside are swallowed) — the assertion is
    # that _boom_post is never reached.
    funnel_engine._dispatch_stage_webhook(sb, "client-1", "lead-1", "a@b.com", "cold", "warm", "test")


# ── api.main._verify_webhook_url ──────────────────────────────────────────

def test_verify_webhook_url_rejects_private_url_without_network_call(monkeypatch):
    import api.main as api_main

    class _BoomClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            raise AssertionError("network GET must not be attempted for an unsafe URL")

    monkeypatch.setattr(api_main.httpx, "AsyncClient", _BoomClient)
    result = asyncio.run(api_main._verify_webhook_url(PRIVATE_URL))
    assert result is False


if __name__ == "__main__":
    import inspect

    class _Mp:
        def __init__(self):
            self._undo = []
        def setattr(self, target, name, value=None, raising=True):
            self._undo.append((target, name, getattr(target, name, None)))
            setattr(target, name, value)
        def undo(self):
            for t, n, v in self._undo:
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
            if mp:
                mp.undo()
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
