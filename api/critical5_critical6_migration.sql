-- =====================================================================
-- critical5_critical6_migration.sql
-- Incremental migration for WARMR_ENTERPRISE_AUDIT_2026-07
--   Critical 5 — Scheduler safety
--   Critical 6 — Cost control
--
-- Idempotent: safe to run multiple times (IF NOT EXISTS / OR REPLACE).
-- Apply against the same Supabase project as full_schema.sql.
-- =====================================================================


-- ---------------------------------------------------------------------
-- C5.1  increment_daily_sent(inbox_uuid uuid) -> integer
--
-- warmup_engine.update_daily_sent() (warmup_engine.py:188) calls:
--     supabase.rpc("increment_daily_sent", {"inbox_uuid": inbox_id})
-- and expects an integer back (warmup_engine.py:189). The RPC never
-- existed, so every call fell through to the non-atomic read-modify-write
-- fallback (warmup_engine.py:191-198), which loses increments under
-- concurrent runs. Parameter name MUST stay `inbox_uuid` to match the call.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION increment_daily_sent(inbox_uuid uuid)
RETURNS integer
LANGUAGE sql
AS $$
    UPDATE inboxes
       SET daily_sent = COALESCE(daily_sent, 0) + 1,
           updated_at = now()
     WHERE id = inbox_uuid
    RETURNING daily_sent;
$$;


-- ---------------------------------------------------------------------
-- C5.2  daily_reset date-guard column
--
-- daily_reset.main() (daily_reset.py:48-51) unconditionally sets
-- daily_sent=0 for every non-retired inbox. Under install_launchd.sh:63
-- it runs HOURLY (StartInterval 3600), so mid-day it wipes counters that
-- warmup_engine just incremented -> daily caps are defeated.
-- last_reset_date lets the reset become a no-op after the first run today.
-- ---------------------------------------------------------------------
ALTER TABLE inboxes ADD COLUMN IF NOT EXISTS last_reset_date DATE;


-- ---------------------------------------------------------------------
-- C5.3  Idempotency backstop on email_events
--
-- NOTE: the audit asked for UNIQUE(campaign_lead_id, sequence_step), but
-- those columns DO NOT EXIST on email_events. Verified against
-- full_schema.sql: email_events has (campaign_id, lead_id,
-- sequence_step_id, inbox_id, event_type, ab_variant, message_id,
-- timestamp). The correct backstop is one 'sent' row per
-- (campaign_id, lead_id, sequence_step_id). A partial unique index makes
-- a duplicate 'sent' insert fail, so a double-fired worker cannot double-log
-- (and, combined with the atomic claim in C5.4, cannot double-send).
-- ---------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS email_events_sent_once_idx
    ON email_events (campaign_id, lead_id, sequence_step_id)
    WHERE event_type = 'sent';


