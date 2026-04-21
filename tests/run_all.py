"""
tests/run_all.py — run all Warmr unit tests and print a summary.

Usage:
    source .venv/bin/activate
    python tests/run_all.py
"""

import importlib.util
import sys
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
TEST_MODULES = [
    "test_spintax_engine",
    "test_funnel_engine",
    "test_engagement_scorer",
    "test_suppression",
    "test_heatr_integration",
    "test_bounce_handler",
    "test_secrets_encryption",
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
            try:
                fn()
                passed += 1
                print(f"  \u2713 {attr}")
            except AssertionError as e:
                print(f"  \u2717 {attr}: {e}")
            except Exception as e:
                print(f"  \u2717 {attr}: {type(e).__name__}: {e}")
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
