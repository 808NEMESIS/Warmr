"""
tests/test_open_redirect.py — api.main.track_click open-redirect regression
tests (Fase 2, item 6).

Confirmed bug: an invalid/expired tracking token skipped the `if verified:`
block entirely and fell through to the endpoint's final, unconditional
RedirectResponse — so `/c/anything?url=https://phishing.example` redirected
without the token ever being validated. Fixed by returning 404 whenever the
token doesn't verify, and adding a scheme allowlist as defense-in-depth.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main
from fastapi import HTTPException
from fastapi.responses import RedirectResponse


class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows=None):
        self.rows = rows or []

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, *a, **k):
        return self

    def execute(self):
        return _Exec(self.rows)


class _FakeSb:
    def table(self, name):
        return _Query()


def test_invalid_token_returns_404_not_a_redirect(monkeypatch):
    monkeypatch.setattr(api_main, "_verify_tracking_token", lambda token: None)
    monkeypatch.setattr(api_main, "_supabase", _FakeSb())

    try:
        asyncio.run(api_main.track_click("bogus-token", url="https://phishing.example/", request=None))
        raise AssertionError("Expected HTTPException for an invalid token")
    except HTTPException as exc:
        assert exc.status_code == 404


def test_valid_token_with_disallowed_scheme_returns_404(monkeypatch):
    monkeypatch.setattr(
        api_main, "_verify_tracking_token",
        lambda token: ("client-1", "camp-1", "lead-1", "a@b.com"),
    )
    monkeypatch.setattr(api_main, "_supabase", _FakeSb())
    monkeypatch.setattr(api_main, "_client_is_suspended", lambda cid: False)

    try:
        asyncio.run(api_main.track_click("good-token", url="javascript:alert(1)", request=None))
        raise AssertionError("Expected HTTPException for a disallowed scheme")
    except HTTPException as exc:
        assert exc.status_code == 404


def test_valid_token_with_https_url_redirects(monkeypatch):
    monkeypatch.setattr(
        api_main, "_verify_tracking_token",
        lambda token: ("client-1", "camp-1", "lead-1", "a@b.com"),
    )
    monkeypatch.setattr(api_main, "_supabase", _FakeSb())
    monkeypatch.setattr(api_main, "_client_is_suspended", lambda cid: True)  # suspended: skip tracking, still redirect

    class _FakeRequest:
        client = None
        headers = {}

    result = asyncio.run(api_main.track_click("good-token", url="https://example.com/page", request=_FakeRequest()))
    assert isinstance(result, RedirectResponse)
    assert result.status_code == 302


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
