"""
tests/test_auto_promote.py — unit tests for warmup → ready inbox-promotion.

4 scenarios per briefing-spec:
  1. insufficient_days — 14 days in warmup, all other criteria pass
  2. recent_spam_incident — incident within 14 days, all other criteria pass
  3. all criteria pass — promoted to ready
  4. already_ready — re-call, no status change

Uses a fake Supabase stub: minimal in-memory dict that records selects + updates.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_promote import check_and_promote_inbox


# ── Supabase stub ──────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal chainable mock of the Supabase Python query-builder."""

    def __init__(self, store, table_name, op="select", payload=None):
        self._store = store
        self._table = table_name
        self._op = op
        self._payload = payload
        self._filter_id = None

    def select(self, *_, **__):
        self._op = "select"
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        assert col == "id", f"stub only supports .eq('id', ...) — got {col!r}"
        self._filter_id = val
        return self

    def limit(self, _n):
        return self

    def execute(self):
        rows = self._store[self._table]
        if self._op == "select":
            if self._filter_id is not None:
                hit = [r for r in rows if r.get("id") == self._filter_id]
                return _Resp(hit)
            return _Resp(list(rows))
        if self._op == "update":
            updated = []
            for r in rows:
                if self._filter_id is None or r.get("id") == self._filter_id:
                    r.update(self._payload)
                    updated.append(r)
            return _Resp(updated)
        return _Resp([])


class FakeSupabase:
    def __init__(self):
        self.store = {"inboxes": []}

    def table(self, name):
        return _Query(self.store, name)


def _make_inbox(**overrides):
    """Baseline inbox row — all criteria pass unless overridden."""
    now = datetime.now(timezone.utc)
    base = {
        "id": "inbox-1",
        "client_id": "client-1",
        "email": "test@example.nl",
        "status": "warmup",
        "warmup_active": True,
        "warmup_start_date": (now - timedelta(days=30)).isoformat(),
        "reputation_score": 80,
        "last_spam_incident": None,
        "auto_pause_count_24h": 0,
        "daily_warmup_target": 50,
        "daily_campaign_target": 5,
        "daily_sent": 0,
        "updated_at": now.isoformat(),
    }
    base.update(overrides)
    return base


# ── 1. insufficient_days ───────────────────────────────────────────────────


def test_insufficient_days_blocks_promotion():
    sb = FakeSupabase()
    now = datetime.now(timezone.utc)
    sb.store["inboxes"].append(_make_inbox(
        warmup_start_date=(now - timedelta(days=14)).isoformat()
    ))

    result = check_and_promote_inbox("inbox-1", sb)

    assert result["promoted"] is False
    assert result["reason"] == "insufficient_days"
    assert result["new_status"] is None
    assert result["previous_status"] == "warmup"
    assert result["criteria_status"]["days_in_warmup"] == 14
    # Underlying row untouched
    assert sb.store["inboxes"][0]["status"] == "warmup"


# ── 2. recent_spam_incident ────────────────────────────────────────────────


def test_recent_spam_incident_blocks_promotion():
    sb = FakeSupabase()
    now = datetime.now(timezone.utc)
    sb.store["inboxes"].append(_make_inbox(
        last_spam_incident=(now - timedelta(days=3)).isoformat(),
    ))

    result = check_and_promote_inbox("inbox-1", sb)

    assert result["promoted"] is False
    assert result["reason"] == "recent_spam_incident"
    assert result["criteria_status"]["spam_clear"] is False
    assert sb.store["inboxes"][0]["status"] == "warmup"


# ── 3. all criteria pass → promoted ─────────────────────────────────────────


def test_all_criteria_pass_promotes_to_ready():
    sb = FakeSupabase()
    sb.store["inboxes"].append(_make_inbox())

    result = check_and_promote_inbox("inbox-1", sb)

    assert result["promoted"] is True
    assert result["reason"] == "promoted"
    assert result["previous_status"] == "warmup"
    assert result["new_status"] == "ready"
    # criteria_status mirrors what passed
    cs = result["criteria_status"]
    assert cs["warmup_active"] is True
    assert cs["days_in_warmup"] >= 28
    assert cs["reputation_score"] == 80
    assert cs["spam_clear"] is True
    assert cs["no_auto_pauses"] is True
    assert cs["target_reached"] is True
    # Side-effect: row status flipped
    assert sb.store["inboxes"][0]["status"] == "ready"
    # warmup_active stays True (reputation-maintenance traffic continues)
    assert sb.store["inboxes"][0]["warmup_active"] is True


# ── 4. already_ready idempotency ───────────────────────────────────────────


def test_already_ready_inbox_no_change():
    sb = FakeSupabase()
    sb.store["inboxes"].append(_make_inbox(status="ready"))

    result = check_and_promote_inbox("inbox-1", sb)

    assert result["promoted"] is False
    assert result["reason"] == "already_ready"
    assert result["previous_status"] == "ready"
    assert result["new_status"] is None
    # No status flip — still 'ready'
    assert sb.store["inboxes"][0]["status"] == "ready"
