"""
utils/job_lock.py — cross-process single-runner lock for scheduled jobs.

Warmr's schedulers overlap: the same job can be fired by launchd
(install_launchd.sh / launchd/*.plist), by cron (crontab_warmr.sh) AND by
n8n workflows hitting the API worker endpoints (api/main.py). Two concurrent
runs of warmup_engine / campaign_scheduler double-send email and corrupt
daily_sent counters. This helper guarantees only ONE run of a given job
executes at a time, regardless of who launched it.

Implementation: a lease row in the `job_locks` table, taken/renewed
atomically by the `try_acquire_job_lock` Postgres function (see
api/critical5_critical6_migration.sql). A lease-based lock — rather than a
session-scoped `pg_try_advisory_lock` — is used deliberately: supabase-py
talks to PostgREST over stateless pooled HTTP, so a session advisory lock
acquired in one request is not held while the Python job runs across later
requests. The lease has a TTL so a crashed run auto-releases.

Usage — wrap each job's main():

    from utils.job_lock import job_lock

    def main() -> None:
        with job_lock("warmup_engine") as acquired:
            if not acquired:
                return            # another run holds the lock; exit quietly
            ...                   # real work here

If Supabase is unreachable or the RPC is missing, `job_lock` FAILS OPEN
(acquired=True) so a DB blip never silently halts all sending — the atomic
send-claim (campaign_scheduler) and the increment_daily_sent RPC are the
second line of defence against duplicates.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# One stable owner id per OS process. Two concurrent processes get distinct
# owners, so only one can hold the lease.
_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

# Default lease length. Should exceed the longest expected run of any job.
DEFAULT_LOCK_TTL_SECONDS = int(os.getenv("JOB_LOCK_TTL_SECONDS", "900"))


def _make_client():
    """Build a service-role Supabase client, or None if unconfigured."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as exc:  # pragma: no cover - import/network guard
        logger.warning("job_lock: could not create Supabase client: %s", exc)
        return None


def try_acquire(
    job_key: str,
    supabase=None,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> bool:
    """
    Attempt to acquire the lease for `job_key`.

    Returns True if this process now holds the lease, False if another live
    run holds it. Fails OPEN (True) on any infrastructure error.
    """
    sb = supabase or _make_client()
    if sb is None:
        logger.warning("job_lock(%s): no Supabase client — failing open.", job_key)
        return True
    try:
        resp = sb.rpc(
            "try_acquire_job_lock",
            {"p_job": job_key, "p_owner": _OWNER, "p_ttl_seconds": ttl_seconds},
        ).execute()
        acquired = bool(resp.data)
        if not acquired:
            logger.info("job_lock(%s): held by another run — skipping.", job_key)
        return acquired
    except Exception as exc:
        # RPC missing / DB unreachable → fail open so we never halt all jobs.
        logger.warning("job_lock(%s): acquire failed (%s) — failing open.", job_key, exc)
        return True


def renew(
    job_key: str,
    supabase=None,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> bool:
    """
    Extend this process's own lease for `job_key` mid-run.

    A run longer than the TTL (e.g. campaign_scheduler processing many leads
    with a 30-180s sleep between sends) can otherwise outlive its own lease:
    try_acquire_job_lock's ON CONFLICT clause now also matches
    `owner = p_owner` (see api/critical5_critical6_migration.sql C5.5), so a
    same-owner re-acquire genuinely pushes expires_at forward rather than
    being a silent no-op. Call this periodically (e.g. every few leads) from
    inside a long-running loop still holding the lock.

    Returns True if the lease is (still) held by this owner, False if it was
    lost (e.g. stolen after this owner's lease actually expired). Fails OPEN
    (True) on any infrastructure error, matching try_acquire's philosophy —
    a DB blip mid-renewal must not abort an already-running job.
    """
    return try_acquire(job_key, supabase=supabase, ttl_seconds=ttl_seconds)


def release(job_key: str, supabase=None) -> None:
    """Release the lease for `job_key` if still owned by this process."""
    sb = supabase or _make_client()
    if sb is None:
        return
    try:
        sb.rpc("release_job_lock", {"p_job": job_key, "p_owner": _OWNER}).execute()
    except Exception as exc:
        logger.debug("job_lock(%s): release failed (%s).", job_key, exc)


@contextmanager
def job_lock(
    job_key: str,
    supabase=None,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> Iterator[bool]:
    """
    Context manager yielding True if the lock was acquired, else False.

    The caller MUST check the yielded value and return early when it is False.
    The lease is released on exit only when it was actually acquired here.
    """
    sb = supabase or _make_client()
    acquired = try_acquire(job_key, supabase=sb, ttl_seconds=ttl_seconds)
    try:
        yield acquired
    finally:
        if acquired:
            release(job_key, supabase=sb)
