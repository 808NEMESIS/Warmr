"""
scripts/check_atomic_release.py — release-discipline gate (Fase 0.3).

Enforces the rule: code that depends on a database RPC function may never
be merged without the migration that defines it landing in the same commit
history. Concretely, every `supabase.rpc("name", ...)` call anywhere in the
tracked Python codebase must have a matching
`CREATE [OR REPLACE] FUNCTION name` in a TRACKED api/*.sql file.

This checks git-tracked files (`git ls-files`), not the working tree — a CI
checkout only ever contains committed content, so this is exactly the state
a merge would produce. Re-validates the whole repo every run (not a diff),
so it also catches a migration being reverted/deleted after the fact while
the calling code stays behind.

Background: this exact gap — utils/job_lock.py and warmup_engine.py calling
RPCs (try_acquire_job_lock, increment_daily_sent, get_daily_api_spend)
defined only in an untracked, never-applied
api/critical5_critical6_migration.sql — caused a live production regression:
job_lock silently failed open and daily_reset threw on every run. See
WARMR_ENTERPRISE_AUDIT_V2_Q3_2026.md.

Exit codes: 0 = every RPC call has a backing tracked migration. 1 = at
least one RPC call references an undefined function.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

RPC_CALL_RE = re.compile(r'\.rpc\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']')
SQL_FUNC_DEF_RE = re.compile(
    r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE,
)


def find_rpc_calls_in_text(text: str) -> set[str]:
    """Pure function: every RPC name called via .rpc("name") in `text`.

    Matches against the whole text, not line-by-line — supabase-py calls
    are routinely wrapped, e.g. `.rpc(\n    "name", {...}\n)`, and `\s` in
    RPC_CALL_RE already spans newlines, so a per-line scan would silently
    miss those (as it did for utils/cost_tracker.py's real call).
    """
    return {m.group(1) for m in RPC_CALL_RE.finditer(text)}


def find_rpc_calls_with_lines(text: str) -> list[tuple[str, int]]:
    """Every (rpc_name, 1-indexed_line_number) match in `text`."""
    return [
        (m.group(1), text.count("\n", 0, m.start()) + 1)
        for m in RPC_CALL_RE.finditer(text)
    ]


def find_rpc_defs_in_text(text: str) -> set[str]:
    """Pure function: every function name defined via CREATE [OR REPLACE]
    FUNCTION in `text`."""
    return {m.group(1) for m in SQL_FUNC_DEF_RE.finditer(text)}


def _repo_root() -> Path:
    """The git repo root for the CURRENT working directory (not this script's
    own location) — makes the tool testable against any repo, and correct
    when invoked from CI's checkout of a possibly-different repo layout."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(out)


def _tracked_files(repo_root: Path) -> list[str]:
    """All git-tracked file paths, relative to repo_root."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.splitlines() if p]


def _read(repo_root: Path, path: str) -> str:
    return (repo_root / path).read_text(encoding="utf-8", errors="ignore")


def defined_rpc_functions(repo_root: Path, tracked: list[str]) -> set[str]:
    names: set[str] = set()
    for path in tracked:
        if path.startswith("api/") and path.endswith(".sql"):
            names |= find_rpc_defs_in_text(_read(repo_root, path))
    return names


def called_rpc_functions(repo_root: Path, tracked: list[str]) -> dict[str, list[str]]:
    """Map of RPC name -> ["file:line", ...] across all tracked .py files."""
    calls: dict[str, list[str]] = {}
    for path in tracked:
        if not path.endswith(".py"):
            continue
        text = _read(repo_root, path)
        for name, line_no in find_rpc_calls_with_lines(text):
            calls.setdefault(name, []).append(f"{path}:{line_no}")
    return calls


def main() -> int:
    repo_root = _repo_root()
    tracked = _tracked_files(repo_root)
    defined = defined_rpc_functions(repo_root, tracked)
    called = called_rpc_functions(repo_root, tracked)

    missing = {name: sites for name, sites in called.items() if name not in defined}

    if not missing:
        print(
            f"check_atomic_release: OK — {len(called)} RPC call(s) checked, "
            f"all backed by a tracked migration."
        )
        return 0

    print("check_atomic_release: FAILED")
    print()
    print(
        "The following RPC(s) are called from tracked code but are NOT defined "
        "in any tracked api/*.sql migration file. Code that depends on a "
        "migration may never be merged separately from it (Fase 0.3 rule)."
    )
    print()
    for name, sites in sorted(missing.items()):
        print(f'  rpc("{name}") called at:')
        for site in sites:
            print(f"    - {site}")
        print(
            f"    but no 'CREATE [OR REPLACE] FUNCTION {name}' found in any "
            f"tracked api/*.sql file."
        )
        print()
    print(
        "Fix: commit the migration that defines these function(s) in the same "
        "commit/PR as this code (ATOMIC RELEASE), or revert the code."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
