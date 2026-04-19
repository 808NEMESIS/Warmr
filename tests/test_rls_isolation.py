"""
tests/test_rls_isolation.py — proves the core multi-tenancy claim.

Creates two clients with distinct Supabase JWTs, inserts data for each,
then asserts that client A's JWT cannot read client B's rows via the
PostgREST API (RLS enforcement).

This is the most important security test. If it ever fails, tenants
can read each other's data — critical breach.

Requires live Supabase credentials. Skipped in CI; run locally:
    source .venv/bin/activate && python tests/test_rls_isolation.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # service role
REST = f"{SUPABASE_URL}/rest/v1"
AUTH = f"{SUPABASE_URL}/auth/v1"


def _admin_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _create_user(email: str, password: str) -> str:
    """Create a confirmed user via admin API, return UUID."""
    r = httpx.post(
        f"{AUTH}/admin/users",
        headers=_admin_headers(),
        json={"email": email, "password": password, "email_confirm": True},
        timeout=10,
    )
    if r.status_code == 422 and "already been registered" in r.text:
        # Look up existing
        lookup = httpx.get(
            f"{AUTH}/admin/users?email={email}",
            headers=_admin_headers(),
            timeout=10,
        )
        users = lookup.json().get("users") or []
        return users[0]["id"] if users else ""
    r.raise_for_status()
    return r.json()["id"]


def _login(email: str, password: str) -> tuple[str, str]:
    """Return (access_token, user_id)."""
    anon_key = _fetch_anon_key()
    r = httpx.post(
        f"{AUTH}/token?grant_type=password",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data["user"]["id"]


def _fetch_anon_key() -> str:
    """Fetch the anon key from frontend/config.js for login."""
    cfg = Path(__file__).resolve().parent.parent / "frontend" / "config.js"
    txt = cfg.read_text()
    import re
    m = re.search(r"supabaseAnonKey:\s*'([^']+)'", txt)
    if not m:
        raise RuntimeError("Could not find anon key in config.js")
    return m.group(1)


def _upsert_client_row(user_id: str, company: str) -> None:
    """Insert a clients row for the user (needed for RLS policies)."""
    httpx.post(
        f"{REST}/clients",
        headers={**_admin_headers(), "Prefer": "resolution=merge-duplicates"},
        json={"id": user_id, "company_name": company, "email": f"{company.lower()}@test.local", "plan": "trial"},
        timeout=10,
    )


def _insert_lead(user_id: str, email: str) -> str:
    r = httpx.post(
        f"{REST}/leads",
        headers=_admin_headers(),
        json={"client_id": user_id, "email": email, "first_name": "Test"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()[0]["id"]


def _user_query_leads(access_token: str) -> list[dict]:
    """Query /leads as a real user — RLS should filter."""
    anon_key = _fetch_anon_key()
    r = httpx.get(
        f"{REST}/leads?select=id,email,client_id",
        headers={"apikey": anon_key, "Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _cleanup(user_id: str) -> None:
    """Delete the test user via admin API (cascades via FK)."""
    httpx.delete(f"{AUTH}/admin/users/{user_id}", headers=_admin_headers(), timeout=10)


# ── The test ─────────────────────────────────────────────────────────────

def test_rls_blocks_cross_tenant_read():
    """
    Two users (A and B) each get their own lead. A's JWT must only see A's lead.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  ⚠ SKIP: SUPABASE_URL/KEY not set")
        return

    import uuid
    run = uuid.uuid4().hex[:8]
    email_a = f"rls-test-a-{run}@warmr-rls.local"
    email_b = f"rls-test-b-{run}@warmr-rls.local"
    password = "RLS-Test-2026!"

    # Setup
    user_a = _create_user(email_a, password)
    user_b = _create_user(email_b, password)
    _upsert_client_row(user_a, f"Client-A-{run}")
    _upsert_client_row(user_b, f"Client-B-{run}")

    lead_a = _insert_lead(user_a, f"prospect-a-{run}@example.com")
    lead_b = _insert_lead(user_b, f"prospect-b-{run}@example.com")

    try:
        # Log in as each user
        token_a, _ = _login(email_a, password)
        token_b, _ = _login(email_b, password)

        # As user A, query leads — should only see lead_a
        rows_a = _user_query_leads(token_a)
        a_sees_own = any(r["id"] == lead_a for r in rows_a)
        a_sees_other = any(r["id"] == lead_b for r in rows_a)
        a_sees_other_client = any(r["client_id"] != user_a for r in rows_a)

        assert a_sees_own, f"User A cannot see own lead {lead_a}"
        assert not a_sees_other, f"CRITICAL BREACH: User A can see User B's lead {lead_b}"
        assert not a_sees_other_client, f"CRITICAL BREACH: User A sees leads with foreign client_id"

        # Symmetric check for user B
        rows_b = _user_query_leads(token_b)
        b_sees_own = any(r["id"] == lead_b for r in rows_b)
        b_sees_other = any(r["id"] == lead_a for r in rows_b)

        assert b_sees_own, f"User B cannot see own lead {lead_b}"
        assert not b_sees_other, f"CRITICAL BREACH: User B can see User A's lead {lead_a}"

        print(f"  ✓ RLS blocks cross-tenant reads (A={len(rows_a)} rows, B={len(rows_b)} rows)")

    finally:
        # Cleanup — deleting user cascades via FK to clients, leads, etc.
        _cleanup(user_a)
        _cleanup(user_b)


def test_rls_blocks_cross_tenant_write():
    """
    User A cannot INSERT a lead with client_id = user_b.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  ⚠ SKIP: SUPABASE_URL/KEY not set")
        return

    import uuid
    run = uuid.uuid4().hex[:8]
    email_a = f"rls-write-a-{run}@warmr-rls.local"
    email_b = f"rls-write-b-{run}@warmr-rls.local"
    password = "RLS-Write-2026!"

    user_a = _create_user(email_a, password)
    user_b = _create_user(email_b, password)
    _upsert_client_row(user_a, f"Client-WA-{run}")
    _upsert_client_row(user_b, f"Client-WB-{run}")

    try:
        token_a, _ = _login(email_a, password)
        anon_key = _fetch_anon_key()

        # Attempt to insert a lead FOR user B using user A's token
        r = httpx.post(
            f"{REST}/leads",
            headers={
                "apikey": anon_key,
                "Authorization": f"Bearer {token_a}",
                "Content-Type": "application/json",
            },
            json={"client_id": user_b, "email": "injected@evil.com"},
            timeout=10,
        )

        # Should be rejected by RLS — 403 or the insert succeeds but gets re-filtered
        assert r.status_code in (401, 403) or r.json() == [], (
            f"CRITICAL BREACH: User A can insert leads for User B (got {r.status_code}: {r.text[:200]})"
        )
        print(f"  ✓ RLS blocks cross-tenant writes (HTTP {r.status_code})")

    finally:
        _cleanup(user_a)
        _cleanup(user_b)


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                failed += 1
                print(f"  ✗ {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
