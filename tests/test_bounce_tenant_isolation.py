"""
tests/test_bounce_tenant_isolation.py — cross-tenant bounce-path regression
tests (new finding from WARMR_ENTERPRISE_AUDIT_V2_Q3_2026.md, not in v1).

Covers two independent bugs, both scoped by client_id/inbox_id now:
  - bounce_handler.mark_lead_and_campaign_bounced looked up a lead by bare
    email (no client_id filter) — a bounce on tenant A's inbox could mark
    tenant B's lead (and campaign) bounced when both share an address
    (e.g. info@/sales@, common in B2B).
  - campaign_scheduler.is_email_hard_bounced accepted a client_id parameter
    but never used it — one tenant's bounce_log history (bounce_log has no
    direct client_id, only inbox_id) suppressed another tenant's sends to
    the same address.

Style matches the repo: top-level test_* functions, hand-rolled in-memory
fakes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bounce_handler as bh
import campaign_scheduler as cs


# ── Fake Supabase: one in-memory store, three tables ─────────────────────

class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self.store = store
        self.table_name = table
        self._op = "select"
        self._payload = None
        self._filters = []   # list of (col, val) equality filters
        self._in_filter = None  # (col, [values])

    def select(self, *a, **k):
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._in_filter = (col, list(vals))
        return self

    def limit(self, n):
        return self

    def _match(self, row):
        for col, val in self._filters:
            if row.get(col) != val:
                return False
        if self._in_filter:
            col, vals = self._in_filter
            if row.get(col) not in vals:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.table_name, [])
        matched = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in matched:
                r.update(self._payload)
        return _Exec(matched)


class FakeSupabase:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name):
        return _Query(self.store, name)


# ── mark_lead_and_campaign_bounced: scoped by client_id ──────────────────

def test_bounce_only_marks_the_correct_tenants_lead():
    """Two tenants both have a lead at info@shared-domain.nl. A bounce on
    tenant A's inbox must NOT touch tenant B's lead or campaign."""
    sb = FakeSupabase()
    sb.store["leads"] = [
        {"id": "lead-A", "email": "info@shared-domain.nl", "client_id": "tenant-A", "status": "active"},
        {"id": "lead-B", "email": "info@shared-domain.nl", "client_id": "tenant-B", "status": "active"},
    ]
    sb.store["campaign_leads"] = [
        {"id": "cl-A", "lead_id": "lead-A", "status": "active"},
        {"id": "cl-B", "lead_id": "lead-B", "status": "active"},
    ]

    bh.mark_lead_and_campaign_bounced(sb, "info@shared-domain.nl", "hard", "tenant-A")

    lead_a = next(r for r in sb.store["leads"] if r["id"] == "lead-A")
    lead_b = next(r for r in sb.store["leads"] if r["id"] == "lead-B")
    cl_a = next(r for r in sb.store["campaign_leads"] if r["id"] == "cl-A")
    cl_b = next(r for r in sb.store["campaign_leads"] if r["id"] == "cl-B")

    assert lead_a["status"] == "bounced"
    assert cl_a["status"] == "bounced"
    # Tenant B's lead and campaign must be completely untouched.
    assert lead_b["status"] == "active"
    assert cl_b["status"] == "active"


def test_bounce_for_unknown_tenant_touches_nothing():
    """If the (email, client_id) pair matches no lead, nothing is updated —
    proves the lookup is genuinely scoped, not falling back to a global match."""
    sb = FakeSupabase()
    sb.store["leads"] = [
        {"id": "lead-A", "email": "info@shared-domain.nl", "client_id": "tenant-A", "status": "active"},
    ]
    sb.store["campaign_leads"] = [
        {"id": "cl-A", "lead_id": "lead-A", "status": "active"},
    ]

    bh.mark_lead_and_campaign_bounced(sb, "info@shared-domain.nl", "hard", "tenant-other")

    lead_a = next(r for r in sb.store["leads"] if r["id"] == "lead-A")
    cl_a = next(r for r in sb.store["campaign_leads"] if r["id"] == "cl-A")
    assert lead_a["status"] == "active"
    assert cl_a["status"] == "active"


def test_soft_bounce_sets_soft_bounced_status():
    sb = FakeSupabase()
    sb.store["leads"] = [
        {"id": "lead-A", "email": "a@x.nl", "client_id": "tenant-A", "status": "active"},
    ]
    bh.mark_lead_and_campaign_bounced(sb, "a@x.nl", "soft", "tenant-A")
    lead_a = next(r for r in sb.store["leads"] if r["id"] == "lead-A")
    assert lead_a["status"] == "soft_bounced"


# ── is_email_hard_bounced: scoped by client_inbox_ids ────────────────────

def test_hard_bounced_check_ignores_other_tenants_inbox():
    """A hard bounce logged against tenant B's inbox must not block tenant
    A's send to the same address."""
    sb = FakeSupabase()
    sb.store["bounce_log"] = [
        {"id": "b1", "lead_email": "info@shared-domain.nl", "bounce_type": "hard", "inbox_id": "inbox-B"},
    ]

    # Tenant A's own inboxes do not include inbox-B.
    assert cs.is_email_hard_bounced(sb, "info@shared-domain.nl", client_inbox_ids=["inbox-A1", "inbox-A2"]) is False


def test_hard_bounced_check_blocks_within_same_tenant():
    sb = FakeSupabase()
    sb.store["bounce_log"] = [
        {"id": "b1", "lead_email": "dead@x.nl", "bounce_type": "hard", "inbox_id": "inbox-A1"},
    ]
    assert cs.is_email_hard_bounced(sb, "dead@x.nl", client_inbox_ids=["inbox-A1", "inbox-A2"]) is True


def test_hard_bounced_check_without_inbox_ids_falls_back_to_global():
    """No inbox_ids provided (e.g. called from a context with no resolved
    inbox set) — falls back to the pre-fix global check rather than
    silently disabling the safety net entirely."""
    sb = FakeSupabase()
    sb.store["bounce_log"] = [
        {"id": "b1", "lead_email": "dead@x.nl", "bounce_type": "hard", "inbox_id": "inbox-B"},
    ]
    assert cs.is_email_hard_bounced(sb, "dead@x.nl") is True


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
