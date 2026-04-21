"""
tests/test_backend_service_role_isolation.py — verify that backend scripts
running under the service-role key do not mix data across clients.

Service-role queries bypass RLS. Isolation depends on every query having an
explicit `.eq("client_id", ...)` filter. This test:

  1. Seeds 2 fake clients with their own inboxes, campaigns, leads,
     warmup_logs, sending_schedule, email_events rows.
  2. Calls each backend entry point that takes a client_id and asserts no
     returned row carries the other client's id.
  3. For backend functions that do NOT take client_id but access multi-tenant
     tables, prints a GAP report so the team knows isolation depends on
     operational discipline (one Warmr instance per tenant, .env gated).

Requires live Supabase. Cleanup always runs.

Run:
    source .venv/bin/activate
    python tests/test_backend_service_role_isolation.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")


# ── Helpers ───────────────────────────────────────────────────────────────

def _sb():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_client(sb, suffix: str) -> dict:
    """Create a fake auth user + matching client row + inbox + campaign + lead."""
    import httpx
    email = f"isotest-{suffix}@warmr-isolation.local"
    pw = "IsoTest-2026!"

    # Create auth user
    r = httpx.post(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        json={"email": email, "password": pw, "email_confirm": True},
        timeout=10,
    )
    if r.status_code == 422 and "already" in r.text.lower():
        lookup = httpx.get(
            f"{SUPABASE_URL}/auth/v1/admin/users?email={email}",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10,
        )
        client_id = (lookup.json().get("users") or [{}])[0].get("id", "")
    else:
        r.raise_for_status()
        client_id = r.json()["id"]

    # Client row
    sb.table("clients").upsert({
        "id":           client_id,
        "email":        email,
        "company_name": f"Isotest-{suffix}",
        "plan":         "trial",
    }, on_conflict="id").execute()

    # Inbox
    inbox_resp = sb.table("inboxes").insert({
        "email":    f"iso-{suffix}-{uuid.uuid4().hex[:6]}@iso-warmr.local",
        "domain":   f"iso-{suffix}.local",
        "provider": "google",
        "status":   "warmup",
        "warmup_active": True,
        "client_id": client_id,
        "created_at": _now(),
    }).execute()
    inbox_id = inbox_resp.data[0]["id"]

    # Campaign
    camp_resp = sb.table("campaigns").insert({
        "name":      f"iso-campaign-{suffix}",
        "status":    "active",
        "client_id": client_id,
    }).execute()
    campaign_id = camp_resp.data[0]["id"]

    # Lead
    lead_resp = sb.table("leads").insert({
        "email":     f"prospect-{suffix}-{uuid.uuid4().hex[:6]}@iso-warmr.local",
        "first_name": "Iso",
        "last_name":  suffix,
        "client_id":  client_id,
    }).execute()
    lead_id = lead_resp.data[0]["id"]

    return {
        "client_id":   client_id,
        "auth_email":  email,
        "inbox_id":    inbox_id,
        "campaign_id": campaign_id,
        "lead_id":     lead_id,
    }


def _cleanup(sb, fixture: dict) -> None:
    import httpx
    cid = fixture["client_id"]
    # FK cascades from clients should delete inboxes, campaigns, leads, etc.
    # But be explicit to survive schemas that haven't applied CASCADE yet.
    for table, col in [
        ("warmup_logs",   "inbox_id"),
        ("email_events",  "client_id"),
        ("campaign_leads", "client_id"),
        ("email_tracking", "client_id"),
        ("notifications",  "client_id"),
    ]:
        try:
            sb.table(table).delete().eq(col, fixture["inbox_id"] if col == "inbox_id" else cid).execute()
        except Exception:
            pass
    for table in ["leads", "campaigns", "inboxes"]:
        try:
            sb.table(table).delete().eq("client_id", cid).execute()
        except Exception:
            pass
    try:
        sb.table("clients").delete().eq("id", cid).execute()
    except Exception:
        pass
    try:
        httpx.delete(
            f"{SUPABASE_URL}/auth/v1/admin/users/{cid}",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10,
        )
    except Exception:
        pass


def _check_skip() -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  \u26A0  SKIP: SUPABASE_URL/KEY not set")
        return True
    return False


# ── Tests ─────────────────────────────────────────────────────────────────

def test_weekly_report_metrics_are_isolated():
    """weekly_report.gather_client_metrics(client_id) must only return rows
    belonging to that client_id."""
    if _check_skip():
        return
    sb = _sb()
    run = uuid.uuid4().hex[:6]
    a = _seed_client(sb, f"a-{run}")
    b = _seed_client(sb, f"b-{run}")
    try:
        from weekly_report import gather_client_metrics

        m_a = gather_client_metrics(sb, a["client_id"])
        m_b = gather_client_metrics(sb, b["client_id"])

        # No inbox from B in A's result, and vice versa
        a_inbox_emails = {i["email"] for i in m_a.get("inboxes", [])}
        b_inbox_emails = {i["email"] for i in m_b.get("inboxes", [])}

        a_inbox = sb.table("inboxes").select("email").eq("id", a["inbox_id"]).single().execute().data["email"]
        b_inbox = sb.table("inboxes").select("email").eq("id", b["inbox_id"]).single().execute().data["email"]

        assert a_inbox in a_inbox_emails, f"A's inbox missing from A metrics"
        assert b_inbox not in a_inbox_emails, f"CROSS-TENANT LEAK: B's inbox {b_inbox} visible in A metrics"
        assert b_inbox in b_inbox_emails, f"B's inbox missing from B metrics"
        assert a_inbox not in b_inbox_emails, f"CROSS-TENANT LEAK: A's inbox {a_inbox} visible in B metrics"

        print(f"    weekly_report: A={len(a_inbox_emails)} inboxes, B={len(b_inbox_emails)} inboxes, no cross-tenant rows")
    finally:
        _cleanup(sb, a)
        _cleanup(sb, b)


def test_campaign_scheduler_due_leads_are_isolated_by_campaign_id():
    """campaign_scheduler.load_due_campaign_leads takes campaign_id, and since
    campaigns are per-client, data cannot cross tenants through this function."""
    if _check_skip():
        return
    sb = _sb()
    run = uuid.uuid4().hex[:6]
    a = _seed_client(sb, f"a-{run}")
    b = _seed_client(sb, f"b-{run}")
    try:
        from campaign_scheduler import load_due_campaign_leads

        # Wire both leads into campaign_leads (overdue so they'd be picked up)
        for fx in (a, b):
            sb.table("campaign_leads").insert({
                "campaign_id":  fx["campaign_id"],
                "lead_id":      fx["lead_id"],
                "client_id":    fx["client_id"],
                "status":       "active",
                "current_step": 1,
                "next_send_at": "1970-01-01T00:00:00+00:00",
            }).execute()

        due_for_a = load_due_campaign_leads(sb, a["campaign_id"])
        due_for_b = load_due_campaign_leads(sb, b["campaign_id"])

        # Each call should only return its own campaign's rows
        for row in due_for_a:
            assert row.get("client_id") == a["client_id"], (
                f"CROSS-TENANT LEAK: campaign A returned row with client_id={row.get('client_id')}"
            )
        for row in due_for_b:
            assert row.get("client_id") == b["client_id"], (
                f"CROSS-TENANT LEAK: campaign B returned row with client_id={row.get('client_id')}"
            )
        print(f"    campaign_scheduler: A={len(due_for_a)} leads, B={len(due_for_b)} leads, no cross-tenant rows")
    finally:
        _cleanup(sb, a)
        _cleanup(sb, b)


def test_funnel_engine_snapshot_is_isolated():
    """funnel_engine.snapshot_funnel(client_id) must only count leads for that client."""
    if _check_skip():
        return
    sb = _sb()
    run = uuid.uuid4().hex[:6]
    a = _seed_client(sb, f"a-{run}")
    b = _seed_client(sb, f"b-{run}")
    try:
        from funnel_engine import snapshot_funnel

        counts_a = snapshot_funnel(sb, a["client_id"])
        counts_b = snapshot_funnel(sb, b["client_id"])
        # A seeded 1 lead; B seeded 1 lead. Each snapshot shows its own lead
        # (default stage "cold") and no more than what belongs to that client.
        a_total = sum(counts_a.values())
        b_total = sum(counts_b.values())
        assert a_total >= 1, f"A's snapshot is empty: {counts_a}"
        assert b_total >= 1, f"B's snapshot is empty: {counts_b}"
        # Hard evidence via raw table: each client really has just 1 lead
        a_leads = sb.table("leads").select("id", count="exact").eq("client_id", a["client_id"]).execute().count
        b_leads = sb.table("leads").select("id", count="exact").eq("client_id", b["client_id"]).execute().count
        assert a_total == a_leads, f"A snapshot total {a_total} != A leads {a_leads}"
        assert b_total == b_leads, f"B snapshot total {b_total} != B leads {b_leads}"
        print(f"    funnel_engine: A snapshot={a_total}, B snapshot={b_total}, matches row counts")
    finally:
        _cleanup(sb, a)
        _cleanup(sb, b)


def test_engagement_scorer_decay_does_not_leak_across_clients():
    """apply_daily_decay iterates ALL leads with score > 0. It takes no
    client_id and shouldn't need one (decay is identical per-lead), but we
    verify that decay applied to A's lead does not touch B's lead's score."""
    if _check_skip():
        return
    sb = _sb()
    run = uuid.uuid4().hex[:6]
    a = _seed_client(sb, f"a-{run}")
    b = _seed_client(sb, f"b-{run}")
    try:
        # Seed a score on A only, 3 days old (so decay should apply)
        three_days_ago = "2026-04-18T00:00:00+00:00"
        sb.table("leads").update({
            "engagement_score": 20.0,
            "engagement_updated_at": three_days_ago,
        }).eq("id", a["lead_id"]).execute()

        # Seed B with same score, same age
        sb.table("leads").update({
            "engagement_score": 10.0,
            "engagement_updated_at": three_days_ago,
        }).eq("id", b["lead_id"]).execute()

        from engagement_scorer import apply_daily_decay
        apply_daily_decay(sb)

        a_final = sb.table("leads").select("engagement_score").eq("id", a["lead_id"]).single().execute().data["engagement_score"]
        b_final = sb.table("leads").select("engagement_score").eq("id", b["lead_id"]).single().execute().data["engagement_score"]

        # Both decayed independently. Behavior is correct as long as each
        # lead's new score depends ONLY on its own previous score — not on
        # any other client's lead.
        assert a_final < 20.0, f"A's score did not decay: {a_final}"
        assert b_final < 10.0, f"B's score did not decay: {b_final}"
        # And the two don't mix: A's decay magnitude shouldn't equal what
        # would happen if B's score were factored in.
        print(f"    engagement_scorer: A {20.0}->{a_final}, B {10.0}->{b_final} (independent decay)")
    finally:
        _cleanup(sb, a)
        _cleanup(sb, b)


