-- ============================================================
-- Warmr — Reputation Migration (Fase 3, KPI-tracking fixes, item 2)
--
-- Backs utils/reputation.py's bump_reputation() and bounce_handler.py's
-- spam_complaints increment. Both were previously non-atomic
-- read-then-write: warmup sends, IMAP receives, and bounce handling run on
-- independent schedules (20/10/30 min respectively) against the same
-- inbox, so two concurrent updates could silently lose one's contribution
-- ("lost update"). Same pattern as increment_daily_sent (Fase 0/1,
-- api/critical5_critical6_migration.sql) — code already falls back to the
-- old non-atomic behavior if these functions aren't applied yet, so this
-- can be rolled out independently of the code deploy.
--
-- Idempotent: safe to run multiple times.
-- Run in the Supabase SQL editor with the service role.
-- ============================================================

CREATE OR REPLACE FUNCTION apply_reputation_delta(p_inbox_id uuid, p_delta numeric)
RETURNS numeric
LANGUAGE sql
AS $$
    UPDATE inboxes
       SET reputation_score = GREATEST(0, LEAST(100, COALESCE(reputation_score, 50) + p_delta)),
           updated_at = now()
     WHERE id = p_inbox_id
    RETURNING reputation_score;
$$;

CREATE OR REPLACE FUNCTION increment_spam_complaints(p_inbox_id uuid)
RETURNS integer
LANGUAGE sql
AS $$
    UPDATE inboxes
       SET spam_complaints = COALESCE(spam_complaints, 0) + 1,
           last_spam_incident = now()
     WHERE id = p_inbox_id
    RETURNING spam_complaints;
$$;

-- ------------------------------------------------------------
-- Verification (run manually after applying):
--   SELECT proname FROM pg_proc
--   WHERE proname IN ('apply_reputation_delta', 'increment_spam_complaints');
--   -- expect 2 rows
--
--   -- Smoke test (replace with a real inbox id, then check the reputation_score
--   -- moved by exactly +0.5 and clamped correctly):
--   SELECT apply_reputation_delta('00000000-0000-0000-0000-000000000000'::uuid, 0.5);
-- ------------------------------------------------------------
