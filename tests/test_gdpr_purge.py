"""
tests/test_gdpr_purge.py — api.main._purge_lead_by_id regression tests
(Fase 2, item 4).

Two bugs fixed here:
  - bounce_log has no client_id column, only inbox_id. The old code filtered
    `.eq("lead_email", email).eq("client_id", client_id)` — a query that
    always raised (undefined column), silently caught, so bounce_log rows
    were NEVER purged regardless of tenant. The fix resolves this tenant's
    own inbox_ids first and scopes the delete through inbox_id — the same
    cross-tenant-safe pattern already used for is_email_hard_bounced.
  - email_events has no client_id column either — the old unconditional
    `.eq("client_id", client_id)` on every table in the loop meant this
    delete also always raised and silently did nothing.
  - crm_sync_log (has client_id + lead_id) was missing from the purge
    entirely.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main


class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self.store = store
        self.table_name = table
        self._op = "select"
        self._insert_payload = None
        self._filters = []
        self._in_filter = None

    def select(self, *a, **k):
        return self

    def insert(self, payload):
        self._op = "insert"
        self._insert_payload = payload
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
        if self._op == "insert":
            rows.append(self._insert_payload)
            return _Exec([self._insert_payload])
        matched = [r for r in rows if self._match(r)]
        if self._op == "delete":
            self.store[self.table_name] = [r for r in rows if r not in matched]
        return _Exec(matched)


class FakeSupabase:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name):
        return _Query(self.store, name)


def _seeded_sb() -> FakeSupabase:
    sb = FakeSupabase()
    sb.store["inboxes"] = [
        {"id": "inbox-A1", "client_id": "client-A"},
        {"id": "inbox-B1", "client_id": "client-B"},
    ]
    sb.store["bounce_log"] = [
        {"id": "bl-A", "lead_email": "info@shared.nl", "inbox_id": "inbox-A1"},
        {"id": "bl-B", "lead_email": "info@shared.nl", "inbox_id": "inbox-B1"},
    ]
    sb.store["email_events"] = [
        {"id": "ee-1", "lead_id": "lead-1", "event_type": "opened"},
    ]
    sb.store["crm_sync_log"] = [
        {"id": "csl-1", "lead_id": "lead-1", "client_id": "client-A"},
    ]
    return sb


def test_purge_scopes_bounce_log_deletion_to_this_tenants_inboxes(monkeypatch):
    """Two tenants share the address info@shared.nl. Purging tenant A's lead
    must only remove tenant A's bounce_log row, not tenant B's."""
    sb = _seeded_sb()
    monkeypatch.setattr(api_main, "_supabase", sb)

    api_main._purge_lead_by_id("lead-1", "info@shared.nl", "client-A")

    remaining = [r["id"] for r in sb.store["bounce_log"]]
    assert remaining == ["bl-B"]


def test_purge_deletes_email_events_by_lead_id_only(monkeypatch):
    sb = _seeded_sb()
    monkeypatch.setattr(api_main, "_supabase", sb)

    deleted = api_main._purge_lead_by_id("lead-1", "info@shared.nl", "client-A")

    assert sb.store["email_events"] == []
    assert deleted["email_events"] == 1


def test_purge_deletes_crm_sync_log(monkeypatch):
    sb = _seeded_sb()
    monkeypatch.setattr(api_main, "_supabase", sb)

    deleted = api_main._purge_lead_by_id("lead-1", "info@shared.nl", "client-A")

    assert sb.store["crm_sync_log"] == []
    assert deleted["crm_sync_log"] == 1


def test_purge_with_no_inboxes_does_not_delete_bounce_log_at_all(monkeypatch):
    """A client with zero inboxes can own no bounce_log rows (they're only
    scoped via inbox_id) — the delete must be a safe no-op, not an error
    that aborts the rest of the purge."""
    sb = FakeSupabase()
    sb.store["bounce_log"] = [{"id": "bl-A", "lead_email": "x@y.nl", "inbox_id": "inbox-A1"}]
    monkeypatch.setattr(api_main, "_supabase", sb)

    deleted = api_main._purge_lead_by_id("lead-1", "x@y.nl", "client-A")

    assert len(sb.store["bounce_log"]) == 1  # untouched
    assert deleted["bounce_log"] == 0


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                import inspect
                if "monkeypatch" in inspect.signature(fn).parameters:
                    class _Mp:
                        def __init__(self):
                            self._undo = []
                        def setattr(self, target, name, value=None, raising=True):
                            self._undo.append((target, name, getattr(target, name, None)))
                            setattr(target, name, value)
                        def undo(self):
                            for t, n, v in self._undo:
                                setattr(t, n, v)
                    mp = _Mp()
                    try:
                        fn(mp)
                    finally:
                        mp.undo()
                else:
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
