"""
scripts/check_service_role_queries.py — static analyzer that flags
service-role Supabase queries on multi-tenant tables that are missing a
`.eq("client_id", …)` (or `.in_("client_id", …)`) filter.

Why this matters
----------------
The service-role Supabase key bypasses Row Level Security. Every read,
update, or delete against a table that has a `client_id` column must
therefore explicitly filter by `client_id` in the query chain — otherwise
one tenant can see or mutate another tenant's data.

How it works
------------
1. Parse `full_schema.sql` to discover every table that has a `client_id`
   column → the MULTI_TENANT set.
2. AST-parse every `.py` file in the repo (excluding .venv, __pycache__,
   build output, etc.).
3. For every `.execute()` call, walk the method chain backwards:
     - find the `.table("…")` anchor
     - record which mutating/reading methods were called
       (select / update / delete / insert / upsert)
     - check whether any `.eq("client_id", …)` or
       `.in_("client_id", …)` appears in the same chain
     - for `.insert(…)` / `.upsert(…)` also accept a literal dict that
       contains a `"client_id"` key as an implicit filter (that's how
       inserts scope themselves)
4. Emit a report. Exit code 1 if there are findings (CI-ready).

Limitations (intentional — documented so CI noise stays low)
------------------------------------------------------------
- Only literal string arguments to `.table(...)` and `.eq(...)` are
  analyzed. Dynamic table names (rare in this codebase) are skipped.
- Method chains that are split across statements
  (`q = sb.table(...); q = q.eq(...); q.execute()`) are NOT tracked.
  Keep chains on one expression.
- `.rpc(...)` calls are listed separately — isolation there depends on
  the function body and cannot be inferred statically.
- Tables without a `client_id` column (e.g. `warmup_logs`, `bounce_log`,
  `reply_inbox`) are considered inbox-scoped and are NOT flagged. Their
  isolation is enforced via join-through-inbox policies at the RLS layer.

Allowlist
---------
Lines immediately preceded by `# service-audit: allow` (or containing the
same marker as a trailing comment on the `.execute()` or `.table()` line)
are treated as reviewed exceptions. Use sparingly — admin endpoints that
intentionally query across tenants are the main legitimate case.

Usage
-----
    python scripts/check_service_role_queries.py            # full report
    python scripts/check_service_role_queries.py --quiet    # findings only
    python scripts/check_service_role_queries.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = REPO_ROOT / "full_schema.sql"

# Directories we never scan
EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "logs", "frontend"}

# Method names that read or mutate data against a table
READ_METHODS = {"select"}
WRITE_METHODS = {"update", "delete", "insert", "upsert"}
ALL_DATA_METHODS = READ_METHODS | WRITE_METHODS

ALLOW_MARKER = "service-audit: allow"


# ─────────────────────────────────────────────────────────────────────
# Schema parsing
# ─────────────────────────────────────────────────────────────────────

def load_multi_tenant_tables(schema_path: Path) -> set[str]:
    """Return the set of table names that have a `client_id` column."""
    if not schema_path.exists():
        raise FileNotFoundError(
            f"Schema file not found: {schema_path}. "
            "The analyzer needs it to know which tables are multi-tenant."
        )
    text = schema_path.read_text(encoding="utf-8")

    tables: set[str] = set()
    # Match every `CREATE TABLE IF NOT EXISTS <name> ( ... );` block
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        name = match.group(1)
        body = match.group(2)
        # Has a `client_id` column definition? Look for it at the start of a line
        # (after whitespace, before a type keyword). A simple word-boundary
        # match on `client_id` inside the body is sufficient here.
        if re.search(r"\bclient_id\b\s+(TEXT|UUID)", body, re.IGNORECASE):
            tables.add(name)
    return tables


# ─────────────────────────────────────────────────────────────────────
# AST traversal
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ChainStep:
    method: str
    args: list[ast.expr]
    lineno: int


@dataclass
class Finding:
    file: str
    lineno: int
    table: str
    ops: list[str]
    reason: str
    source_snippet: str = ""


def _walk_chain(expr: ast.expr) -> list[ChainStep]:
    """
    Given `foo.bar(...).baz(...).qux(...)`, walk backwards and return
    the steps in call order:
        [ChainStep('bar', ...), ChainStep('baz', ...), ChainStep('qux', ...)]
    """
    steps: list[ChainStep] = []
    node: ast.AST = expr
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        steps.append(ChainStep(method=node.func.attr, args=list(node.args), lineno=node.lineno))
        node = node.func.value
    steps.reverse()
    return steps


def _literal_str(node: ast.expr) -> str | None:
    """Return the string value if `node` is a literal string, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dict_contains_client_id(node: ast.expr) -> bool:
    """True if `node` is a literal dict with a 'client_id' key."""
    if not isinstance(node, ast.Dict):
        return False
    for key in node.keys:
        if key is not None and _literal_str(key) == "client_id":
            return True
    return False


