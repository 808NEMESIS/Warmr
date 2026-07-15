"""
tests/test_events.py — utils/events.py regression tests (Fase 4, architecture
item 1, first slice).

EventBus is deliberately tested in isolation here (its own instance per test,
not utils.events.default_bus) — utils/event_handlers.py registers real
handlers on default_bus at import time, and this file shouldn't depend on
import order to stay a pure unit test of the bus mechanics themselves.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.events import DomainEvent, EventBus


class _FakeSb:
    pass


def test_publish_fans_out_to_all_subscribers():
    bus = EventBus()
    calls = []
    bus.subscribe("thing.happened", lambda sb, ev: calls.append(("a", ev.name)))
    bus.subscribe("thing.happened", lambda sb, ev: calls.append(("b", ev.name)))

    bus.publish(_FakeSb(), DomainEvent("thing.happened", "client-1", {"x": 1}))

    assert calls == [("a", "thing.happened"), ("b", "thing.happened")]


def test_publish_with_no_subscribers_is_a_silent_noop():
    bus = EventBus()
    bus.publish(_FakeSb(), DomainEvent("nobody.listening", "client-1", {}))  # must not raise


def test_one_failing_handler_does_not_stop_the_others():
    bus = EventBus()
    calls = []

    def _boom(sb, ev):
        raise RuntimeError("handler blew up")

    bus.subscribe("thing.happened", lambda sb, ev: calls.append("first"))
    bus.subscribe("thing.happened", _boom)
    bus.subscribe("thing.happened", lambda sb, ev: calls.append("third"))

    bus.publish(_FakeSb(), DomainEvent("thing.happened", "client-1", {}))

    assert calls == ["first", "third"]


def test_only_subscribers_for_the_published_event_name_fire():
    bus = EventBus()
    calls = []
    bus.subscribe("event.a", lambda sb, ev: calls.append("a"))
    bus.subscribe("event.b", lambda sb, ev: calls.append("b"))

    bus.publish(_FakeSb(), DomainEvent("event.a", "client-1", {}))

    assert calls == ["a"]


def test_handlers_receive_the_sb_argument_and_the_event_payload():
    bus = EventBus()
    received = {}

    def _handler(sb, ev):
        received["sb"] = sb
        received["payload"] = ev.payload
        received["client_id"] = ev.client_id

    bus.subscribe("thing.happened", _handler)
    sentinel_sb = _FakeSb()

    bus.publish(sentinel_sb, DomainEvent("thing.happened", "client-1", {"lead_id": "lead-1"}))

    assert received["sb"] is sentinel_sb
    assert received["payload"] == {"lead_id": "lead-1"}
    assert received["client_id"] == "client-1"


def test_domain_event_occurred_at_is_set_automatically():
    ev = DomainEvent("thing.happened", "client-1", {})
    assert ev.occurred_at  # non-empty ISO timestamp string
    assert "T" in ev.occurred_at


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
