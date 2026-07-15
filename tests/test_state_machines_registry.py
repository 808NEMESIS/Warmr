"""
tests/test_state_machines_registry.py — contract tests on the two
reconstructed transition graphs (Fase 4b): CAMPAIGN_LEAD_STATUS, INBOX_STATUS.

These pin down the graphs as read directly from the current write sites (see
utils/state_machines_registry.py's docstring for the file:line sources) so a
future edit that silently narrows/widens a graph gets caught here first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.state_machine import InvalidTransition
from utils.state_machines_registry import CAMPAIGN_LEAD_STATUS, INBOX_STATUS


# ── CAMPAIGN_LEAD_STATUS ──────────────────────────────────────────────────

def test_campaign_lead_creation_is_active():
    CAMPAIGN_LEAD_STATUS.validate(None, "active")


def test_campaign_lead_active_to_sending_allowed():
    CAMPAIGN_LEAD_STATUS.validate("active", "sending")


def test_campaign_lead_active_to_terminal_states_allowed():
    for to_state in ("bounced", "completed", "paused", "unsubscribed"):
        CAMPAIGN_LEAD_STATUS.validate("active", to_state)


def test_campaign_lead_sending_back_to_active_allowed():
    # reap_stranded_sends.py's stranded-lead revert.
    CAMPAIGN_LEAD_STATUS.validate("sending", "active")


def test_campaign_lead_sending_to_completed_allowed():
    CAMPAIGN_LEAD_STATUS.validate("sending", "completed")


def test_campaign_lead_completed_to_active_not_allowed():
    try:
        CAMPAIGN_LEAD_STATUS.validate("completed", "active")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_campaign_lead_unsubscribed_to_active_not_allowed():
    # unsubscribed is terminal — no outgoing edges in the current graph.
    try:
        CAMPAIGN_LEAD_STATUS.validate("unsubscribed", "active")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_campaign_lead_pending_treated_like_active_minus_sending():
    # "pending" is never observed as a write target (dead/legacy value) but
    # is kept in the graph for parity with the read-filters that treat it
    # like "active" — it can reach the same terminal states.
    for to_state in ("bounced", "completed", "paused", "unsubscribed"):
        CAMPAIGN_LEAD_STATUS.validate("pending", to_state)


def test_campaign_lead_idempotent_rewrite_always_allowed():
    for state in CAMPAIGN_LEAD_STATUS.states:
        CAMPAIGN_LEAD_STATUS.validate(state, state)


def test_campaign_lead_unknown_status_rejected():
    try:
        CAMPAIGN_LEAD_STATUS.validate("active", "nonexistent_status")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


# ── INBOX_STATUS ───────────────────────────────────────────────────────────

def test_inbox_creation_is_warmup():
    INBOX_STATUS.validate(None, "warmup")


def test_inbox_warmup_to_ready_allowed():
    # auto_promote.py's promotion write.
    INBOX_STATUS.validate("warmup", "ready")


def test_inbox_warmup_to_paused_allowed():
    INBOX_STATUS.validate("warmup", "paused")


def test_inbox_ready_to_paused_allowed():
    INBOX_STATUS.validate("ready", "paused")


def test_inbox_paused_to_warmup_allowed():
    # diagnostics_engine.py's auto-resume write.
    INBOX_STATUS.validate("paused", "warmup")


def test_inbox_ready_to_warmup_not_allowed():
    # no code path demotes ready straight back to warmup.
    try:
        INBOX_STATUS.validate("ready", "warmup")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_inbox_paused_to_ready_not_allowed():
    # resume always goes through warmup, never straight to ready.
    try:
        INBOX_STATUS.validate("paused", "ready")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_inbox_retired_is_legal_value_but_unreachable():
    # "retired" is a legal state (schema/CHECK should allow it) but has no
    # incoming transition in any code path found this session.
    assert "retired" in INBOX_STATUS.states
    for from_state in (None, "warmup", "ready", "paused"):
        try:
            INBOX_STATUS.validate(from_state, "retired")
            assert False, f"expected InvalidTransition from {from_state!r}"
        except InvalidTransition:
            pass


def test_inbox_idempotent_rewrite_always_allowed():
    for state in INBOX_STATUS.states:
        INBOX_STATUS.validate(state, state)


def test_inbox_unknown_status_rejected():
    try:
        INBOX_STATUS.validate("warmup", "nonexistent_status")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  ok: {t.__name__}")
    print(f"{passed}/{len(tests)} passed")
