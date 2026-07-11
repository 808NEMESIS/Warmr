-- ============================================================
-- Warmr — Retention Migration (Fase 2, Track 2, item 8)
--
-- Backs retention_engine.py:
--   - client_settings.retention_days: per-client override for how long
--     event/tracking data is kept. NULL = fall back to the global default
--     (WARMR_DEFAULT_RETENTION_DAYS env var).
--   - clients.closed_at: set when a client account is closed. NULL = active
--     account. retention_engine.py hard-deletes accounts whose closed_at is
--     older than the grace period (default 30 days), per CLAUDE.md's GDPR
--     section ("Delete all data for opted-out contacts within 30 days").
--
-- Idempotent: safe to run multiple times.
-- Run in the Supabase SQL editor with the service role.
-- ============================================================

ALTER TABLE client_settings ADD COLUMN IF NOT EXISTS retention_days INT;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP;

-- ------------------------------------------------------------
-- Verification (run manually after applying):
--   SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--   WHERE (table_name = 'client_settings' AND column_name = 'retention_days')
--      OR (table_name = 'clients' AND column_name = 'closed_at');
--   -- expect 2 rows, both nullable
-- ------------------------------------------------------------
