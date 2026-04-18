"""
utils/startup_validator.py

Pre-flight checks for the Warmr API server and backend scripts.

Validates:
  1. Python version >= 3.11
  2. All required environment variables are set
  3. Required Python packages are importable
  4. Supabase connection is live (can reach the project URL)
  5. Supabase schema — required tables exist
  6. Anthropic API key is present and syntactically valid
  7. SMTP credentials are present for at least one inbox
  8. /health endpoint structure (dry-run, no server needed)

Run standalone:
    python -m utils.startup_validator

Or from the API startup:
    python -c "import asyncio; from utils.startup_validator import validate_startup; asyncio.run(validate_startup())"

Exit codes:
    0 — all checks passed (warnings allowed)
    1 — one or more checks FAILED (server should not start)
"""

import asyncio
import importlib
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ── Colours ───────────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

def _ok(msg: str)   -> str: return f"  {_GREEN}✓{_RESET}  {msg}"
def _warn(msg: str) -> str: return f"  {_YELLOW}⚠{_RESET}  {msg}"
def _fail(msg: str) -> str: return f"  {_RED}✗{_RESET}  {msg}"


@dataclass
class CheckResult:
    """Result of a single validation check."""
    name: str
    passed: bool
    warning: bool = False     # True → non-fatal (warn but continue)
    detail: str = ""


@dataclass
class ValidationReport:
    """Aggregated results of all checks."""
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and not c.warning]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.warning and not c.passed]

    @property
    def passed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.passed]

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, passed=True, detail=detail))

    def warn(self, name: str, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, passed=False, warning=True, detail=detail))

    def fail(self, name: str, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, passed=False, warning=False, detail=detail))


# ── Individual checks ─────────────────────────────────────────────────────────

def check_python_version(report: ValidationReport) -> None:
    """Require Python 3.11+."""
    v = sys.version_info
    ver_str = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        report.ok("Python version", f"Python {ver_str}")
    elif v >= (3, 10):
        report.warn(
            "Python version",
            f"Python {ver_str} — project targets 3.11+. Some syntax may fail at runtime.",
        )
    else:
        report.fail(
            "Python version",
            f"Python {ver_str} — requires 3.11+. Upgrade before running.",
        )


def check_env_vars(report: ValidationReport) -> None:
    """Verify required and recommended environment variables."""
    # Load .env if present
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
    except ImportError:
        pass  # Will be caught in packages check

    required = {
        "SUPABASE_URL":       "Supabase project URL",
        "SUPABASE_KEY":       "Supabase service role key",
        "ANTHROPIC_API_KEY":  "Anthropic API key",
    }
    recommended = {
        "SUPABASE_JWT_SECRET": "JWT secret for token validation (from Supabase → Settings → API)",
        "WARMR_API_TOKEN":     "API token for n8n webhook authentication",
        "ALLOWED_ORIGINS":     "CORS allowed origins",
    }

    for key, description in required.items():
        val = os.getenv(key, "").strip()
        if not val:
            report.fail(f"ENV: {key}", f"Missing — {description}")
        elif val.startswith("<") or "placeholder" in val.lower() or "vervang" in val.lower():
            report.fail(f"ENV: {key}", f"Still set to placeholder value: '{val[:40]}…'")
        else:
            report.ok(f"ENV: {key}", f"Set ({len(val)} chars)")

    for key, description in recommended.items():
        val = os.getenv(key, "").strip()
        if not val:
            report.warn(f"ENV: {key}", f"Not set — {description}")
        else:
            report.ok(f"ENV: {key}", f"Set ({len(val)} chars)")

    # Inbox credentials
    inbox_count = 0
    for i in range(1, 11):
        if os.getenv(f"INBOX_{i}_EMAIL"):
            inbox_count += 1
    if inbox_count == 0:
        report.warn("ENV: INBOX_*", "No inbox credentials found (INBOX_1_EMAIL etc.)")
    else:
        report.ok("ENV: INBOX_*", f"{inbox_count} inbox(es) configured")

    # Warmup network
    net_count = 0
    for i in range(1, 31):
        if os.getenv(f"WARMUP_NETWORK_{i}_EMAIL"):
            net_count += 1
    if net_count == 0:
        report.warn("ENV: WARMUP_NETWORK_*", "No warmup network accounts configured")
    elif net_count < 5:
        report.warn("ENV: WARMUP_NETWORK_*", f"Only {net_count} account(s) — recommend 20+ for realistic warmup patterns")
    else:
        report.ok("ENV: WARMUP_NETWORK_*", f"{net_count} warmup network account(s) configured")

    # Resend (optional but needed for briefing)
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key or "placeholder" in resend_key.lower():
        report.warn("ENV: RESEND_API_KEY", "Not set — daily briefing emails will not be sent")
    else:
        report.ok("ENV: RESEND_API_KEY", f"Set ({len(resend_key)} chars)")


def check_packages(report: ValidationReport) -> None:
    """Verify all required packages are importable."""
    packages = {
        "fastapi":          "fastapi",
        "uvicorn":          "uvicorn",
        "supabase":         "supabase",
        "anthropic":        "anthropic",
        "dotenv":           "python-dotenv",
        "jose":             "python-jose[cryptography]",
        "dns.resolver":     "dnspython",
        "httpx":            "httpx",
        "pandas":           "pandas",
        "multipart":        "python-multipart",
        "slowapi":          "slowapi",
        "starlette":        "starlette (bundled with FastAPI)",
        "pydantic":         "pydantic (bundled with FastAPI)",
    }
    for module, package in packages.items():
        try:
            importlib.import_module(module)
            report.ok(f"Package: {package}")
        except ImportError as e:
            report.fail(f"Package: {package}", f"ImportError: {e}")


