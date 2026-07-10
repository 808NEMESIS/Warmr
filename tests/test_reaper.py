"""
tests/test_reaper.py — reap_stranded_sends.py regression tests.

Covers the new finding (WARMR_ENTERPRISE_AUDIT_V2_Q3_2026.md): the atomic
send-claim in campaign_scheduler.py can strand a campaign_lead in
status='sending' forever if the process crashes between the claim and
completion — campaign_leads has no timestamp column to detect this, so
status_changed_at (added alongside this reaper) and the reaper itself are
one atomic bundle.

Style matches the repo: top-level test_* functions, hand-rolled fakes.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reap_stranded_sends as reaper


class _Exec:
    def __init__(self, data):
        self.data = data


class _ReaperTable:
    """
    Models: UPDATE campaign_leads SET status='active', status_changed_at=now()
            WHERE status='sending' AND status_changed_at < cutoff
    """
    def __init__(self, rows):
        self.rows = rows
        self._payload = None
        self._eq = {}
        self._lt = None

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    def _match(self, row):
        for col, val in self._eq.items():
            if row.get(col) != val:
                return False
        if self._lt:
            col, cutoff = self._lt
            if not (row.get(col) or "") < cutoff:
                return False
        return True

    def execute(self):
        matched = [r for r in self.rows if self._match(r)]
        for r in matched:
            r.update(self._payload)
        return _Exec(matched)


class FakeSupabase:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "campaign_leads"
        return _ReaperTable(self.rows)


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_reaps_lead_stranded_past_the_threshold():
    rows = [
        {"id": "cl-stuck", "status": "sending", "status_changed_at": _iso(45)},  # 45 min ago, threshold 30
    ]
    sb = FakeSupabase(rows)

    reaped = reaper.reap(sb, minutes=30)

    assert len(reaped) == 1
    assert reaped[0]["id"] == "cl-stuck"
    assert rows[0]["status"] == "active"


def test_leaves_a_fresh_sending_row_alone():
    """A lead that entered 'sending' 2 minutes ago is still within a normal
    SMTP round-trip — must NOT be reaped."""
    rows = [
        {"id": "cl-fresh", "status": "sending", "status_changed_at": _iso(2)},
    ]
    sb = FakeSupabase(rows)

    reaped = reaper.reap(sb, minutes=30)

    assert reaped == []
    assert rows[0]["status"] == "sending"


def test_leaves_non_sending_rows_alone_regardless_of_age():
    """An old 'active' row (normal, waiting for its next_send_at) must never
    be touched — the reaper only ever matches status='sending'."""
    rows = [
        {"id": "cl-active", "status": "active", "status_changed_at": _iso(999)},
    ]
    sb = FakeSupabase(rows)

    reaped = reaper.reap(sb, minutes=30)

    assert reaped == []
    assert rows[0]["status"] == "active"


def test_reaps_only_the_stranded_rows_in_a_mixed_batch():
    rows = [
        {"id": "cl-stuck-1", "status": "sending", "status_changed_at": _iso(60)},
        {"id": "cl-stuck-2", "status": "sending", "status_changed_at": _iso(31)},
        {"id": "cl-fresh", "status": "sending", "status_changed_at": _iso(1)},
        {"id": "cl-active", "status": "active", "status_changed_at": _iso(9999)},
    ]
    sb = FakeSupabase(rows)

    reaped_ids = {r["id"] for r in reaper.reap(sb, minutes=30)}

    assert reaped_ids == {"cl-stuck-1", "cl-stuck-2"}
    statuses = {r["id"]: r["status"] for r in rows}
    assert statuses["cl-stuck-1"] == "active"
    assert statuses["cl-stuck-2"] == "active"
    assert statuses["cl-fresh"] == "sending"
    assert statuses["cl-active"] == "active"


def test_reap_stranded_minutes_env_default_is_an_int():
    assert isinstance(reaper.REAP_STRANDED_MINUTES, int)
    assert reaper.REAP_STRANDED_MINUTES > 0


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
