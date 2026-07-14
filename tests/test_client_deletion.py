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

from utils.client_deletion import (
    hard_delete_client,
    CLIENT_ID_TABLES,
    INBOX_SCOPED_TABLES,
    FK_BLOCKING_CLIENT_ID_TABLES,
)


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

    for table in CLIENT_ID_TABLES + INBOX_SCOPED_TABLES + FK_BLOCKING_CLIENT_ID_TABLES + ["email_events", "clients"]:
        assert table in deleted, f"{table} missing from delete report"


def test_hard_delete_clears_email_events_which_has_no_client_id_column():
    """Regression: email_events was never deleted at all by hard_delete_client
    (not in CLIENT_ID_TABLES — correctly, since it has no client_id column —
    but also never handled via any other scoping). Its FKs to inboxes/
    campaigns/leads are ON DELETE NO ACTION (confirmed via pg_constraint),
    so leaving it untouched makes the final `clients` delete (which cascades
    into inboxes/campaigns/leads) fail with a foreign-key violation for any
    client with campaign history."""
    sb = _seeded_sb()
    sb.store["email_events"] = [
        {"id": "ee-1", "inbox_id": "inbox-1", "campaign_id": None, "lead_id": "lead-1"},
        {"id": "ee-2", "inbox_id": "inbox-B", "campaign_id": None, "lead_id": "lead-B"},
    ]

    deleted = hard_delete_client(sb, "client-A")

    remaining = [r["id"] for r in sb.store["email_events"]]
    assert remaining == ["ee-2"]  # only the other tenant's row survives
    assert deleted["email_events"] == 1


def test_hard_delete_clears_fk_blocking_tables_before_their_parents():
    """Regression: reply_inbox and sending_schedule reference inboxes/
    campaigns/leads via ON DELETE NO ACTION. Previously they sat AFTER
    campaigns/inboxes/leads in CLIENT_ID_TABLES, so those parent deletes (and
    the client-level cascade at the very end) would fail while reply_inbox/
    sending_schedule rows still existed. This fake enforces the same
    ordering constraint Postgres does, to prove the real call order works."""

    class _FkEnforcingQuery(_Query):
        def execute(self):
            if self._op == "delete" and self.table_name in ("campaigns", "inboxes", "leads", "clients"):
                matched_ids = {r["id"] for r in self.store.get(self.table_name, []) if self._match(r)}
                fk_col = {"campaigns": "campaign_id", "inboxes": "inbox_id", "leads": "lead_id"}.get(self.table_name)
                for child_table in ("email_events", "reply_inbox", "sending_schedule"):
                    for row in self.store.get(child_table, []):
                        if fk_col and row.get(fk_col) in matched_ids:
                            raise RuntimeError(f"foreign key violation: {child_table}.{fk_col} -> {self.table_name}")
            return super().execute()

    class _FkEnforcingSupabase(FakeSupabase):
        def table(self, name):
            return _FkEnforcingQuery(self.store, name)

    sb = _FkEnforcingSupabase()
    sb.store["inboxes"] = [{"id": "inbox-1", "client_id": "client-A"}]
    sb.store["campaigns"] = [{"id": "camp-1", "client_id": "client-A"}]
    sb.store["leads"] = [{"id": "lead-1", "client_id": "client-A"}]
    sb.store["clients"] = [{"id": "client-A"}]
    sb.store["email_events"] = [{"id": "ee-1", "inbox_id": "inbox-1", "campaign_id": "camp-1", "lead_id": "lead-1"}]
    sb.store["reply_inbox"] = [{"id": "ri-1", "client_id": "client-A", "inbox_id": "inbox-1",
                                "campaign_id": "camp-1", "lead_id": "lead-1"}]
    sb.store["sending_schedule"] = [{"id": "ss-1", "client_id": "client-A", "inbox_id": "inbox-1"}]

    deleted = hard_delete_client(sb, "client-A")

    assert deleted["campaigns"] == 1
    assert deleted["inboxes"] == 1
    assert deleted["leads"] == 1
    assert deleted["clients"] == 1
    assert deleted["email_events"] == 1
    assert deleted["reply_inbox"] == 1
    assert deleted["sending_schedule"] == 1


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
