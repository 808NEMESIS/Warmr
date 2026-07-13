"""
tests/test_campaign_performance.py — api.main.campaigns_performance /
campaign_stats regression tests (Fase 3, KPI-tracking fixes, item 7-8).

Both endpoints previously crashed on every call:
  - /campaigns/performance queried email_events.created_at and
    email_events.client_id — neither column exists (real columns:
    timestamp, no client_id at all).
  - /campaigns/{id}/stats counted leads.campaign_id — leads has no such
    column; that relation lives on campaign_leads.

Async endpoints tested via asyncio.run(...) around a direct call — the
established pattern in this repo (no FastAPI TestClient anywhere).
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
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
        self._gte = None
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

    def gte(self, col, val):
        self._gte = (col, val)
        return self

    def order(self, *a, **k):
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
        if self._gte:
            col, cutoff = self._gte
            if not (row.get(col) or "") >= cutoff:
                return False
        return True

    def execute(self):
        matched = [r for r in self.rows if self._match(r)]
        count = len(matched) if self._count_mode else None
        return _Exec(matched, count=count)


class FakeSupabase:
    def __init__(self, tables: dict):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ── /campaigns/performance ────────────────────────────────────────────────

def test_campaigns_performance_does_not_crash_and_computes_rates(monkeypatch):
    sb = FakeSupabase({
        "campaigns": [
            {"id": "camp-A", "client_id": "client-1", "name": "Q3 Outreach", "status": "active",
             "created_at": _iso(10), "daily_limit": 50},
        ],
        "email_events": [
            {"campaign_id": "camp-A", "lead_id": "lead-1", "event_type": "sent", "timestamp": _iso(2)},
            {"campaign_id": "camp-A", "lead_id": "lead-2", "event_type": "sent", "timestamp": _iso(2)},
            {"campaign_id": "camp-A", "lead_id": "lead-1", "event_type": "opened", "timestamp": _iso(1)},
            {"campaign_id": "camp-A", "lead_id": "lead-1", "event_type": "replied", "timestamp": _iso(1)},
        ],
    })
    monkeypatch.setattr(api_main, "_supabase", sb)

    result = asyncio.run(api_main.campaigns_performance("client-1", days=30))

    camp = result["campaigns"][0]
    assert camp["sent"] == 2
    assert camp["unique_opens"] == 1
    assert camp["replies"] == 1
    assert camp["open_rate"] == 0.5
    assert camp["reply_rate"] == 0.5


def test_campaigns_performance_trend_scoped_to_this_clients_campaigns(monkeypatch):
    """The daily-trend query has no client_id column to filter on — it must
    scope through this client's own campaign_ids instead, and must not pull
    in another tenant's campaign events."""
    sb = FakeSupabase({
        "campaigns": [
            {"id": "camp-A", "client_id": "client-1", "name": "Mine", "status": "active",
             "created_at": _iso(10), "daily_limit": 50},
        ],
        "email_events": [
            {"campaign_id": "camp-A", "lead_id": "lead-1", "event_type": "sent", "timestamp": _iso(1)},
            {"campaign_id": "camp-OTHER-TENANT", "lead_id": "lead-9", "event_type": "sent", "timestamp": _iso(1)},
        ],
    })
    monkeypatch.setattr(api_main, "_supabase", sb)

    result = asyncio.run(api_main.campaigns_performance("client-1", days=30))

    total_trend_sent = sum(day["sent"] for day in result["daily_trend"])
    assert total_trend_sent == 1  # only camp-A's event, not the other tenant's


def test_campaigns_performance_with_zero_campaigns_returns_empty_trend(monkeypatch):
    sb = FakeSupabase({"campaigns": [], "email_events": []})
    monkeypatch.setattr(api_main, "_supabase", sb)

    result = asyncio.run(api_main.campaigns_performance("client-1", days=30))

    assert result["campaigns"] == []
    assert result["daily_trend"] == []
    assert result["overall"]["sent"] == 0


# ── /campaigns/{id}/stats ─────────────────────────────────────────────────

def test_campaign_stats_does_not_crash_and_counts_leads_via_campaign_leads(monkeypatch):
    sb = FakeSupabase({
        "campaigns": [{"id": "camp-A", "client_id": "client-1", "name": "Q3 Outreach"}],
        "campaign_leads": [
            {"id": "cl-1", "campaign_id": "camp-A", "lead_id": "lead-1"},
            {"id": "cl-2", "campaign_id": "camp-A", "lead_id": "lead-2"},
            {"id": "cl-3", "campaign_id": "camp-OTHER", "lead_id": "lead-9"},
        ],
        "sending_schedule": [
            {"campaign_id": "camp-A", "client_id": "client-1", "status": "sent"},
            {"campaign_id": "camp-A", "client_id": "client-1", "status": "sent"},
            {"campaign_id": "camp-A", "client_id": "client-1", "status": "replied"},
        ],
    })
    monkeypatch.setattr(api_main, "_supabase", sb)

    result = asyncio.run(api_main.campaign_stats("camp-A", "client-1"))

    assert result.total_leads == 2  # only camp-A's rows, not camp-OTHER's
    assert result.sent == 2
    assert result.replied == 1


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
