"""
tests/test_state_machine.py — unit tests for utils/state_machine.py's
StateMachine.validate()/check_log_only() (Fase 4b).

Pure, DB-agnostic tests — no mocks needed, the class has zero I/O. Plain
no-arg test functions (no pytest fixtures) so this runs under both
tests/run_all.py's hand-rolled discovery and pytest directly.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.state_machine import InvalidTransition, StateMachine

MACHINE = StateMachine(
    name="test.status",
    states=frozenset({"a", "b", "c"}),
    transitions={
        None: frozenset({"a"}),
        "a": frozenset({"b"}),
        "b": frozenset({"c"}),
    },
)


class _ListHandler(logging.Handler):
    """Minimal log-capture handler — avoids depending on pytest's caplog."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_creation_transition_allowed():
    MACHINE.validate(None, "a")  # must not raise


def test_creation_transition_to_wrong_state_raises():
    try:
        MACHINE.validate(None, "b")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_legal_transition_allowed():
    MACHINE.validate("a", "b")  # must not raise
    MACHINE.validate("b", "c")  # must not raise


def test_illegal_transition_raises():
    try:
        MACHINE.validate("a", "c")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_unknown_target_state_raises():
    try:
        MACHINE.validate("a", "nonexistent")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_same_state_rewrite_always_allowed():
    # Idempotent rewrite must be allowed even though e.g. "b" isn't in
    # transitions.get("b", ...) as a target of itself.
    MACHINE.validate("b", "b")
    MACHINE.validate("c", "c")


def test_from_state_with_no_outgoing_edges_raises_for_new_target():
    try:
        MACHINE.validate("c", "a")
        assert False, "expected InvalidTransition"
    except InvalidTransition:
        pass


def test_check_log_only_never_raises_on_illegal_transition():
    logger = logging.getLogger("test_state_machine.illegal")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        MACHINE.check_log_only("a", "c", logger)  # must not raise
        assert any("state machine mismatch (log-only)" in m for m in handler.messages)
    finally:
        logger.removeHandler(handler)


def test_check_log_only_logs_nothing_on_legal_transition():
    logger = logging.getLogger("test_state_machine.legal")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        MACHINE.check_log_only("a", "b", logger)
        assert not any("state machine mismatch" in m for m in handler.messages)
    finally:
        logger.removeHandler(handler)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  ok: {t.__name__}")
    print(f"{passed}/{len(tests)} passed")
