"""
tests/test_inbox_resume.py — W1-1 (mailbox health plan): de auto-resume-fix.

DE regressietest voor de zes-weken-fuik: Supabase geeft auto_pause_reset_at
ZONDER offset terug ("2026-05-27T11:28:30.857826"); de oude parser
produceerde een naive datetime en de aware-vergelijking crashte elke
cyclus — gevangen als warning, dus onzichtbaar. Deze tests spelen exact
die situatie na en bewijzen: resume vuurt, teller wordt gepersisteerd op
0, en de transitie belandt in inbox_status_log.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diagnostics_engine import check_smtp_errors


class _Resp:
    def __init__(self, data):
        self.data = data
        self.count = len(data) if isinstance(data, list) else 0


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._op = "select"
        self._payload = None
        self._filters: list = []

    def select(self, *_, **__):
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def limit(self, _n):
        return self

    def _match(self, row):
        for kind, col, val in self._filters:
            if kind == "eq" and row.get(col) != val:
                return False
            if kind == "neq" and row.get(col) == val:
                return False
            if kind == "gte" and not (str(row.get(col) or "") >= str(val)):
                return False
            if kind == "in" and row.get(col) not in val:
                return False
        return True

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._op == "select":
            return _Resp([dict(r) for r in rows if self._match(r)])
        if self._op == "update":
            updated = []
            for r in rows:
                if self._match(r):
                    r.update(self._payload)
                    updated.append(dict(r))
            return _Resp(updated)
        rows.append(dict(self._payload))
        return _Resp([self._payload])


class FakeSupabase:
    def __init__(self):
        self.store: dict = {}

    def table(self, name):
        return _Query(self.store, name)


def _paused_inbox(reset_at: str) -> dict:
    return {
        "id": "inbox-1", "client_id": "client-1",
        "email": "info@example.nl", "status": "paused",
        "auto_pause_count_24h": 3,       # de vastgelopen legacy-teller
        "auto_pause_reset_at": reset_at,
    }


def test_naive_timestamp_resume_regression():
    """De 26-27-mei-situatie: naive reset_at (geen offset, geen Z) in het
    verleden → resume MOET vuren. De oude code crashte hier zes weken lang
    op 'can't compare offset-naive and offset-aware datetimes'."""
    sb = FakeSupabase()
    naive_past = (datetime.now(timezone.utc) - timedelta(days=45)) \
        .replace(tzinfo=None).isoformat()          # exact het Supabase-formaat
    sb.store["inboxes"] = [_paused_inbox(naive_past)]

    check_smtp_errors(sb, "client-1")

    inbox = sb.store["inboxes"][0]
    assert inbox["status"] == "warmup", "resume vuurde niet — tz-bug terug?"
    assert inbox["auto_pause_reset_at"] is None
    # W1-2: teller wordt nu gepersisteerd gereset (blokkeerde promotie eeuwig)
    assert inbox["auto_pause_count_24h"] == 0
    # transitie is herleidbaar
    logs = sb.store.get("inbox_status_log", [])
    assert any(t["to_status"] == "warmup" and t["reason"] == "auto_resume"
               for t in logs)
    # en de bestaande warmup_logs-registratie blijft werken
    assert any(w.get("action") == "auto_resumed"
               for w in sb.store.get("warmup_logs", []))


def test_future_reset_stays_paused():
    """Pauze-venster nog niet verstreken → géén resume."""
    sb = FakeSupabase()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    sb.store["inboxes"] = [_paused_inbox(future)]

    check_smtp_errors(sb, "client-1")

    assert sb.store["inboxes"][0]["status"] == "paused"


def test_aware_timestamp_also_works():
    """Timestamps mét offset (het formaat dat de code zelf schrijft) blijven
    óók werken — de fix mag geen nieuwe aanname introduceren."""
    sb = FakeSupabase()
    aware_past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    sb.store["inboxes"] = [_paused_inbox(aware_past)]

    check_smtp_errors(sb, "client-1")

    assert sb.store["inboxes"][0]["status"] == "warmup"
