"""
tests/test_suppression_helper.py — utils/suppression.py regression tests
(Fase 2, item 1).

Covers the single, reusable suppress_and_cancel() implementation shared by
the link-click unsubscribe flow (api/main.py) and the reply-based unsubscribe
flow (imap_processor.py) — previously the reply path only updated
leads.status and never touched suppression_list or campaign_leads.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.suppression import suppress_and_cancel


class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self.store = store
        self.table_name = table
        self._op = "select"
        self._payload = None
        self._filters = []
        self._in_filter = None

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
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
            rows.append(self._payload)
            return _Exec([self._payload])
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


def test_suppress_and_cancel_inserts_suppression_row():
    sb = FakeSupabase()
    suppress_and_cancel(sb, "client-1", "lead-1", "prospect@example.nl", source="reply")

    rows = sb.store["suppression_list"]
    assert len(rows) == 1
    assert rows[0]["client_id"] == "client-1"
    assert rows[0]["email"] == "prospect@example.nl"
    assert rows[0]["domain"] == "example.nl"
    assert rows[0]["source"] == "reply"


def test_suppress_and_cancel_stops_pending_and_active_campaign_leads():
    sb = FakeSupabase()
    sb.store["campaign_leads"] = [
        {"id": "cl-1", "lead_id": "lead-1", "status": "active"},
        {"id": "cl-2", "lead_id": "lead-1", "status": "pending"},
        {"id": "cl-3", "lead_id": "lead-1", "status": "completed"},
        {"id": "cl-other", "lead_id": "lead-2", "status": "active"},
    ]

    suppress_and_cancel(sb, "client-1", "lead-1", "prospect@example.nl")

    statuses = {r["id"]: r["status"] for r in sb.store["campaign_leads"]}
    assert statuses["cl-1"] == "unsubscribed"
    assert statuses["cl-2"] == "unsubscribed"
    assert statuses["cl-3"] == "completed"  # untouched — not active/pending
    assert statuses["cl-other"] == "active"  # different lead, untouched


def test_suppress_and_cancel_updates_lead_status():
    sb = FakeSupabase()
    sb.store["leads"] = [{"id": "lead-1", "status": "active"}]

    suppress_and_cancel(sb, "client-1", "lead-1", "prospect@example.nl")

    assert sb.store["leads"][0]["status"] == "unsubscribed"


def test_suppress_and_cancel_survives_a_failing_step():
    """One step raising (e.g. suppression_list insert erroring on a
    duplicate) must not prevent the other steps from running."""
    class _BrokenTable(_Query):
        def execute(self):
            if self.table_name == "suppression_list":
                raise RuntimeError("duplicate key")
            return super().execute()

    class _BrokenSupabase(FakeSupabase):
        def table(self, name):
            return _BrokenTable(self.store, name)

    sb = _BrokenSupabase()
    sb.store["leads"] = [{"id": "lead-1", "status": "active"}]

    suppress_and_cancel(sb, "client-1", "lead-1", "prospect@example.nl")

    assert sb.store["leads"][0]["status"] == "unsubscribed"


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