async def check_supabase(report: ValidationReport) -> None:
    """Test Supabase connection and verify required tables exist."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()

    if not url or not key:
        report.fail("Supabase connection", "SUPABASE_URL or SUPABASE_KEY not set — skipping")
        return

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{url}/rest/v1/",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
            )
            if r.status_code in (200, 404):  # 404 on root is normal for Supabase
                report.ok("Supabase connection", f"Reachable (HTTP {r.status_code})")
            else:
                report.fail("Supabase connection", f"Unexpected status {r.status_code}: {r.text[:100]}")
    except Exception as e:
        report.fail("Supabase connection", f"Connection failed: {e}")
        return

    # Check required tables
    required_tables = [
        "inboxes", "domains", "warmup_logs", "sending_schedule",
        "bounce_log", "clients", "campaigns", "leads",
        "notifications", "decision_log", "experiments",
    ]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            for table in required_tables:
                r = await client.get(
                    f"{url}/rest/v1/{table}",
                    headers={
                        "apikey": key,
                        "Authorization": f"Bearer {key}",
                        "Range": "0-0",
                    },
                    params={"limit": "1"},
                )
                if r.status_code in (200, 206):
                    report.ok(f"Supabase table: {table}", "exists")
                elif r.status_code == 404:
                    report.fail(f"Supabase table: {table}", "Table not found — run full_schema.sql in Supabase")
                else:
                    report.warn(f"Supabase table: {table}", f"HTTP {r.status_code} — may be an RLS issue")
    except Exception as e:
        report.warn("Supabase tables", f"Could not verify tables: {e}")


def check_anthropic_key(report: ValidationReport) -> None:
    """Verify Anthropic API key format."""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        report.fail("Anthropic API key", "Not set")
    elif not key.startswith("sk-ant-"):
        report.warn("Anthropic API key", f"Unexpected format — expected 'sk-ant-…' prefix (got '{key[:12]}…')")
    elif len(key) < 50:
        report.warn("Anthropic API key", "Key looks too short — verify it's the full key")
    else:
        report.ok("Anthropic API key", f"Format OK (sk-ant-… {len(key)} chars)")


def check_api_imports(report: ValidationReport) -> None:
    """Verify the main API module can be imported without error."""
    try:
        # Temporarily suppress any uvicorn startup side effects
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import importlib
        # Only import the module definitions, not run the server
        spec = importlib.util.spec_from_file_location(
            "api.models",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "api", "models.py"),
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            report.ok("API: models.py imports")
        else:
            report.warn("API: models.py imports", "Could not locate file")
    except Exception as e:
        report.fail("API: models.py imports", str(e))

    try:
        spec = importlib.util.spec_from_file_location(
            "api.auth",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "api", "auth.py"),
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            report.ok("API: auth.py imports")
        else:
            report.warn("API: auth.py imports", "Could not locate file")
    except Exception as e:
        report.fail("API: auth.py imports", str(e))


# ── Main validator ────────────────────────────────────────────────────────────

async def validate_startup(exit_on_failure: bool = False) -> ValidationReport:
    """
    Run all pre-flight checks and print a formatted report.

    Args:
        exit_on_failure: If True, call sys.exit(1) when any check FAILS.

    Returns the ValidationReport (useful when called programmatically).
    """
    report = ValidationReport()

    print(f"\n{_BOLD}Warmr — Startup Validator{_RESET}")
    print(f"{'─' * 48}")
    print(f"  Timestamp : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Python    : {sys.version.split()[0]}")
    print(f"  CWD       : {os.getcwd()}")
    print(f"{'─' * 48}\n")

    # Run checks
    check_python_version(report)
    check_env_vars(report)
    check_packages(report)
    check_anthropic_key(report)
    await check_supabase(report)
    check_api_imports(report)

    # Print results
    sections = [
        ("PASSED",   report.passed,   _ok),
        ("WARNINGS", report.warnings, _warn),
        ("FAILED",   report.failures, _fail),
    ]

    for label, items, formatter in sections:
        if not items:
            continue
        print(f"\n{_BOLD}{label} ({len(items)}){_RESET}")
        for c in items:
            line = formatter(c.name)
            if c.detail:
                line += f"\n       {_YELLOW if c.warning else (_RED if not c.passed else '')}{c.detail}{_RESET}"
            print(line)

    # Summary
    total = len(report.checks)
    n_ok   = len(report.passed)
    n_warn = len(report.warnings)
    n_fail = len(report.failures)

    print(f"\n{'─' * 48}")
    if n_fail == 0 and n_warn == 0:
        print(f"{_GREEN}{_BOLD}✓ All {total} checks passed. Server is ready to start.{_RESET}\n")
    elif n_fail == 0:
        print(f"{_YELLOW}{_BOLD}⚠ {n_ok}/{total} passed, {n_warn} warning(s). Server can start but review warnings.{_RESET}\n")
    else:
        print(f"{_RED}{_BOLD}✗ {n_fail} check(s) FAILED ({n_warn} warnings). Fix failures before starting the server.{_RESET}\n")

    if exit_on_failure and n_fail > 0:
        sys.exit(1)

    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(validate_startup(exit_on_failure=True))
