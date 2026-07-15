"""
tests/test_stage_progression_engagement.py — regression tests for the
check_stage_progression engagement-signal fix (campaign_scheduler.py).

Previously campaign_scheduler.py called
check_stage_progression(supabase, lead_id, current_step, len(all_steps))
without has_opened/has_clicked, which silently default to False per
funnel_engine.check_stage_progression's own signature — engagement-triggered
cold->warm/warm->hot funnel transitions were dead in practice, only the
step-count fallback ever fired. Fixed by resolving both signals via a new
_lead_has_event() helper (shared with check_step_condition, which already
had an identical query) before calling check_stage_progression.
"""

import sys
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

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        return self

    def _match(self, row):
        return all(row.get(col) == val for col, val in self._filters)

    def execute(self):
        return _Exec([r for r in self.rows if self._match(r)])


class FakeSupabase:
    def __init__(self, email_events: list[dict]):
        self.email_events = email_events

    def table(self, name):
        assert name == "email_events"
        return _Query(self.email_events)


# ── _lead_has_event ───────────────────────────────────────────────────────

def test_lead_has_event_true_when_matching_row_exists():
    sb = FakeSupabase(email_events=[
        {"campaign_id": "camp-1", "lead_id": "lead-1", "event_type": "opened"},
    ])
    assert cs._lead_has_event(sb, "lead-1", "camp-1", "opened") is True


def test_lead_has_event_false_when_no_matching_row():
    sb = FakeSupabase(email_events=[
        {"campaign_id": "camp-1", "lead_id": "lead-1", "event_type": "opened"},
    ])
    assert cs._lead_has_event(sb, "lead-1", "camp-1", "clicked") is False


def test_lead_has_event_scoped_to_this_campaign_only():
    """A lead may be in multiple campaigns — an open in campaign A must not
    count as engagement for campaign B."""
    sb = FakeSupabase(email_events=[
        {"campaign_id": "camp-OTHER", "lead_id": "lead-1", "event_type": "opened"},
    ])
    assert cs._lead_has_event(sb, "lead-1", "camp-1", "opened") is False


# ── check_step_condition still works after the shared-helper refactor ────

def test_check_step_condition_if_opened_passes_when_opened():
    sb = FakeSupabase(email_events=[
        {"campaign_id": "camp-1", "lead_id": "lead-1", "event_type": "opened"},
    ])
    step = {"condition_type": "if_opened", "condition_step": 1}
    assert cs.check_step_condition(sb, step, "lead-1", "camp-1", 2) is True


def test_check_step_condition_if_not_opened_skips_when_opened():
    sb = FakeSupabase(email_events=[
        {"campaign_id": "camp-1", "lead_id": "lead-1", "event_type": "opened"},
    ])
    step = {"condition_type": "if_not_opened", "condition_step": 1}
    assert cs.check_step_condition(sb, step, "lead-1", "camp-1", 2) is False


def test_check_step_condition_always_passes_without_querying():
    sb = FakeSupabase(email_events=[])
    step = {"condition_type": "always"}
    assert cs.check_step_condition(sb, step, "lead-1", "camp-1", 2) is True


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
