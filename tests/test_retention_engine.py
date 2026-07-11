"""
tests/test_retention_engine.py — retention_engine.py regression tests
(Fase 2, Track 2, item 8).

Covers:
  - purge_aged_events respects a per-client retention_days override, falls
    back to the global default otherwise, leaves fresh rows alone, and
    correctly scopes the tables that have no client_id column (warmup_logs/
    bounce_log via inbox_id, email_events via lead_id) — the same pattern
    already proven for utils/client_deletion.py.
  - purge_closed_accounts only hard-deletes accounts whose closed_at is
    past the grace period; active accounts (closed_at=None) and
    closed-but-not-yet-due accounts are left untouched.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import retention_engine as re_mod


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self.store = store
        self.table_name = table
        self._op = "select"
        self._filters = []
        self._in_filter = None
        self._lt = None

    def select(self, *a, **k):
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._in_filter = (col, list(vals))
        return self

    def lt(self, col, val):
        self._lt = (col, val)
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
        if self._lt:
            col, cutoff = self._lt
            val = row.get(col)
            if val is None or not (val < cutoff):
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.table_name, [])
        matched = [r for r in rows if self._match(r)]
        if self._op == "delete":
            self.store[self.table_name] = [r for r in rows if r not in matched]
        return _Exec(matched)


class FakeSupabase:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name):
        return _Query(self.store, name)


# ── purge_aged_events ──────────────────────────────────────────────────

def _base_sb() -> FakeSupabase:
    sb = FakeSupabase()
    sb.store["clients"] = [{"id": "client-A"}]
    sb.store["client_settings"] = []
    sb.store["inboxes"] = [{"id": "inbox-A1", "client_id": "client-A"}]
    sb.store["leads"] = [{"id": "lead-A1", "client_id": "client-A"}]
    return sb


def test_purge_aged_events_uses_global_default_without_override():
    sb = _base_sb()
    sb.store["reply_inbox"] = [
        {"id": "ri-old", "client_id": "client-A", "received_at": _iso(400)},
        {"id": "ri-fresh", "client_id": "client-A", "received_at": _iso(5)},
    ]

    result = re_mod.purge_aged_events(sb, default_days=365)

    remaining = [r["id"] for r in sb.store["reply_inbox"]]
    assert remaining == ["ri-fresh"]
    assert result["client-A"]["reply_inbox"] == 1


def test_purge_aged_events_respects_per_client_retention_override():
    sb = _base_sb()
    sb.store["client_settings"] = [{"client_id": "client-A", "retention_days": 30}]
    sb.store["email_tracking"] = [
        {"id": "et-1", "client_id": "client-A", "created_at": _iso(45)},  # older than 30d override
        {"id": "et-2", "client_id": "client-A", "created_at": _iso(10)},
    ]

    result = re_mod.purge_aged_events(sb, default_days=365)

    remaining = [r["id"] for r in sb.store["email_tracking"]]
    assert remaining == ["et-2"]
    assert result["client-A"]["email_tracking"] == 1


def test_purge_aged_events_scopes_warmup_and_bounce_logs_via_inbox_id():
    sb = _base_sb()
    sb.store["warmup_logs"] = [
        {"id": "wl-1", "inbox_id": "inbox-A1", "timestamp": _iso(400)},
        {"id": "wl-other", "inbox_id": "inbox-OTHER", "timestamp": _iso(400)},
    ]
    sb.store["bounce_log"] = [
        {"id": "bl-1", "inbox_id": "inbox-A1", "timestamp": _iso(400)},
    ]

    re_mod.purge_aged_events(sb, default_days=365)

    remaining_wl = [r["id"] for r in sb.store["warmup_logs"]]
    assert remaining_wl == ["wl-other"]  # only this tenant's inbox row purged
    assert sb.store["bounce_log"] == []


def test_purge_aged_events_scopes_email_events_via_lead_id():
    sb = _base_sb()
    sb.store["email_events"] = [
        {"id": "ee-1", "lead_id": "lead-A1", "timestamp": _iso(400)},
        {"id": "ee-other", "lead_id": "lead-OTHER", "timestamp": _iso(400)},
    ]

    re_mod.purge_aged_events(sb, default_days=365)

    remaining = [r["id"] for r in sb.store["email_events"]]
    assert remaining == ["ee-other"]


def test_purge_aged_events_client_with_no_inboxes_or_leads_is_a_safe_noop():
    sb = FakeSupabase()
    sb.store["clients"] = [{"id": "client-empty"}]
    sb.store["client_settings"] = []
    sb.store["inboxes"] = []
    sb.store["leads"] = []

    result = re_mod.purge_aged_events(sb, default_days=365)

    assert result["client-empty"]["warmup_logs"] == 0
    assert result["client-empty"]["bounce_log"] == 0
    assert result["client-empty"]["email_events"] == 0


# ── purge_closed_accounts ──────────────────────────────────────────────

def test_purge_closed_accounts_deletes_past_grace_period():
    sb = FakeSupabase()
    sb.store["clients"] = [{"id": "client-closed-old", "closed_at": _iso(45)}]
    for t in ("inboxes", "leads", "campaign_leads", "campaigns", "client_settings",
              "domains", "email_tracking", "suppression_list", "notifications",
              "reply_inbox", "warmup_logs", "bounce_log"):
        sb.store[t] = []

    purged = re_mod.purge_closed_accounts(sb, grace_days=30)

    assert purged == ["client-closed-old"]
    assert sb.store["clients"] == []


def test_purge_closed_accounts_leaves_active_accounts_untouched():
    """closed_at=None (never set) must never match `lt` — an active
    account must not be silently swept up."""
    sb = FakeSupabase()
    sb.store["clients"] = [{"id": "client-active", "closed_at": None}]

    purged = re_mod.purge_closed_accounts(sb, grace_days=30)

    assert purged == []
    assert len(sb.store["clients"]) == 1


def test_purge_closed_accounts_leaves_closed_but_not_yet_due_account_untouched():
    sb = FakeSupabase()
    sb.store["clients"] = [{"id": "client-recently-closed", "closed_at": _iso(5)}]  # closed 5 days ago, grace=30

    purged = re_mod.purge_closed_accounts(sb, grace_days=30)

    assert purged == []
    assert len(sb.store["clients"]) == 1


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