def _list_of_dicts_all_have_client_id(node: ast.expr) -> bool:
    """True if `node` is a list of dict literals all containing 'client_id'."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return False
    if not node.elts:
        return False
    return all(_dict_contains_client_id(elt) for elt in node.elts)


def analyze_file(
    path: Path,
    multi_tenant_tables: set[str],
    allow_lines: set[int],
) -> tuple[list[Finding], list[tuple[int, str]]]:
    """
    Return (findings, rpc_calls).

    rpc_calls is a list of (lineno, function_name) tuples — separately
    reported because static analysis can't tell whether a stored
    procedure respects tenant isolation.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return [], []

    findings: list[Finding] = []
    rpc_calls: list[tuple[int, str]] = []
    source_lines = source.splitlines()

    for node in ast.walk(tree):
        # RPC calls: report but don't judge
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rpc"
            and node.args
        ):
            fn = _literal_str(node.args[0]) or "<dynamic>"
            rpc_calls.append((node.lineno, fn))
            continue

        # We only care about `.execute()` chain heads
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ):
            continue

        if node.lineno in allow_lines:
            continue

        chain = _walk_chain(node.func.value)
        table_name: str | None = None
        table_lineno: int = node.lineno
        ops: list[str] = []
        has_client_filter = False

        for step in chain:
            if step.method == "table" and step.args:
                name = _literal_str(step.args[0])
                if name:
                    table_name = name
                    table_lineno = step.lineno
            elif step.method in ALL_DATA_METHODS:
                ops.append(step.method)
                # Insert/upsert: check the payload itself for client_id
                if step.method in ("insert", "upsert") and step.args:
                    payload = step.args[0]
                    if _dict_contains_client_id(payload) or _list_of_dicts_all_have_client_id(payload):
                        has_client_filter = True
            elif step.method in ("eq", "in_", "match"):
                if step.args:
                    key = _literal_str(step.args[0])
                    if key == "client_id":
                        has_client_filter = True
                    elif step.method == "match" and isinstance(step.args[0], ast.Dict):
                        # .match({"client_id": ..., ...})
                        if _dict_contains_client_id(step.args[0]):
                            has_client_filter = True

        if table_name is None or table_name not in multi_tenant_tables:
            continue
        if not ops:
            continue
        if has_client_filter:
            continue
        if table_lineno in allow_lines or node.lineno in allow_lines:
            continue

        snippet = source_lines[node.lineno - 1].strip() if 0 <= node.lineno - 1 < len(source_lines) else ""
        findings.append(Finding(
            file=str(path.relative_to(REPO_ROOT)),
            lineno=node.lineno,
            table=table_name,
            ops=sorted(set(ops)),
            reason="no .eq('client_id', …) / .in_('client_id', …) / insert payload with client_id key",
            source_snippet=snippet,
        ))

    return findings, rpc_calls


def _collect_allow_lines(path: Path) -> set[int]:
    """Return the set of line numbers marked with `# service-audit: allow`."""
    allowed: set[int] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return allowed
    for i, line in enumerate(lines, start=1):
        if ALLOW_MARKER in line:
            allowed.add(i)
            # Also treat the NEXT non-blank line as allowed — lets you put
            # the marker on the line above an `.execute()`.
            for j in range(i + 1, min(i + 5, len(lines) + 1)):
                if lines[j - 1].strip():
                    allowed.add(j)
                    break
    return allowed


# ─────────────────────────────────────────────────────────────────────
# File discovery + main
# ─────────────────────────────────────────────────────────────────────

def iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--quiet", action="store_true", help="Findings only; no summary.")
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repo root to scan (default: repo).")
    args = parser.parse_args()

    multi_tenant = load_multi_tenant_tables(SCHEMA_FILE)

    all_findings: list[Finding] = []
    all_rpcs: list[tuple[str, int, str]] = []
    files_scanned = 0

    for py_file in iter_python_files(args.root):
        files_scanned += 1
        allow_lines = _collect_allow_lines(py_file)
        findings, rpcs = analyze_file(py_file, multi_tenant, allow_lines)
        all_findings.extend(findings)
        for lineno, fn in rpcs:
            all_rpcs.append((str(py_file.relative_to(REPO_ROOT)), lineno, fn))

    if args.json:
        print(json.dumps({
            "files_scanned": files_scanned,
            "multi_tenant_tables": sorted(multi_tenant),
            "findings": [f.__dict__ for f in all_findings],
            "rpc_calls": [{"file": f, "lineno": ln, "function": fn} for f, ln, fn in all_rpcs],
        }, indent=2))
        return 1 if all_findings else 0

    if not args.quiet:
        print(f"Scanned {files_scanned} Python files.")
        print(f"Multi-tenant tables ({len(multi_tenant)}): {', '.join(sorted(multi_tenant))}")
        print()

    if all_findings:
        print(f"⚠  {len(all_findings)} query/queries on multi-tenant tables without client_id filter:\n")
        # Group by file for readability
        by_file: dict[str, list[Finding]] = {}
        for f in all_findings:
            by_file.setdefault(f.file, []).append(f)
        for file_path, items in sorted(by_file.items()):
            print(f"  {file_path}")
            for f in items:
                ops = "/".join(f.ops)
                print(f"    line {f.lineno:>5}  {ops:<20}  {f.table}")
                if f.source_snippet:
                    print(f"                {f.source_snippet[:100]}")
            print()
    elif not args.quiet:
        print("✓ No service-role queries without client_id filter detected.")
        print()

    if all_rpcs and not args.quiet:
        print(f"ℹ  {len(all_rpcs)} .rpc(…) call(s) — review manually (static analysis cannot verify isolation):")
        for file_path, lineno, fn in sorted(all_rpcs):
            print(f"    {file_path}:{lineno}  rpc({fn!r})")
        print()

    return 1 if all_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