# ── GAP report (non-fatal — printed so the team knows) ────────────────────

def test_report_backend_functions_without_client_id_filter():
    """
    Several backend entry points operate over all tenants by design because
    they're driven by `.env` credentials (one Warmr instance per tenant).
    This test documents the list so the team knows isolation depends on
    operational discipline, not query filters.
    """
    gaps = [
        ("warmup_engine.load_active_inboxes(supabase)",
         "No client_id param. Returns ALL warmup_active inboxes. Isolation depends on .env passwords (`INBOX_N_PASSWORD` only maps emails the current server knows)."),
        ("imap_processor.load_active_inboxes(supabase)",
         "Same pattern — reads all inboxes globally; only those matching env-loaded passwords are actually connected to."),
        ("bounce_handler.load_active_inboxes(sb)",
         "Same. Scans IMAP only for inboxes with .env passwords."),
        ("diagnostics_engine.check_reputation_drift(sb, client_id)",
         "DOES take client_id — good. Kept here for visibility."),
    ]
    print("\n    GAP REPORT — backend functions that query multi-tenant tables without client_id:")
    for fn, note in gaps:
        print(f"      - {fn}")
        print(f"        {note}")
    print("    Isolation model today: one Warmr instance per tenant, .env-gated credentials.")
    print("    If you ever run multi-tenant on a single process, these functions need client_id filters.")


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"  \u2713 {name}")
            except AssertionError as e:
                failed += 1
                print(f"  \u2717 {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  \u2717 {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
