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
        # W1-2: het venster-criterium query't inbox_status_log/warmup_logs
        # met andere kolommen — de stub filtert nu generiek.
        self._filters = getattr(self, "_filters", [])
        self._filters.append(("eq", col, val))
        if col == "id":
            self._filter_id = val
        return self

    def gte(self, col, val):
        self._filters = getattr(self, "_filters", [])
        self._filters.append(("gte", col, val))
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def limit(self, _n):
        return self

    def _match(self, row):
        for kind, col, val in getattr(self, "_filters", []):
            if kind == "eq" and row.get(col) != val:
                return False
            if kind == "gte" and not (str(row.get(col) or "") >= str(val)):
                return False
        return True

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._op == "select":
            return _Resp([r for r in rows if self._match(r)])
        if self._op == "update":
            updated = []
            for r in rows:
                if self._match(r):
                    r.update(self._payload)
                    updated.append(r)
            return _Resp(updated)
        if self._op == "insert":
            rows.append(dict(self._payload))
            return _Resp([self._payload])
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


# ── W1-2: 7-dagen-pauzevenster i.p.v. de nooit-gerestte teller ──────────────


def test_recent_pause_blocks_promotion():
    """Een pauze binnen 7 dagen blokkeert; de oude kolom is irrelevant."""
    sb = FakeSupabase()
    now = datetime.now(timezone.utc)
    sb.store["inboxes"] = [_make_inbox()]
    sb.store["inbox_status_log"] = [{
        "inbox_id": "inbox-1", "to_status": "paused",
        "created_at": (now - timedelta(days=2)).isoformat(),
    }]
    result = check_and_promote_inbox("inbox-1", sb)
    assert result["promoted"] is False
    assert result["reason"] == "recent_auto_pauses"
    assert result["criteria_status"]["recent_pauses_7d"] == 1


def test_old_pause_outside_window_promotes():
    """DE fuik-regressietest: pauzes ouder dan 7 dagen (zoals 26-27 mei)
    blokkeren promotie NIET meer — ook al staat de legacy-teller op 3."""
    sb = FakeSupabase()
    now = datetime.now(timezone.utc)
    sb.store["inboxes"] = [_make_inbox(auto_pause_count_24h=3)]  # legacy-teller genegeerd
    sb.store["inbox_status_log"] = [{
        "inbox_id": "inbox-1", "to_status": "paused",
        "created_at": (now - timedelta(days=45)).isoformat(),
    }]
    result = check_and_promote_inbox("inbox-1", sb)
    assert result["promoted"] is True, result
    assert sb.store["inboxes"][0]["status"] == "ready"


def test_promotion_writes_status_log():
    sb = FakeSupabase()
    sb.store["inboxes"] = [_make_inbox()]
    result = check_and_promote_inbox("inbox-1", sb)
    assert result["promoted"] is True
    transitions = sb.store.get("inbox_status_log", [])
    assert any(t.get("to_status") == "ready" and t.get("reason") == "promotion"
               for t in transitions)
