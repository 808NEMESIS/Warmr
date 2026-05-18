"""
tests/test_campaign_scheduler_business_hours.py — unit tests voor
campaign_scheduler._within_business_hours().

Deze helper is de eerste-laag bescherming tegen send-traffic buiten
07:00-19:00 Europe/Amsterdam Mon-Fri (per-campaign gating in
process_campaign() is tweede laag). Regressie hier = send-traffic op
verkeerde momenten naar prospects. Pure-function tests met fixed-time
fixtures — geen datetime.now() mock nodig.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campaign_scheduler import _within_business_hours


TZ = ZoneInfo("Europe/Amsterdam")


@pytest.mark.parametrize("dt,expected,label", [
    (datetime(2026, 5, 18, 12, 0, tzinfo=TZ), True, "Mon 12:00 mid-window"),
    (datetime(2026, 5, 18, 6, 59, tzinfo=TZ), False, "Mon 06:59 net vóór window"),
    (datetime(2026, 5, 18, 19, 0, tzinfo=TZ), False, "Mon 19:00 exclusief eind"),
    (datetime(2026, 5, 23, 12, 0, tzinfo=TZ), False, "Sat 12:00 weekend"),
    (datetime(2026, 5, 24, 12, 0, tzinfo=TZ), False, "Sun 12:00 weekend"),
    (datetime(2026, 5, 22, 18, 59, tzinfo=TZ), True, "Fri 18:59 net vóór eind"),
])
def test_within_business_hours(dt, expected, label):
    assert _within_business_hours(dt) is expected, f"FAIL: {label}"


def test_default_now_uses_current_time():
    """Sanity-check: zonder argument retourneert helper een bool op huidige tijd.

    Geen mock van datetime.now() — verifieert alleen dat de signature werkt
    en het type correct is.
    """
    result = _within_business_hours()
    assert isinstance(result, bool)
