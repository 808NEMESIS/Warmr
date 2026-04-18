-- ============================================================
-- Warmr — Intelligence layer migration
-- Run AFTER api/enrichment_migration.sql
-- ============================================================

-- ------------------------------------------------------------
-- warmup_network_accounts
-- Tracks health of warmup network Gmail accounts loaded from .env.
-- diagnostics_engine checks IMAP login health every 6 hours.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warmup_network_accounts (
  id                UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id         TEXT,                              -- null = shared network across all clients
  email             TEXT      NOT NULL UNIQUE,
  provider          TEXT      NOT NULL DEFAULT 'gmail',
  status            TEXT      NOT NULL DEFAULT 'active', -- active | inactive | suspended
  last_login_check  TIMESTAMP,
  last_login_success TIMESTAMP,
  failure_count     INT       NOT NULL DEFAULT 0,
  created_at        TIMESTAMP DEFAULT now()
);

CREATE INDEX warmup_network_accounts_status_idx ON warmup_network_accounts (status);
CREATE INDEX warmup_network_accounts_client_idx ON warmup_network_accounts (client_id);

-- No RLS — warmup network is managed by service role only

-- ------------------------------------------------------------
-- network_health_log
-- Daily snapshot of warmup network health per client.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS network_health_log (
  id              UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       TEXT,
  total_accounts  INT       NOT NULL,
  active_accounts INT       NOT NULL,
  health_score    FLOAT     NOT NULL,    -- active / total * 100
  timestamp       TIMESTAMP DEFAULT now()
);

CREATE INDEX network_health_log_client_idx ON network_health_log (client_id);
CREATE INDEX network_health_log_ts_idx     ON network_health_log (timestamp);

-- ------------------------------------------------------------
-- diagnostics_log
-- Audit trail of every diagnostic check run.
-- Stores structured results for trending and historical comparison.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diagnostics_log (
  id          UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT,
  check_type  TEXT      NOT NULL,   -- reputation_drift|network_health|smtp_pattern|forecast
  entity_id   UUID,                 -- inbox_id, domain_id, etc.
  result      TEXT      NOT NULL,   -- ok|warning|critical
  details     JSONB,
  timestamp   TIMESTAMP DEFAULT now()
);

CREATE INDEX diagnostics_log_client_idx     ON diagnostics_log (client_id);
CREATE INDEX diagnostics_log_check_type_idx ON diagnostics_log (check_type);
CREATE INDEX diagnostics_log_entity_idx     ON diagnostics_log (entity_id);
CREATE INDEX diagnostics_log_ts_idx         ON diagnostics_log (timestamp DESC);

ALTER TABLE diagnostics_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "diagnostics_log_isolation"
  ON diagnostics_log FOR SELECT
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- sequence_suggestions
-- Claude-generated improvement suggestions for underperforming sequence steps.
-- Surfaced as dismissable cards on the campaign detail page.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sequence_suggestions (
  id                   UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id            TEXT      NOT NULL,
  campaign_id          UUID      REFERENCES campaigns(id)       ON DELETE CASCADE,
  sequence_step_id     UUID      REFERENCES sequence_steps(id)  ON DELETE CASCADE,
  suggestion_type      TEXT      NOT NULL, -- subject_line|opening|structure|full_rewrite
  current_performance  JSONB,              -- {open_rate, reply_rate, sends}
  suggestion_text      TEXT,
  claude_reasoning     TEXT,
  status               TEXT      NOT NULL DEFAULT 'pending', -- pending|applied|dismissed
  created_at           TIMESTAMP DEFAULT now()
);

CREATE INDEX sequence_suggestions_campaign_idx ON sequence_suggestions (campaign_id);
CREATE INDEX sequence_suggestions_status_idx   ON sequence_suggestions (status) WHERE status = 'pending';
CREATE INDEX sequence_suggestions_client_idx   ON sequence_suggestions (client_id);

ALTER TABLE sequence_suggestions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "sequence_suggestions_isolation"
  ON sequence_suggestions FOR ALL
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- Extend analytics_cache entity_type values (documentation only)
-- New entity_types used by the intelligence layer:
--   inbox_forecast            — 7-day linear regression projection per inbox
--   send_time_recommendation  — top 3 send time slots per campaign
--   provider_match_rate       — weekly provider routing stats
-- ------------------------------------------------------------

-- Add inbox_auto_pause_count to inboxes for escalation tracking
ALTER TABLE inboxes
  ADD COLUMN IF NOT EXISTS auto_pause_count_24h INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS auto_pause_reset_at  TIMESTAMP;
