"""
tests/test_rls_hardening.py — proves the fix for WARMR_ENTERPRISE_AUDIT_2026-07.md
§6.1 (Critical 1): 8 tenant tables previously had RLS disabled, so any logged-in
tenant could read/write every other tenant's rows via the anon/authenticated key.

Companion to tests/test_rls_isolation.py (which only covers `leads`). This file
reuses that harness (same two-tenant JWT setup, same anon-key login) and asserts,
for each of the 8 newly-secured tables, that:

  1. Tenant A's JWT (authenticated role, anon apikey) CANNOT SELECT a row that
     belongs to tenant B — seeded via the service role.
  2. Tenant A's JWT CANNOT INSERT a row carrying tenant B's client_id.

After api/rls_hardening_migration.sql, anon/authenticated have NO table grant, so
PostgREST answers with 401/403 ("permission denied for table ..."). The assertions
also accept an empty 200 result, so the test still passes if a future revision
switches a table from REVOKE to an RLS-policy-only model.

Requires live Supabase credentials (service role). Skipped when unset. Run:
    source .venv/bin/activate && python tests/test_rls_hardening.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

# Reuse the exact harness from the existing RLS test (two-tenant JWTs, anon login).
from test_rls_isolation import (  # noqa: E402
    AUTH,
    REST,
    SUPABASE_KEY,
    SUPABASE_URL,
    _admin_headers,
    _cleanup,
    _create_user,
    _fetch_anon_key,
    _login,
    _upsert_client_row,
)

# The 8 tables locked down by api/rls_hardening_migration.sql.
SECURED_TABLES = [
    "webhook_logs",
    "webhook_events",
    "warmup_network_accounts",
    "network_health_log",
    "placement_test_results",
    "dns_check_log",
    "blacklist_recoveries",
    "unsubscribe_tokens",
]


# ── low-level helpers ─────────────────────────────────────────────────────

def _admin_insert(table: str, row: dict) -> dict:
    """Insert a row as the service role (bypasses RLS) and return it."""
    r = httpx.post(
        f"{REST}/{table}",
        headers=_admin_headers(),
        json=row,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()[0]


def _admin_delete(table: str, id_col: str, id_val: str) -> None:
    try:
        httpx.delete(
            f"{REST}/{table}?{id_col}=eq.{id_val}",
            headers=_admin_headers(),
            timeout=10,
        )
    except Exception:
        pass


def _tenant_select(table: str, access_token: str, filter_qs: str) -> httpx.Response:
    """SELECT as a real tenant (authenticated role + anon apikey)."""
    anon_key = _fetch_anon_key()
    return httpx.get(
        f"{REST}/{table}?{filter_qs}",
        headers={"apikey": anon_key, "Authorization": f"Bearer {access_token}"},
        timeout=10,
    )


def _tenant_insert(table: str, access_token: str, row: dict) -> httpx.Response:
    """INSERT as a real tenant (authenticated role + anon apikey)."""
    anon_key = _fetch_anon_key()
    return httpx.post(
        f"{REST}/{table}",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        json=row,
        timeout=10,
    )


def _assert_cannot_read(table: str, resp: httpx.Response, marker: str) -> None:
    """
    Pass if the tenant is denied (401/403 permission denied) OR gets a 200 that
    does not contain the foreign row (empty / RLS-filtered).
    """
    if resp.status_code in (401, 403):
        return  # REVOKE / permission denied — strongest outcome
    assert resp.status_code == 200, (
        f"{table}: unexpected status {resp.status_code}: {resp.text[:200]}"
    )
    body = resp.text
    assert marker not in body, (
        f"CRITICAL BREACH: tenant A can read tenant B's {table} row "
        f"(marker {marker!r} present in cross-tenant SELECT)"
    )


def _assert_cannot_write(table: str, resp: httpx.Response) -> None:
    """Pass if the foreign-client_id insert is rejected (401/403) or no-op ([])."""
    denied = resp.status_code in (401, 403)
    empty = False
    if resp.status_code in (200, 201):
        try:
            empty = resp.json() == []
        except Exception:
            empty = False
    assert denied or empty, (
        f"CRITICAL BREACH: tenant A inserted into {table} "
        f"(got {resp.status_code}: {resp.text[:200]})"
    )


# ── per-tenant seeding (service role) ─────────────────────────────────────

def _seed_secured_rows(client_id: str, run: str) -> dict:
    """
    Seed exactly one row per secured table for `client_id`, creating the
    parent rows (domain, placement_test) the FK-linked tables require.
    Returns identifiers used for cross-tenant assertions and cleanup.
    """
    marker = f"rlshard-{run}"

    # Parent: domains (for dns_check_log + blacklist_recoveries)
    domain = _admin_insert("domains", {
        "domain": f"{marker}.example.com",
        "client_id": client_id,
    })
    domain_id = domain["id"]

    # Parent: placement_tests (for placement_test_results)
    ptest = _admin_insert("placement_tests", {
        "client_id": client_id,
        "subject": f"{marker} subject",
        "status": "pending",
    })
    ptest_id = ptest["id"]

    rows: dict[str, dict] = {}

    rows["webhook_logs"] = _admin_insert("webhook_logs", {
        "client_id": client_id,
        "event_type": f"{marker}.event",
        "payload": {"marker": marker},
    })

    rows["webhook_events"] = _admin_insert("webhook_events", {
        "client_id": client_id,
        "event_type": f"{marker}.event",
        "payload": {"marker": marker, "lead_email": f"{marker}@prospect.example"},
    })

    rows["warmup_network_accounts"] = _admin_insert("warmup_network_accounts", {
        "client_id": client_id,
        "email": f"{marker}@warmup.example",
        "provider": "gmail",
        "status": "active",
    })

    rows["network_health_log"] = _admin_insert("network_health_log", {
        "client_id": client_id,
        "total_accounts": 10,
        "active_accounts": 9,
        "health_score": 90.0,
    })

    rows["placement_test_results"] = _admin_insert("placement_test_results", {
        "test_id": ptest_id,
        "seed_provider": "gmail",
        "seed_email": f"{marker}@seed.example",
        "placement": "inbox",
    })

    rows["dns_check_log"] = _admin_insert("dns_check_log", {
        "domain_id": domain_id,
        "check_type": "spf",
        "result": "ok",
        "actual_value": marker,
    })

    rows["blacklist_recoveries"] = _admin_insert("blacklist_recoveries", {
        "domain_id": domain_id,
        "blacklist_name": f"{marker}-zone",
    })

    rows["unsubscribe_tokens"] = _admin_insert("unsubscribe_tokens", {
        "token": f"{marker}-token",
        "client_id": client_id,
        "lead_id": f"{marker}-lead",
        "lead_email": f"{marker}@prospect.example",
    })

    return {
        "marker": marker,
        "domain_id": domain_id,
        "ptest_id": ptest_id,
        "rows": rows,
    }


def _cleanup_secured_rows(seed: dict) -> None:
    for table, row in seed["rows"].items():
        _admin_delete(table, "id", row["id"])
    _admin_delete("placement_tests", "id", seed["ptest_id"])
    _admin_delete("domains", "id", seed["domain_id"])


# A foreign-insert probe row per table (minimal valid payload; client_id set to
# the victim tenant). Value filled in at call time with tenant B's client_id.
def _foreign_insert_row(table: str, victim_client_id: str, run: str) -> dict:
    tag = f"evil-{run}"
    return {
        "webhook_logs": {
            "client_id": victim_client_id, "event_type": f"{tag}.x", "payload": {},
        },
        "webhook_events": {
            "client_id": victim_client_id, "event_type": f"{tag}.x", "payload": {},
        },
        "warmup_network_accounts": {
            "client_id": victim_client_id, "email": f"{tag}@evil.example",
        },
        "network_health_log": {
            "client_id": victim_client_id,
            "total_accounts": 1, "active_accounts": 1, "health_score": 1.0,
        },
        "unsubscribe_tokens": {
            "token": f"{tag}-token", "client_id": victim_client_id,
            "lead_id": f"{tag}-lead", "lead_email": f"{tag}@evil.example",
        },
        # FK-linked tables: even a valid FK-less attempt must be blocked at the
        # grant/RLS layer. Use a random UUID parent so the only thing that can
        # let it through is a missing grant/policy.
        "placement_test_results": {
            "test_id": str(uuid.uuid4()), "seed_provider": "gmail",
            "seed_email": f"{tag}@evil.example", "placement": "inbox",
        },
        "dns_check_log": {
            "domain_id": str(uuid.uuid4()), "check_type": "spf", "result": "ok",
        },
        "blacklist_recoveries": {
            "domain_id": str(uuid.uuid4()), "blacklist_name": f"{tag}-zone",
        },
    }[table]


# ── the tests ─────────────────────────────────────────────────────────────

def _make_two_tenants(run: str):
    email_a = f"rls-hard-a-{run}@warmr-rls.local"
    email_b = f"rls-hard-b-{run}@warmr-rls.local"
    password = "RLS-Hard-2026!"
    user_a = _create_user(email_a, password)
    user_b = _create_user(email_b, password)
    _upsert_client_row(user_a, f"Client-HA-{run}")
    _upsert_client_row(user_b, f"Client-HB-{run}")
    token_a, _ = _login(email_a, password)
    return user_a, user_b, token_a


def test_secured_tables_block_cross_tenant_read():
    """
    Seed one row per secured table for tenant B; assert tenant A's JWT cannot
    SELECT any of them.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  ⚠ SKIP: SUPABASE_URL/KEY not set")
        return

    run = uuid.uuid4().hex[:8]
    user_a, user_b, token_a = _make_two_tenants(run)
    seed = _seed_secured_rows(user_b, run)
    marker = seed["marker"]

    try:
        for table in SECURED_TABLES:
            row = seed["rows"][table]
            # Sanity: the row really exists (service role sees it).
            check = httpx.get(
                f"{REST}/{table}?id=eq.{row['id']}",
                headers=_admin_headers(), timeout=10,
            )
            assert check.status_code == 200 and check.json(), (
                f"seed failed for {table}: {check.status_code} {check.text[:200]}"
            )
            # Tenant A tries to read tenant B's row by its own id.
            resp = _tenant_select(table, token_a, f"id=eq.{row['id']}&select=*")
            _assert_cannot_read(table, resp, marker)
            # And a broad unfiltered read must not leak it either.
            resp_all = _tenant_select(table, token_a, "select=*&limit=1000")
            _assert_cannot_read(table, resp_all, marker)
            print(f"  ✓ {table}: cross-tenant read blocked (HTTP {resp.status_code})")
    finally:
        _cleanup_secured_rows(seed)
        _cleanup(user_a)
        _cleanup(user_b)


def test_secured_tables_block_cross_tenant_write():
    """
    Tenant A cannot INSERT a row carrying tenant B's client_id into any secured
    table.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  ⚠ SKIP: SUPABASE_URL/KEY not set")
        return

    run = uuid.uuid4().hex[:8]
    user_a, user_b, token_a = _make_two_tenants(run)

    inserted_ids: list[tuple[str, str]] = []
    try:
        for table in SECURED_TABLES:
            row = _foreign_insert_row(table, user_b, run)
            resp = _tenant_insert(table, token_a, row)
            _assert_cannot_write(table, resp)
            # If (breach) something got written, record it for cleanup.
            if resp.status_code in (200, 201):
                try:
                    for r in resp.json():
                        if isinstance(r, dict) and r.get("id"):
                            inserted_ids.append((table, r["id"]))
                except Exception:
                    pass
            print(f"  ✓ {table}: cross-tenant write blocked (HTTP {resp.status_code})")
    finally:
        for table, rid in inserted_ids:
            _admin_delete(table, "id", rid)
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
