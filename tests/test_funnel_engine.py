"""
tests/test_funnel_engine.py — unit tests for funnel_engine state machine.

Tests stage transitions, reply routing, and nurture cooldown logic using a
mock Supabase client — no real DB calls.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import funnel_engine as fe


class MockSupabaseTable:
    """Minimal mock that records calls and returns pre-canned data."""

    def __init__(self):
        self.update_calls = []
        self.insert_calls = []
        self.selected_data = []
        self._filters = {}

    def select(self, *args, **kwargs):
        self._filters = {"select": args}
        return self

    def insert(self, payload):
        self.insert_calls.append(payload)
        self._pending_insert = payload
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, n):
        return self

    def in_(self, col, vals):
        self._filters[col + "_in"] = vals
        return self

    def lt(self, col, val):
        self._filters[col + "_lt"] = val
        return self

    def gte(self, col, val):
        return self

    def order(self, *args, **kwargs):
        return self

    def execute(self):
        if hasattr(self, "_pending_update"):
            self.update_calls.append({"filters": dict(self._filters), "payload": self._pending_update})
            del self._pending_update
            return _Exec([{"id": self._filters.get("id", "x")}])
        if hasattr(self, "_pending_insert"):
            payload = self._pending_insert
            del self._pending_insert
            return _Exec([payload])
        return _Exec(self.selected_data)


class _Exec:
    def __init__(self, data):
        self.data = data


class MockSupabase:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        if name not in self.tables:
            self.tables[name] = MockSupabaseTable()
        return self.tables[name]


# ── move_to_stage ──────────────────────────────────────────────────────────

def test_move_to_stage_updates_lead():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "cold", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["crm_integrations"] = MockSupabaseTable()
    sb.tables["crm_integrations"].selected_data = []

    fe.move_to_stage(sb, "lead-1", "warm", reason="test")

    updates = sb.tables["leads"].update_calls
    assert len(updates) == 1
    assert updates[0]["payload"]["funnel_stage"] == "warm"
    assert "funnel_updated_at" in updates[0]["payload"]


def test_move_to_stage_rejects_invalid():
    sb = MockSupabase()
    fe.move_to_stage(sb, "lead-1", "not_a_stage")
    # Should silently do nothing — no tables touched
    assert "leads" not in sb.tables or not sb.tables["leads"].update_calls


def test_move_to_nurture_sets_cooldown():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "warm", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["crm_integrations"] = MockSupabaseTable()
    sb.tables["crm_integrations"].selected_data = []

    fe.move_to_stage(sb, "lead-1", "nurture", reason="cooldown")

    payload = sb.tables["leads"].update_calls[0]["payload"]
    assert payload["funnel_stage"] == "nurture"
    assert "nurture_until" in payload


# ── check_stage_progression ────────────────────────────────────────────────

def test_cold_to_warm_after_step_2():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "cold", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["crm_integrations"] = MockSupabaseTable()
    sb.tables["crm_integrations"].selected_data = []

    fe.check_stage_progression(sb, "lead-1", current_step=2, total_steps=5)
    updates = sb.tables["leads"].update_calls
    assert any(u["payload"].get("funnel_stage") == "warm" for u in updates)


def test_cold_stays_cold_on_step_1_no_engagement():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "cold"}]

    fe.check_stage_progression(sb, "lead-1", current_step=1, total_steps=5, has_opened=False, has_clicked=False)
    # No stage updates
    updates = sb.tables["leads"].update_calls
    assert not any(u["payload"].get("funnel_stage") for u in updates)


def test_warm_to_hot_on_click():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "warm", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["crm_integrations"] = MockSupabaseTable()
    sb.tables["crm_integrations"].selected_data = []

    fe.check_stage_progression(sb, "lead-1", current_step=3, total_steps=5, has_clicked=True)
    updates = sb.tables["leads"].update_calls
    assert any(u["payload"].get("funnel_stage") == "hot" for u in updates)


# ── route_reply ────────────────────────────────────────────────────────────

def test_route_interested_moves_to_hot():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "warm", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["reply_routing_rules"] = MockSupabaseTable()
    sb.tables["reply_routing_rules"].selected_data = []  # Use defaults
    sb.tables["notifications"] = MockSupabaseTable()
    sb.tables["crm_integrations"] = MockSupabaseTable()
    sb.tables["crm_integrations"].selected_data = []

    result = fe.route_reply(sb, "c1", "lead-1", "a@b.com", "interested")

    assert result["action_taken"] == "send_calendar"
    assert result["notification_created"] is True
    # Lead moved to hot
    assert any(u["payload"].get("funnel_stage") == "hot" for u in sb.tables["leads"].update_calls)


def test_route_unsubscribe_suppresses_lead():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "warm", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["reply_routing_rules"] = MockSupabaseTable()
    sb.tables["reply_routing_rules"].selected_data = []
    sb.tables["notifications"] = MockSupabaseTable()
    sb.tables["suppression_list"] = MockSupabaseTable()
    sb.tables["campaign_leads"] = MockSupabaseTable()
    sb.tables["crm_integrations"] = MockSupabaseTable()
    sb.tables["crm_integrations"].selected_data = []

    result = fe.route_reply(sb, "c1", "lead-1", "a@b.com", "unsubscribe", campaign_id="camp-1")

    assert result["action_taken"] == "suppress"
    # Added to suppression list
    assert len(sb.tables["suppression_list"].insert_calls) == 1
    # Lead moved to unsubscribed
    assert any(u["payload"].get("funnel_stage") == "unsubscribed" for u in sb.tables["leads"].update_calls)


def test_route_not_interested_stops_sequences():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "cold", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["reply_routing_rules"] = MockSupabaseTable()
    sb.tables["reply_routing_rules"].selected_data = []
    sb.tables["notifications"] = MockSupabaseTable()
    sb.tables["campaign_leads"] = MockSupabaseTable()
    sb.tables["crm_integrations"] = MockSupabaseTable()
    sb.tables["crm_integrations"].selected_data = []

    result = fe.route_reply(sb, "c1", "lead-1", "a@b.com", "not_interested")

    assert result["action_taken"] == "stop_sequence"
    # Lead moved to lost
    assert any(u["payload"].get("funnel_stage") == "lost" for u in sb.tables["leads"].update_calls)


def test_route_out_of_office_reschedules():
    sb = MockSupabase()
    sb.tables["leads"] = MockSupabaseTable()
    sb.tables["leads"].selected_data = [{"funnel_stage": "warm", "client_id": "c1", "email": "a@b.com"}]
    sb.tables["reply_routing_rules"] = MockSupabaseTable()
    sb.tables["reply_routing_rules"].selected_data = []
    sb.tables["campaign_leads"] = MockSupabaseTable()

    result = fe.route_reply(sb, "c1", "lead-1", "a@b.com", "out_of_office")

    assert result["action_taken"] == "reschedule"
    # campaign_leads was updated (next_send_at pushed forward)
    assert len(sb.tables["campaign_leads"].update_calls) >= 1
    # Funnel stage not changed for OOO
    lead_updates = [u for u in sb.tables["leads"].update_calls if u["payload"].get("funnel_stage")]
    assert len(lead_updates) == 0


# ── get_default_rules ──────────────────────────────────────────────────────

def test_default_rules_cover_all_categories():
    rules = fe.get_default_rules()
    required = {"interested", "question", "not_interested", "out_of_office", "referral", "unsubscribe", "other"}
    assert required.issubset(rules.keys())


if __name__ == "__main__":
    import sys as _sys
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
    _sys.exit(1 if failed else 0)
