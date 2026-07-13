"""
tests/test_reputation_helper.py — utils/reputation.py regression tests
(Fase 3, KPI-tracking fixes, item 1-2).

bump_reputation() is the single, canonical way to adjust inboxes.reputation_score
now (replacing imap_processor.py's update_reputation and bounce_handler.py's
inline read-then-write). Tries the atomic apply_reputation_delta RPC first,
falls back to a non-atomic read-then-write if the RPC isn't available yet —
same pattern as warmup_engine.update_daily_sent (Fase 0/1).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.reputation import REPUTATION_DELTA, bump_reputation


class _Exec:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self.store = store
        self.table_name = table
        self._op = "select"
        self._payload = None
        self._filters = []

    def select(self, *a, **k):
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        return self

    def _match(self, row):
        return all(row.get(col) == val for col, val in self._filters)

    def execute(self):
        rows = self.store.setdefault(self.table_name, [])
        matched = [r for r in rows if self._match(r)]
        if self._op == "update":
            for r in matched:
                r.update(self._payload)
        return _Exec(matched)


class _RpcCall:
    def __init__(self, sb, name, params):
        self.sb = sb
        self.name = name
        self.params = params

    def execute(self):
        if self.sb.rpc_should_fail:
            raise RuntimeError("RPC not available")
        self.sb.rpc_calls.append((self.name, self.params))
        if self.name == "apply_reputation_delta":
            inbox_id = self.params["p_inbox_id"]
            delta = self.params["p_delta"]
            row = next(r for r in self.sb.store["inboxes"] if r["id"] == inbox_id)
            new_score = max(0.0, min(100.0, (row.get("reputation_score") or 50.0) + delta))
            row["reputation_score"] = new_score
            return _Exec(new_score)
        return _Exec(None)


class FakeSupabase:
    def __init__(self, rpc_should_fail: bool = False):
        self.store: dict[str, list[dict]] = {"inboxes": []}
        self.rpc_should_fail = rpc_should_fail
        self.rpc_calls: list[tuple] = []

    def table(self, name):
        return _Query(self.store, name)

    def rpc(self, name, params):
        return _RpcCall(self, name, params)


def test_bump_reputation_via_rpc_returns_new_score():
    sb = FakeSupabase()
    sb.store["inboxes"] = [{"id": "inbox-1", "reputation_score": 50.0}]

    new_score = bump_reputation(sb, "inbox-1", {"received": 1})

    assert new_score == 50.5
    assert sb.rpc_calls == [("apply_reputation_delta", {"p_inbox_id": "inbox-1", "p_delta": 0.5})]


def test_bump_reputation_sums_multiple_events():
    sb = FakeSupabase()
    sb.store["inboxes"] = [{"id": "inbox-1", "reputation_score": 50.0}]

    bump_reputation(sb, "inbox-1", {"received": 1, "opened": 1})

    _, params = sb.rpc_calls[0]
    assert params["p_delta"] == REPUTATION_DELTA["received"] + REPUTATION_DELTA["opened"]


def test_bump_reputation_unknown_event_contributes_zero():
    sb = FakeSupabase()
    sb.store["inboxes"] = [{"id": "inbox-1", "reputation_score": 50.0}]

    new_score = bump_reputation(sb, "inbox-1", {"totally_unknown_event": 5})

    assert new_score == 50.0


def test_bump_reputation_falls_back_when_rpc_unavailable():
    sb = FakeSupabase(rpc_should_fail=True)
    sb.store["inboxes"] = [{"id": "inbox-1", "reputation_score": 50.0}]

    new_score = bump_reputation(sb, "inbox-1", {"hard_bounce": 1}, current_score=50.0)

    assert new_score == 45.0
    assert sb.store["inboxes"][0]["reputation_score"] == 45.0
    assert sb.rpc_calls == []


def test_bump_reputation_fallback_clamps_at_zero():
    sb = FakeSupabase(rpc_should_fail=True)
    sb.store["inboxes"] = [{"id": "inbox-1", "reputation_score": 5.0}]

    new_score = bump_reputation(sb, "inbox-1", {"spam_complaint": 1}, current_score=5.0)

    assert new_score == 0.0


def test_bump_reputation_fallback_clamps_at_hundred():
    sb = FakeSupabase(rpc_should_fail=True)
    sb.store["inboxes"] = [{"id": "inbox-1", "reputation_score": 99.9}]

    new_score = bump_reputation(sb, "inbox-1", {"spam_rescued": 5}, current_score=99.9)

    assert new_score == 100.0


def test_bump_reputation_fallback_reads_current_score_when_not_supplied():
    sb = FakeSupabase(rpc_should_fail=True)
    sb.store["inboxes"] = [{"id": "inbox-1", "reputation_score": 60.0}]

    new_score = bump_reputation(sb, "inbox-1", {"received": 1})

    assert new_score == 60.5


def test_reputation_delta_matches_claude_md_contract():
    assert REPUTATION_DELTA == {
        "sent": 0.2,
        "received": 0.5,
        "spam_rescued": 1.0,
        "opened": 0.3,
        "soft_bounce": -2.0,
        "hard_bounce": -5.0,
        "spam_complaint": -20.0,
    }


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
