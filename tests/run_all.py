"""
tests/run_all.py — run all Warmr unit tests and print a summary.

Usage:
    source .venv/bin/activate
    python tests/run_all.py
"""

import importlib.util
import inspect
import os
import sys
from pathlib import Path


class _MiniMonkeypatch:
    """Minimal pytest-monkeypatch emulator for run_all.py.

    Supports the subset of the pytest API used by our tests:
      setenv, delenv, setattr (target, name, value).
    Call .undo() to restore.
    """

    def __init__(self) -> None:
        self._env_undo: list[tuple[str, str | None]] = []
        self._attr_undo: list[tuple[object, str, object]] = []

    def setenv(self, k: str, v: str) -> None:
        self._env_undo.append((k, os.environ.get(k)))
        os.environ[k] = v

    def delenv(self, k: str, raising: bool = True) -> None:
        self._env_undo.append((k, os.environ.get(k)))
        os.environ.pop(k, None)

    def setattr(self, target, name, value=None, raising: bool = True) -> None:
        self._attr_undo.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    def undo(self) -> None:
        for k, v in self._env_undo:
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for t, n, v in self._attr_undo:
            setattr(t, n, v)


TESTS_DIR = Path(__file__).resolve().parent
TEST_MODULES = [
    "test_spintax_engine",
    "test_funnel_engine",
    "test_engagement_scorer",
    "test_suppression",
    "test_heatr_integration",
    "test_bounce_handler",
    "test_secrets_encryption",
    "test_public_api_protections",
    "test_reply_features",
    "test_new_safety_features",
]


def run_module(name: str) -> tuple[int, int]:
    """Return (passed, total)."""
    spec = importlib.util.spec_from_file_location(name, TESTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        print(f"  ERROR: Could not load {name}")
        return 0, 0
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    passed = 0
    total = 0
    print(f"\n{name}")
    print("─" * 60)
    for attr in dir(mod):
        if attr.startswith("test_"):
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            total += 1
            needs_mp = "monkeypatch" in inspect.signature(fn).parameters
            mp = _MiniMonkeypatch() if needs_mp else None
            try:
                if mp is not None:
                    fn(mp)
                else:
                    fn()
                passed += 1
                print(f"  \u2713 {attr}")
            except AssertionError as e:
                print(f"  \u2717 {attr}: {e}")
            except Exception as e:
                print(f"  \u2717 {attr}: {type(e).__name__}: {e}")
            finally:
                if mp is not None:
                    mp.undo()
    print(f"  → {passed}/{total}")
    return passed, total


def main() -> int:
    total_passed = 0
    total_tests = 0
    for mod_name in TEST_MODULES:
        p, t = run_module(mod_name)
        total_passed += p
        total_tests += t

    print("\n" + "=" * 60)
    print(f"TOTAL: {total_passed}/{total_tests} passed")
    print("=" * 60)
    return 0 if total_passed == total_tests else 1


if __name__ == "__main__":
    sys.exit(main())
