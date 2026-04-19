"""
tests/test_engagement_scorer.py — unit tests for engagement scoring + decay.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engagement_scorer as es


class FakeTable:
    def __init__(self, selected=None):
        self.selected = selected or []
        self.update_calls = []
        self._filters = {}
        self._payload = None

    def select(self, *a, **k): return self
    def eq(self, k, v): self._filters[k] = v; return self
    def gt(self, k, v): self._filters[k + "_gt"] = v; return self
    def lt(self, k, v): self._filters[k + "_lt"] = v; return self
    def limit(self, n): return self
    def update(self, payload):
        self._payload = payload
        return self
    def execute(self):
        if self._payload is not None:
            self.update_calls.append({"filters": dict(self._filters), "payload": self._payload})
            self._payload = None
        return _Exec(self.selected)


class _Exec:
    def __init__(self, data): self.data = data


class FakeSupabase:
    def __init__(self, leads_selected=None):
        self._leads = FakeTable(leads_selected or [])

    def table(self, name):
        if name == "leads":
            return self._leads
        return FakeTable()


def test_add_engagement_open():
    sb = FakeSupabase(leads_selected=[{"engagement_score": 10}])
    new = es.add_engagement(sb, "lead-1", "opened")
    assert new == 15  # 10 + 5


def test_add_engagement_click_on_top_of_open():
    sb = FakeSupabase(leads_selected=[{"engagement_score": 15}])
    new = es.add_engagement(sb, "lead-1", "clicked")
    assert new == 25  # 15 + 10


def test_add_engagement_reply():
    sb = FakeSupabase(leads_selected=[{"engagement_score": 30}])
    new = es.add_engagement(sb, "lead-1", "replied")
    assert new == 55  # 30 + 25


def test_add_engagement_caps_at_100():
    sb = FakeSupabase(leads_selected=[{"engagement_score": 95}])
    new = es.add_engagement(sb, "lead-1", "replied")  # +25 would be 120
    assert new == 100


def test_add_engagement_floor_at_0():
    sb = FakeSupabase(leads_selected=[{"engagement_score": 5}])
    new = es.add_engagement(sb, "lead-1", "bounced")  # -10 → would be -5
    assert new == 0


def test_add_engagement_unknown_event():
    sb = FakeSupabase(leads_selected=[{"engagement_score": 10}])
    new = es.add_engagement(sb, "lead-1", "not_an_event")
    assert new is None


def test_add_engagement_updates_row():
    sb = FakeSupabase(leads_selected=[{"engagement_score": 0}])
    es.add_engagement(sb, "lead-1", "opened")
    updates = sb._leads.update_calls
    assert len(updates) == 1
    assert updates[0]["payload"]["engagement_score"] == 5
    assert "engagement_updated_at" in updates[0]["payload"]


def test_add_engagement_missing_lead_returns_none():
    sb = FakeSupabase(leads_selected=[])  # no leads
    new = es.add_engagement(sb, "missing", "opened")
    assert new is None


def test_scores_table_values():
    # Contract test — if someone changes the values, this test fails loudly
    assert es.SCORES["opened"] == 5
    assert es.SCORES["clicked"] == 10
    assert es.SCORES["replied"] == 25
    assert es.SCORES["interested"] == 50
    assert es.SCORES["bounced"] == -10
    assert es.DAILY_DECAY == 2.0
    assert es.MAX_SCORE == 100.0


def test_apply_daily_decay_reduces_score():
    # Lead active 3 days ago — should decay 3 × DAILY_DECAY = 6 points
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    sb = FakeSupabase(leads_selected=[
        {"id": "lead-1", "engagement_score": 20, "engagement_updated_at": three_days_ago}
    ])
    decayed = es.apply_daily_decay(sb)
    assert decayed == 1
    updates = sb._leads.update_calls
    assert len(updates) == 1
    assert updates[0]["payload"]["engagement_score"] == 14  # 20 - 6


def test_apply_daily_decay_floors_at_zero():
    one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    sb = FakeSupabase(leads_selected=[
        {"id": "lead-1", "engagement_score": 1, "engagement_updated_at": one_day_ago}
    ])
    es.apply_daily_decay(sb)
    updates = sb._leads.update_calls
    assert updates[0]["payload"]["engagement_score"] == 0


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"  \u2713 {name}")
            except AssertionError as e:
                failed += 1
                print(f"  \u2717 {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  \u2717 {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
