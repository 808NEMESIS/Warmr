"""
tests/test_compliance_overview.py — api.main.compliance_overview regression
test (Fase 2, item 7a).

email_events has no client_id column, so the old `_count("email_events")`
(a bare `.eq("client_id", ...)`) always raised and was silently caught,
always reporting 0 to the client regardless of how much data actually
existed. The fix resolves this tenant's own lead_ids first and counts
email_events through lead_id instead — this test also proves it doesn't
leak another tenant's event count.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main


class _Exec:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self._filters = []
        self._in_filter = None
        self._count_mode = None

    def select(self, *a, count=None, **k):
        self._count_mode = count
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._in_filter = (col, list(vals))
        return self

    def gte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
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
        matched = [r for r in self.rows if self._match(r)]
        count = len(matched) if self._count_mode else None
        return _Exec(matched, count=count)


class FakeSupabase:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _seeded_sb() -> FakeSupabase:
    return FakeSupabase({
        "leads": [
            {"id": "lead-A1", "client_id": "client-A"},
            {"id": "lead-A2", "client_id": "client-A"},
            {"id": "lead-B1", "client_id": "client-B"},
        ],
        "email_events": [
            {"id": "ee-1", "lead_id": "lead-A1", "event_type": "opened"},
            {"id": "ee-2", "lead_id": "lead-A2", "event_type": "clicked"},
            {"id": "ee-3", "lead_id": "lead-B1", "event_type": "opened"},
        ],
        "admin_audit_log": [],
    })


def test_compliance_overview_counts_email_events_via_lead_ids(monkeypatch):
    sb = _seeded_sb()
    monkeypatch.setattr(api_main, "_supabase", sb)

    result = asyncio.run(api_main.compliance_overview("client-A"))

    assert result["data_held_by_warmr"]["email_events"] == 2


def test_compliance_overview_email_events_count_does_not_leak_other_tenant(monkeypatch):
    sb = _seeded_sb()
    monkeypatch.setattr(api_main, "_supabase", sb)

    result = asyncio.run(api_main.compliance_overview("client-B"))

    assert result["data_held_by_warmr"]["email_events"] == 1


def test_compliance_overview_client_with_no_leads_reports_zero(monkeypatch):
    sb = FakeSupabase({"leads": [], "email_events": [], "admin_audit_log": []})
    monkeypatch.setattr(api_main, "_supabase", sb)

    result = asyncio.run(api_main.compliance_overview("client-nobody"))

    assert result["data_held_by_warmr"]["email_events"] == 0


if __name__ == "__main__":
    import inspect

    class _Mp:
        def __init__(self):
            self._undo = []
        def setattr(self, target, name, value=None, raising=True):
            self._undo.append((target, name, getattr(target, name, None)))
            setattr(target, name, value)
        def undo(self):
            for t, n, v in self._undo:
                setattr(t, n, v)

    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        total += 1
        mp = _Mp() if "monkeypatch" in inspect.signature(fn).parameters else None
        try:
            fn(mp) if mp else fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
        finally:
            if mp:
                mp.undo()
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
