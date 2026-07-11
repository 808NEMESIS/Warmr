"""
utils/status_log.py — append-only audit-trail voor inbox-statuswissels.

W0-1 (mailbox health plan): de pauze van 26-27 mei was zes weken
onherleidbaar omdat geen enkel pad vastlegde wie/wat/waarom de status
wijzigde. Vanaf nu schrijft ÉLKE status-writer (diagnostics,
bounce_handler, auto_promote, operator-endpoints) hier een rij.

Fail-gedrag: fail-soft met luide log — een gemiste audit-rij mag een
pauze/resume/promotie nooit blokkeren (de transitie zelf is belangrijker
dan de administratie), maar moet wél zichtbaar zijn.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def log_status_transition(
    sb: Any,
    *,
    inbox_id: str,
    from_status: str | None,
    to_status: str,
    reason: str,
    source: str,
    detail: dict | None = None,
) -> bool:
    """Registreer een statuswissel in inbox_status_log. Returns True bij succes."""
    try:
        sb.table("inbox_status_log").insert({
            "inbox_id": inbox_id,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "source": source,
            "detail": detail or {},
        }).execute()
        return True
    except Exception as exc:
        logger.error(
            "inbox_status_log: TRANSITIE NIET GELOGD (%s → %s, reason=%s, inbox=%s): %s — "
            "is migratie 2026-07-11_capaciteit_terug gedraaid? De volgende "
            "incident-forensiek is anders weer onherleidbaar.",
            from_status, to_status, reason, inbox_id, exc,
        )
        return False
