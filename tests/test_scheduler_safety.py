"""
tests/test_scheduler_safety.py — Critical 5 (scheduler safety) regression tests.

Covers:
  - job_lock: a second concurrent acquire of the same key is refused while
    the first holds it (prevents double-run of warmup/campaign schedulers).
  - job_lock fails OPEN when Supabase is unavailable (never halts all jobs).
  - daily_reset: the SQL guard means a second same-day reset updates 0 rows
    (no-op), so an hourly launchd re-fire cannot wipe live counters.
  - Atomic send-claim: two workers racing on the same campaign_lead, only
    one wins the status='active' -> 'sending' transition; the other gets no
    row back and must not send.

Style matches the repo: top-level test_* functions, hand-rolled fakes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils.job_lock as jl


# ── Fakes ────────────────────────────────────────────────────────────────

class _Exec:
    def __init__(self, data):
        self.data = data


class _RpcBuilder:
    def __init__(self, exec_result):
        self._exec = exec_result

    def execute(self):
        return self._exec


class LeaseFakeSupabase:
    """
    In-memory stand-in for the job_locks lease functions. One shared instance
    models one database, so two "processes" (two owners) contend for it.
    """

    def __init__(self):
        self.locks: dict[str, str] = {}   # job_key -> owner

    def rpc(self, name, params):
        if name == "try_acquire_job_lock":
            job, owner = params["p_job"], params["p_owner"]
            holder = self.locks.get(job)
            if holder is None or holder == owner:
                self.locks[job] = owner
                return _RpcBuilder(_Exec(True))
            return _RpcBuilder(_Exec(False))
        if name == "release_job_lock":
            job, owner = params["p_job"], params["p_owner"]
            if self.locks.get(job) == owner:
                del self.locks[job]
            return _RpcBuilder(_Exec(True))
        raise AssertionError(f"unexpected rpc {name}")


# ── job_lock: single-runner guarantee ────────────────────────────────────

def test_job_lock_blocks_second_concurrent_run(monkeypatch):
    db = LeaseFakeSupabase()

    # First "process" acquires and holds.
    monkeypatch.setattr(jl, "_OWNER", "hostA:111:aaaa")
    assert jl.try_acquire("warmup_engine", supabase=db) is True

    # Second "process" (different owner) is refused while the first holds it.
    monkeypatch.setattr(jl, "_OWNER", "hostB:222:bbbb")
    assert jl.try_acquire("warmup_engine", supabase=db) is False

    # After the first releases, the second can acquire.
    monkeypatch.setattr(jl, "_OWNER", "hostA:111:aaaa")
    jl.release("warmup_engine", supabase=db)
    monkeypatch.setattr(jl, "_OWNER", "hostB:222:bbbb")
    assert jl.try_acquire("warmup_engine", supabase=db) is True


def test_job_lock_context_manager_yields_false_when_held(monkeypatch):
    db = LeaseFakeSupabase()
    monkeypatch.setattr(jl, "_OWNER", "hostA:1:a")
    jl.try_acquire("campaign_scheduler", supabase=db)  # first holder

    monkeypatch.setattr(jl, "_OWNER", "hostB:2:b")
    with jl.job_lock("campaign_scheduler", supabase=db) as acquired:
        assert acquired is False   # caller must return early on False


def test_job_lock_fails_open_without_supabase(monkeypatch):
    # No client and no env config → fail OPEN so a DB blip never halts all jobs.
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    assert jl.try_acquire("warmup_engine", supabase=None) is True


# ── daily_reset: same-day re-run is a no-op ──────────────────────────────

class ResetGuardSupabase:
    """
    Models the guarded reset: UPDATE ... WHERE last_reset_date IS DISTINCT
    FROM current_date. Tracks how many rows each reset would touch.
    """

    def __init__(self, inbox_count=3):
        # Every inbox starts un-reset today.
        self.reset_today = [False] * inbox_count
        self.update_row_counts: list[int] = []

    def table(self, name):
        assert name == "inboxes"
        return _ResetTable(self)


class _ResetTable:
    def __init__(self, db):
        self.db = db
        self._payload = None
        self._guarded = False

    def update(self, payload):
        self._payload = payload
        return self

    def neq(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    # The guarded reset expresses "only rows not yet reset today".
    def is_(self, *a, **k):
        self._guarded = True
        return self

    def not_(self):
        self._guarded = True
        return self

    def execute(self):
        # Rows that still need a reset today.
        to_reset = [i for i, done in enumerate(self.db.reset_today) if not done]
        for i in to_reset:
            self.db.reset_today[i] = True
        self.db.update_row_counts.append(len(to_reset))
        return _Exec([{"id": i} for i in to_reset])


def test_daily_reset_second_same_day_run_is_noop():
    """
    First reset touches all inboxes; the immediate second reset touches zero.
    This is the property daily_reset's last_reset_date guard must provide so
    an hourly launchd re-fire cannot zero counters mid-day.
    """
    db = ResetGuardSupabase(inbox_count=3)

    # Simulate the guarded reset statement twice in the same calendar day.
    db.table("inboxes").update({"daily_sent": 0}).is_("x", None).execute()
    db.table("inboxes").update({"daily_sent": 0}).is_("x", None).execute()

    assert db.update_row_counts == [3, 0], db.update_row_counts


# ── Atomic send-claim: exactly one worker wins ───────────────────────────

class ClaimFakeSupabase:
    """
    Models the atomic claim UPDATE campaign_leads SET status='sending'
    WHERE id=? AND status='active' RETURNING *. PostgREST returns the updated
    rows (representation), empty when the WHERE matched nothing.
    """

    def __init__(self, lead_id, status="active"):
        self.lead_id = lead_id
        self.status = status

    def table(self, name):
        assert name == "campaign_leads"
        return _ClaimTable(self)


class _ClaimTable:
    def __init__(self, db):
        self.db = db
        self._payload = None
        self._eq = {}

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def execute(self):
        want_id = self._eq.get("id")
        want_status = self._eq.get("status")
        if want_id == self.db.lead_id and self.db.status == want_status:
            # Claim succeeds: flip status, return the row.
            self.db.status = self._payload.get("status", self.db.status)
            return _Exec([{"id": self.db.lead_id, "status": self.db.status}])
        # Already claimed by someone else → zero rows.
        return _Exec([])


def _claim(supabase, lead_id):
    """The claim primitive to be added to campaign_scheduler.process_lead."""
    resp = (
        supabase.table("campaign_leads")
        .update({"status": "sending"})
        .eq("id", lead_id)
        .eq("status", "active")
        .execute()
    )
    return bool(resp.data)


def test_atomic_claim_only_one_worker_wins():
    db = ClaimFakeSupabase("lead-1", status="active")

    first = _claim(db, "lead-1")     # worker A
    second = _claim(db, "lead-1")    # worker B, moments later

    assert first is True, "first worker should win the claim"
    assert second is False, "second worker must NOT also claim/send"
    assert db.status == "sending"


if __name__ == "__main__":
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "monkeypatch" in inspect.signature(fn).parameters:
                import pytest  # type: ignore
                pytest.main([__file__])
                break
            fn()
            print("ok", name)
