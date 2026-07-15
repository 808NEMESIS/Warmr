"""
utils/state_machine.py — pure, DB-agnostic transition validation (Fase 4b,
architecture item 1 of 3: state machine).

No DB access, no side effects — this is deliberately the smallest possible
building block, so it's safe to wire into existing write sites in log-only
mode (call validate(), log a warning on mismatch, never block the write)
before anything ever depends on it for real enforcement. See
utils/state_machines_registry.py for the two reconstructed graphs
(campaign_leads.status, inboxes.status) and docs on how they were derived.

Every status/stage field examined this session (15 across the schema, see
production audit) had ad hoc, duplicated, or entirely absent validation —
e.g. campaign_leads.status's own writer function's docstring admits
"arbitrary value, no validation", and campaigns.status has three
independently-implemented, inconsistent guards across two files. This module
is the shared primitive those should eventually consolidate onto (Fase 4c+),
one field at a time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass


class InvalidTransition(Exception):
    """Raised by StateMachine.validate() when a transition isn't allowed."""


@dataclass(frozen=True)
class StateMachine:
    name: str
    states: frozenset[str]
    # from_state -> allowed to_states. Key `None` covers the creation
    # transition (no prior state).
    transitions: dict[str | None, frozenset[str]]

    def validate(self, from_state: str | None, to_state: str) -> None:
        """Raise InvalidTransition if to_state is unknown or the from->to
        edge isn't in the transition graph. Re-writing the same value is
        always allowed (idempotent), regardless of the graph."""
        if to_state not in self.states:
            raise InvalidTransition(f"{self.name}: unknown target state {to_state!r}")
        if from_state == to_state:
            return
        allowed = self.transitions.get(from_state, frozenset())
        if to_state not in allowed:
            raise InvalidTransition(f"{self.name}: {from_state!r} -> {to_state!r} not allowed")

    def check_log_only(self, from_state: str | None, to_state: str, logger: logging.Logger) -> None:
        """Fase 4b: observe whether a status write matches this (still-DRAFT)
        graph, without ever blocking it. Call this immediately before an
        existing write — never changes what gets written or whether it
        succeeds. See utils/state_machines_registry.py for why these graphs
        stay log-only until verified against real production data."""
        try:
            self.validate(from_state, to_state)
        except InvalidTransition as exc:
            logger.warning("state machine mismatch (log-only): %s", exc)
