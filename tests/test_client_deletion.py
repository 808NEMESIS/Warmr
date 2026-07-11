"""
tests/test_client_deletion.py — utils/client_deletion.py regression tests
(Fase 2, item 3).

The old admin_delete_client only touched 7 of ~30 client_id tables and
swallowed every failure with a bare `except: pass`. hard_delete_client
replaces it: covers the full confirmed table list, scopes warmup_logs/
bounce_log via inbox_id (they have no client_id column), and reports every
step's outcome instead of hiding it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.client_deletion import hard_delete_client, CLIENT_ID_TABLES, INBOX_SCOPED_TABLES


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
        {"id": "inbox-1", "client_id": "client-A"},
        {"id": "inbox-2", "client_id": "client-A"},
        {"id": "inbox-B", "client_id": "client-B"},
    ]
    sb.store["warmup_logs"] = [
        {"id": "wl-1", "inbox_id": "inbox-1"},
        {"id": "wl-2", "inbox_id": "inbox-B"},
    ]
    sb.store["bounce_log"] = [
        {"id": "bl-1", "inbox_id": "inbox-2"},
        {"id": "bl-2", "inbox_id": "inbox-B"},
    ]
    sb.store["leads"] = [
        {"id": "lead-1", "client_id": "client-A"},
        {"id": "lead-B", "client_id": "client-B"},
    ]
    sb.store["clients"] = [
        {"id": "client-A"},
        {"id": "client-B"},
    ]
    return sb


def test_hard_delete_removes_inbox_scoped_rows_via_inbox_id():
    sb = _seeded_sb()
    deleted = hard_delete_client(sb, "client-A")

    remaining_wl = [r["id"] for r in sb.store["warmup_logs"]]
    remaining_bl = [r["id"] for r in sb.store["bounce_log"]]
    assert remaining_wl == ["wl-2"]  # inbox-1's row gone, inbox-B's kept
    assert remaining_bl == ["bl-2"]  # inbox-2's row gone, inbox-B's kept
    assert deleted["warmup_logs"] == 1
    assert deleted["bounce_log"] == 1


def test_hard_delete_does_not_touch_other_tenants_leads():
    sb = _seeded_sb()
    hard_delete_client(sb, "client-A")

    remaining_leads = [r["id"] for r in sb.store["leads"]]
    assert remaining_leads == ["lead-B"]


def test_hard_delete_removes_the_client_row_itself_last():
    sb = _seeded_sb()
    deleted = hard_delete_client(sb, "client-A")

    remaining_clients = [r["id"] for r in sb.store["clients"]]
    assert remaining_clients == ["client-B"]
    assert deleted["clients"] == 1


def test_hard_delete_records_every_table_outcome_not_swallowed():
    """Every table in the confirmed list gets an entry in the result dict —
    proves nothing is silently skipped the way the old bare except:pass was."""
    sb = _seeded_sb()
    deleted = hard_delete_client(sb, "client-A")

    for table in CLIENT_ID_TABLES + INBOX_SCOPED_TABLES + ["clients"]:
        assert table in deleted, f"{table} missing from delete report"


def test_hard_delete_with_no_inboxes_still_completes():
    """A client with zero inboxes must not error out of the inbox-scoped
    deletes (empty inbox_ids list) — the rest of the deletion still runs."""
    sb = FakeSupabase()
    sb.store["clients"] = [{"id": "client-A"}]

    deleted = hard_delete_client(sb, "client-A")

    assert deleted["warmup_logs"] == 0
    assert deleted["bounce_log"] == 0
    assert deleted["clients"] == 1


def test_hard_delete_one_table_error_does_not_abort_the_rest():
    """A single table erroring (e.g. missing in this fake) must not prevent
    the client row itself from still being deleted."""
    class _ExplodingQuery(_Query):
        def execute(self):
            if self.table_name == "leads" and self._op == "delete":
                raise RuntimeError("boom")
            return super().execute()

    class _ExplodingSupabase(FakeSupabase):
        def table(self, name):
            return _ExplodingQuery(self.store, name)

    sb = _ExplodingSupabase()
    sb.store["clients"] = [{"id": "client-A"}]

    deleted = hard_delete_client(sb, "client-A")

    assert isinstance(deleted["leads"], str) and deleted["leads"].startswith("error:")
    assert deleted["clients"] == 1


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
