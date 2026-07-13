"""
tests/test_cross_campaign_dedup.py — campaign_scheduler.filter_recent_cross_campaign_touches
regression tests (Fase 3, KPI-tracking fixes, item 6).

Previously this filtered email_events on `.eq("client_id", client_id)` — a
column that doesn't exist on email_events — so the lookup always raised,
was caught, and the function silently fell back to returning every row
unfiltered. The dedup this function exists to provide ("same prospect
receives 3 emails on the same Tuesday from 3 parallel campaigns") was
permanently disabled. The fix scopes through the already-tenant-scoped
lead_ids instead, dropping the broken client_id filter.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import campaign_scheduler as cs


class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self._filters = []
        self._in_filter = None
        self._gte = None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._in_filter = (col, list(vals))
        return self

    def gte(self, col, val):
        self._gte = (col, val)
        return self

    def _match(self, row):
        for col, val in self._filters:
            if row.get(col) != val:
                return False
        if self._in_filter:
            col, vals = self._in_filter
            if row.get(col) not in vals:
                return False
        if self._gte:
            col, cutoff = self._gte
            if not (row.get(col) or "") >= cutoff:
                return False
        return True

    def execute(self):
        return _Exec([r for r in self.rows if self._match(r)])


class FakeSupabase:
    def __init__(self, events: list[dict]):
        self.events = events

    def table(self, name):
        assert name == "email_events"
        return _Query(self.events)


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_filters_out_lead_touched_by_another_campaign_recently():
    sb = FakeSupabase(events=[
        {"lead_id": "lead-1", "campaign_id": "camp-OTHER", "event_type": "sent", "timestamp": _iso(2)},
    ])
    due = [
        {"lead_id": "lead-1", "campaign_id": "camp-A"},
        {"lead_id": "lead-2", "campaign_id": "camp-A"},
    ]

    result = cs.filter_recent_cross_campaign_touches(sb, due, "client-1", min_days_between_sends=7)

    remaining = [r["lead_id"] for r in result]
    assert remaining == ["lead-2"]


def test_does_not_filter_touches_within_the_same_campaign():
    """A lead already emailed by THIS campaign shouldn't be caught by
    cross-campaign dedup — that's a different concern (sequence steps)."""
    sb = FakeSupabase(events=[
        {"lead_id": "lead-1", "campaign_id": "camp-A", "event_type": "sent", "timestamp": _iso(2)},
    ])
    due = [{"lead_id": "lead-1", "campaign_id": "camp-A"}]

    result = cs.filter_recent_cross_campaign_touches(sb, due, "client-1", min_days_between_sends=7)

    assert len(result) == 1


def test_does_not_filter_touches_outside_the_window():
    sb = FakeSupabase(events=[
        {"lead_id": "lead-1", "campaign_id": "camp-OTHER", "event_type": "sent", "timestamp": _iso(30)},
    ])
    due = [{"lead_id": "lead-1", "campaign_id": "camp-A"}]

    result = cs.filter_recent_cross_campaign_touches(sb, due, "client-1", min_days_between_sends=7)

    assert len(result) == 1


def test_no_op_when_client_id_missing():
    sb = FakeSupabase(events=[])
    due = [{"lead_id": "lead-1", "campaign_id": "camp-A"}]

    result = cs.filter_recent_cross_campaign_touches(sb, due, None, min_days_between_sends=7)

    assert result == due


def test_disabled_when_min_days_is_zero():
    sb = FakeSupabase(events=[
        {"lead_id": "lead-1", "campaign_id": "camp-OTHER", "event_type": "sent", "timestamp": _iso(1)},
    ])
    due = [{"lead_id": "lead-1", "campaign_id": "camp-A"}]

    result = cs.filter_recent_cross_campaign_touches(sb, due, "client-1", min_days_between_sends=0)

    assert result == due


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
