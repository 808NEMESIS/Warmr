"""
utils/timeparse.py — één timestamp-parser voor de hele codebase.

DE bug die zes weken lang de auto-resume blokkeerde (W1-1, mailbox health
plan): Supabase geeft timestamps zonder offset terug
("2026-05-27T11:28:30.857826", geen Z, geen +00:00). De parser in
diagnostics_engine deed alleen `.replace("Z", "+00:00")` — een no-op op
zo'n string — en produceerde een NAIVE datetime. De vergelijking met een
aware `now_utc` gooide vervolgens elke cyclus
"can't compare offset-naive and offset-aware datetimes", gevangen door
een except die alleen een warning logde.

Regel: alles wat uit de database komt is UTC. Geen offset = UTC aannemen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_ts_utc(value: Any) -> datetime | None:
    """Parse een Supabase-timestamp naar een tz-aware UTC-datetime.

    Accepteert datetime-objecten, ISO-strings met Z/offset, en naive
    ISO-strings (→ UTC aangenomen). Return None bij onparseerbaar/leeg —
    de caller beslist wat dat betekent.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
