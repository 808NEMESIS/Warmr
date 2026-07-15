"""
utils/events.py — minimal synchronous in-process domain-event bus (Fase 4,
architecture item 1).

Warmr has no single place where "this business event happened, therefore do
everything that should follow" is expressed — each side effect (operator
notification, webhook dispatch, CRM sync, engagement scoring, funnel-stage
routing) is wired ad hoc at whichever call site happened to need it, with no
shared record of what SHOULD fire for a given event. For "prospect replied to
a campaign email" specifically, this meant CRM sync and engagement scoring
never fired at all, and funnel routing was only reachable via a disconnected
manual dashboard endpoint — see utils/event_handlers.py for the fix.

This is deliberately not a message queue, not async, not persistent — a
synchronous fan-out within the same request/process, matching how every
other side effect in this codebase already runs (inline, best-effort, one
try/except per side effect). Publishing an event with no subscribers is a
silent no-op. One handler raising does not stop the others — mirrors the
existing scattered try/except blocks it replaces.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DomainEvent:
    name: str
    client_id: str
    payload: dict
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


Handler = Callable[[Any, DomainEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def subscribe(self, name: str, fn: Handler) -> None:
        self._handlers.setdefault(name, []).append(fn)

    def publish(self, sb, event: DomainEvent) -> None:
        for fn in self._handlers.get(event.name, []):
            try:
                fn(sb, event)
            except Exception:
                logger.exception(
                    "event handler failed for %s (%s)", event.name, getattr(fn, "__name__", fn)
                )


default_bus = EventBus()


def subscribe(name: str):
    """Decorator sugar for utils/event_handlers.py: @subscribe("lead.replied")."""
    def deco(fn: Handler) -> Handler:
        default_bus.subscribe(name, fn)
        return fn
    return deco
