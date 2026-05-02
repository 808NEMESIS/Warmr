"""
scripts/preflight_check.py — single-command go/no-go audit of a Warmr install.

Runs every check from the manual audit checklist:
  1. Git working tree state
  2. Pyflakes (excluding .venv + live smoke tests)
  3. Test suite (tests/run_all.py)
  4. App boot — does api.main:app import without errors
  5. .env presence + critical variables (Supabase, Anthropic, MASTER_KEY,
     at least one inbox)
  6. Live /health probe (Supabase reachability + Claude key + SMTP creds)
  7. Configured inboxes + warmup network count
  8. DNS for each configured sending domain (SPF / DKIM / DMARC / MX)
  9. Migration files present in api/

Output uses three states per check:
  ✓  green   passed
  ⚠  yellow  passed but worth knowing (e.g. only 5 warmup network accounts)
  ✗  red     blocker before going live

Exit code: 0 if no reds, 1 otherwise. Suitable for CI / pre-deploy gate.

Usage:
    python scripts/preflight_check.py
    python scripts/preflight_check.py --skip dns          # skip slow checks
    python scripts/preflight_check.py --json              # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent

GREEN  = "✓"
YELLOW = "⚠"
RED    = "✗"


# ─────────────────────────────────────────────────────────────────────
# Result model
# ─────────────────────────────────────────────────────────────────────

class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = GREEN
        self.lines: list[str] = []

    def green(self, msg: str)  -> None: self.lines.append(f"  {GREEN} {msg}")
    def yellow(self, msg: str) -> None:
        self.lines.append(f"  {YELLOW} {msg}")
        if self.status == GREEN:
            self.status = YELLOW
    def red(self, msg: str) -> None:
        self.lines.append(f"  {RED} {msg}")
        self.status = RED

    def render(self) -> str:
        head = f"{self.status} {self.name}"
        if not self.lines:
            return head
        return head + "\n" + "\n".join(self.lines)


# ─────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────

def check_git(c: Check) -> None:
    try:
        out = subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO_ROOT, text=True)
        if out.strip():
            files = [l for l in out.splitlines() if l.strip()]
            c.yellow(f"{len(files)} uncommitted file(s) — review before deploy")
            for f in files[:5]:
                c.yellow(f"  {f}")
        else:
            c.green("working tree clean")

        ahead = subprocess.check_output(
            ["git", "rev-list", "--count", "@{u}..HEAD"], cwd=REPO_ROOT, text=True,
        ).strip()
        behind = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD..@{u}"], cwd=REPO_ROOT, text=True,
        ).strip()
        if int(ahead) > 0:
            c.yellow(f"{ahead} commit(s) ahead of origin (not yet pushed)")
        if int(behind) > 0:
            c.yellow(f"{behind} commit(s) behind origin")
        if int(ahead) == 0 and int(behind) == 0:
            c.green("synced with origin")
    except subprocess.CalledProcessError as exc:
        c.red(f"git check failed: {exc}")


def check_pyflakes(c: Check) -> None:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pyflakes", "."],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        c.red("pyflakes not installed (pip install pyflakes)")
        return
    except subprocess.TimeoutExpired:
        c.red("pyflakes timed out")
        return

    findings = [
        line for line in (proc.stdout + proc.stderr).splitlines()
        if line.strip()
        and "/.venv/" not in line
        and "test_supabase_connection" not in line
        and "test_smtp_connection" not in line
        and "test_imap_connection" not in line
        and "test_claude_api" not in line
        and ("imported but unused" in line or "never used" in line or "redefinition" in line)
    ]
    if not findings:
        c.green("0 findings (excluding .venv + live smoke tests)")
    else:
        c.red(f"{len(findings)} finding(s)")
        for f in findings[:5]:
            c.red(f"  {f}")


def check_tests(c: Check) -> None:
    try:
        proc = subprocess.run(
            [sys.executable, "tests/run_all.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        c.red("test suite timed out (>180s)")
        return

    output = proc.stdout + proc.stderr
    match = re.search(r"TOTAL:\s*(\d+)/(\d+)\s*passed", output)
    if not match:
        c.red("could not parse test output")
        return
    passed, total = int(match.group(1)), int(match.group(2))
    if passed == total:
        c.green(f"{passed}/{total} passed")
    else:
        c.red(f"{passed}/{total} passed — {total - passed} failure(s)")


def check_app_boot(c: Check) -> None:
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "from api.main import app; "
             "print(sum(1 for r in app.routes if hasattr(r,'methods') and r.methods))"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        c.red("import timed out")
        return
    if proc.returncode != 0:
        c.red("app failed to import")
        for line in (proc.stderr or "").splitlines()[:5]:
            c.red(f"  {line}")
        return
    try:
        n = int(proc.stdout.strip())
        c.green(f"{n} routes registered, no import errors")
    except ValueError:
        c.red(f"unexpected output: {proc.stdout[:200]}")


def _read_dotenv() -> dict[str, str]:
    """Lightweight .env reader (no python-dotenv import to keep this script standalone)."""
    env: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def check_env(c: Check) -> dict:
    env = _read_dotenv()
    if not env:
        c.red(".env missing — copy .env.example and fill it in")
        return {}

    # Critical: app cannot run without these
    for k in ("SUPABASE_URL", "SUPABASE_KEY", "ANTHROPIC_API_KEY"):
        if not env.get(k):
            c.red(f"{k} not set")
        else:
            c.green(f"{k}: set")

    # Critical-once-you-have-webhooks: needed for secret encryption
    mk = env.get("WARMR_MASTER_KEY", "")
    if not mk:
        c.red("WARMR_MASTER_KEY not set — webhook + CRM secrets will be plaintext")
    elif len(mk) < 32:
        c.yellow(f"WARMR_MASTER_KEY only {len(mk)} chars — recommend 64 hex (openssl rand -hex 32)")
    else:
        c.green(f"WARMR_MASTER_KEY set ({len(mk)} chars)")

    # Inbox count
    inboxes = sum(1 for k in env if re.match(r"^INBOX_\d+_EMAIL$", k) and env[k])
    if inboxes == 0:
        c.red("no inboxes configured (INBOX_1_EMAIL, …)")
    elif inboxes < 2:
        c.yellow(f"only {inboxes} inbox configured — recommend 2+ for redundancy")
    else:
        c.green(f"{inboxes} inbox(es) configured")

    # Warmup network count
    wn = sum(1 for k in env if re.match(r"^WARMUP_NETWORK_\d+_EMAIL$", k) and env[k])
    if wn == 0:
        c.red("no warmup network accounts (WARMUP_NETWORK_1_EMAIL, …)")
    elif wn < 15:
        c.yellow(f"{wn} warmup network accounts — CLAUDE.md recommends 20-30")
    else:
        c.green(f"{wn} warmup network accounts")

    return env


def check_health_endpoint(c: Check) -> None:
    """Run the in-process /health probe."""
    try:
        # Use subprocess so app import errors surface here too
        code = (
            "import asyncio, json, sys; "
            "from api.main import health_check; "
            "r = asyncio.run(health_check()); "
            "print(json.dumps(r))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            c.red(f"/health probe failed: {(proc.stderr or '')[:200]}")
            return
        # Filter out log lines — find the JSON line
        json_line = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                json_line = line
                break
        if not json_line:
            c.red(f"unexpected /health output: {proc.stdout[:200]}")
            return
        result = json.loads(json_line)
        status = result.get("status")
        checks = result.get("checks", {})
        if status == "ok":
            c.green(f"all checks ok: {', '.join(f'{k}={v}' for k,v in checks.items())}")
        elif status == "degraded":
            c.yellow(f"degraded: {checks}")
        else:
            c.red(f"unhealthy: {checks}")
    except subprocess.TimeoutExpired:
        c.red("/health probe timed out (Supabase unreachable?)")
    except Exception as exc:
        c.red(f"/health probe error: {exc}")


def check_dns(c: Check, env: dict) -> None:
    """Verify SPF / DKIM / DMARC / MX for each configured INBOX_N_DOMAIN."""
    domains: set[str] = set()
    for k, v in env.items():
        m = re.match(r"^INBOX_\d+_DOMAIN$", k)
        if m and v:
            domains.add(v)
        # Also derive from email if no domain set
        m = re.match(r"^INBOX_(\d+)_EMAIL$", k)
        if m and v and "@" in v:
            domains.add(v.split("@")[-1])

    if not domains:
        c.yellow("no sending domains configured")
        return

    try:
        import dns.resolver  # type: ignore
    except ImportError:
        c.yellow("dnspython not installed — skipping DNS check")
        return

    for domain in sorted(domains):
        ok = []
        bad = []
        # SPF
        try:
            txt_records = dns.resolver.resolve(domain, "TXT", lifetime=5)
            spf = next((r for r in txt_records if "v=spf1" in str(r)), None)
            if spf:
                ok.append("SPF")
            else:
                bad.append("SPF")
        except Exception:
            bad.append("SPF")
        # DKIM (google selector — assumes Workspace; not all providers use this)
        try:
            dns.resolver.resolve(f"google._domainkey.{domain}", "TXT", lifetime=5)
            ok.append("DKIM(google)")
        except Exception:
            bad.append("DKIM(google)")
        # DMARC
        try:
            txt = dns.resolver.resolve(f"_dmarc.{domain}", "TXT", lifetime=5)
            dmarc = next((str(r) for r in txt if "v=DMARC1" in str(r)), "")
            if "p=reject" in dmarc:
                ok.append("DMARC(reject)")
            elif "p=quarantine" in dmarc:
                ok.append("DMARC(quarantine)")
            elif "p=none" in dmarc:
                ok.append("DMARC(none)")
            else:
                bad.append("DMARC")
        except Exception:
            bad.append("DMARC")
        # MX
        try:
            dns.resolver.resolve(domain, "MX", lifetime=5)
            ok.append("MX")
        except Exception:
            bad.append("MX")

        if not bad:
            c.green(f"{domain}: {', '.join(ok)}")
        elif len(bad) == 1:
            c.yellow(f"{domain}: missing {bad[0]} (have {', '.join(ok)})")
        else:
            c.red(f"{domain}: missing {', '.join(bad)}")


def check_migrations(c: Check) -> None:
    files = sorted((REPO_ROOT / "api").glob("*migration*.sql"))
    if not files:
        c.red("no migration files found in api/")
        return
    c.green(f"{len(files)} migration file(s) present")
    # Surface the most recent ones — those are the most likely to be unapplied
    recent = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    for f in recent:
        c.yellow(f"verify applied in Supabase: {f.name}")


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

CHECKS: list[tuple[str, str, Callable]] = [
    ("git",        "Git working tree",                check_git),
    ("pyflakes",   "Pyflakes",                        check_pyflakes),
    ("tests",      "Test suite",                      check_tests),
    ("boot",       "App import / route registration", check_app_boot),
    ("env",        ".env critical variables",         check_env),
    ("health",     "Live /health probe",              check_health_endpoint),
    ("dns",        "DNS for sending domains",         check_dns),
    ("migrations", "Migration files",                 check_migrations),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--skip", action="append", default=[],
        help="Skip a check (e.g. --skip dns). Repeatable.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args()

    results: list[Check] = []
    env: dict = {}

    for key, label, fn in CHECKS:
        if key in args.skip:
            continue
        c = Check(label)
        try:
            if key == "env":
                env = check_env(c)
            elif key == "dns":
                # DNS check needs env from previous step
                if env:
                    check_dns(c, env)
                else:
                    c.yellow("env not loaded — skipping DNS check")
            else:
                fn(c)
        except Exception as exc:
            c.red(f"check raised: {exc}")
        results.append(c)

    reds = sum(1 for r in results if r.status == RED)
    yellows = sum(1 for r in results if r.status == YELLOW)
    greens = sum(1 for r in results if r.status == GREEN)

    if args.json:
        print(json.dumps({
            "summary": {"green": greens, "yellow": yellows, "red": reds},
            "checks": [
                {"name": r.name, "status": r.status, "messages": r.lines}
                for r in results
            ],
        }, indent=2))
        return 1 if reds else 0

    print()
    print("Warmr preflight check")
    print("=" * 60)
    for r in results:
        print(r.render())
    print("=" * 60)
    if reds:
        print(f"  {RED} {reds} blocker(s)  ·  {YELLOW} {yellows} warning(s)  ·  {GREEN} {greens} ok")
        print("  → NOT READY. Fix blockers before going live.")
    elif yellows:
        print(f"  {YELLOW} {yellows} warning(s)  ·  {GREEN} {greens} ok")
        print("  → Ready, but review warnings.")
    else:
        print(f"  {GREEN} all {greens} checks passed")
        print("  → READY.")
    print()
    return 1 if reds else 0


if __name__ == "__main__":
    raise SystemExit(main())