-- ---------------------------------------------------------------------
-- C5.5  Lease-based job lock (single-runner guarantee)
--
-- Why not session-level pg_try_advisory_lock via an RPC:
--   supabase-py talks to PostgREST over stateless HTTP behind a pooler.
--   Each .rpc() call is a separate transaction on a possibly-different
--   pooled backend, so a *session*-scoped advisory lock acquired in one
--   HTTP call is NOT held while the Python job runs across later calls,
--   and can leak to the next request that reuses the backend. A lease
--   row is the reliable cross-process lock for this architecture.
--
-- try_acquire_job_lock: atomically inserts or steals an expired lease.
-- Returns TRUE only if THIS owner now holds the lease.
-- release_job_lock: releases only if still owned by this owner.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_locks (
    job_key     TEXT        PRIMARY KEY,
    owner       TEXT        NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION try_acquire_job_lock(
    p_job   TEXT,
    p_owner TEXT,
    p_ttl_seconds INT DEFAULT 900
) RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    v_now TIMESTAMPTZ := now();
BEGIN
    INSERT INTO job_locks (job_key, owner, acquired_at, expires_at)
    VALUES (p_job, p_owner, v_now, v_now + make_interval(secs => p_ttl_seconds))
    ON CONFLICT (job_key) DO UPDATE
        SET owner       = EXCLUDED.owner,
            acquired_at = EXCLUDED.acquired_at,
            expires_at  = EXCLUDED.expires_at
      -- Steal only if the current lease has expired, OR extend our OWN
      -- still-live lease (renewal). Without the owner clause, a same-owner
      -- re-acquire mid-run is a silent no-op: the final EXISTS check below
      -- still returns true (we already own it), but expires_at is never
      -- pushed forward — so a naive "call try_acquire again" heartbeat from
      -- utils.job_lock.renew() would NOT actually extend the TTL, and a run
      -- longer than p_ttl_seconds could still have its lease stolen by a
      -- competing scheduler while still legitimately executing.
      WHERE job_locks.expires_at < v_now OR job_locks.owner = p_owner;

    -- We hold the lock iff the row is now owned by us with a live lease.
    RETURN EXISTS (
        SELECT 1 FROM job_locks
         WHERE job_key = p_job
           AND owner   = p_owner
           AND expires_at > v_now
    );
END;
$$;

CREATE OR REPLACE FUNCTION release_job_lock(
    p_job   TEXT,
    p_owner TEXT
) RETURNS boolean
LANGUAGE sql
AS $$
    DELETE FROM job_locks
     WHERE job_key = p_job AND owner = p_owner
    RETURNING true;
$$;


-- ---------------------------------------------------------------------
-- C5.6  Sending reaper — status_changed_at
--
-- The atomic claim (campaign_scheduler.py, C5.4) flips campaign_leads.status
-- 'active' -> 'sending' but writes no timestamp. campaign_leads has ZERO
-- timestamp columns (id, campaign_id, lead_id, current_step, next_send_at,
-- status, thread_message_id), so a lead that gets claimed and then never
-- completes (process crash, worker killed) is stranded in 'sending' forever
-- — there is no way to tell how long it has been stuck, and therefore no way
-- to age it out. status_changed_at is written by both the claim UPDATE and
-- the SMTP-failure revert UPDATE (see campaign_scheduler.py), and read by
-- reap_stranded_sends.py to revert leads stuck in 'sending' past a timeout
-- back to 'active' so the campaign scheduler can retry them.
-- ---------------------------------------------------------------------
ALTER TABLE campaign_leads ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ DEFAULT now();


-- ---------------------------------------------------------------------
-- C6.6  Aggregate daily API spend (replaces Python full-scan+sum)
--
-- cost_tracker.get_daily_spend() (utils/cost_tracker.py:79-88) SELECTs
-- every api_cost_log row for today and sums in Python. As the table grows
-- this pulls thousands of rows per Claude call. Push the sum into Postgres.
-- api_cost_log.client_id is TEXT (full_schema.sql), so p_client is TEXT.
-- Uses idx_api_cost_log_client_date / idx_api_cost_log_date (already present).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION get_daily_api_spend(p_client TEXT DEFAULT NULL)
RETURNS numeric
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(SUM(cost_eur), 0)::numeric
      FROM api_cost_log
     WHERE date = current_date
       AND (p_client IS NULL OR client_id = p_client);
$$;


-- ---------------------------------------------------------------------
-- C6.7  Warmup maintenance mode
--
-- auto_promote.check_and_promote_inbox() flips status 'warmup'->'ready'
-- but leaves warmup_active=true (auto_promote.py:154-158), so a ready
-- inbox keeps burning full-volume LIVE Claude warmup forever. warmup_mode
-- lets the engine degrade ready inboxes to low-volume template traffic.
--   full        — normal week-by-week warmup (default, back-compat)
--   maintenance — low volume, templates only, zero live LLM
--   off         — no warmup sends
-- ---------------------------------------------------------------------
ALTER TABLE inboxes ADD COLUMN IF NOT EXISTS warmup_mode TEXT NOT NULL DEFAULT 'full';

-- Backfill: inboxes already promoted to 'ready' go straight to maintenance.
UPDATE inboxes SET warmup_mode = 'maintenance'
 WHERE status = 'ready' AND warmup_mode = 'full';
