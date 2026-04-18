-- ============================================================
-- Warmr — Public API + Webhook system migration
-- Run AFTER api/migrations.sql
-- ============================================================

-- ------------------------------------------------------------
-- api_keys
-- Machine-to-machine authentication for external integrations.
-- The raw key is shown once on creation — only the SHA-256
-- hash is stored. Format: wrmr_<64 hex chars>
-- ------------------------------------------------------------
CREATE TABLE api_keys (
  id           UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id    TEXT      NOT NULL,
  key_hash     TEXT      NOT NULL UNIQUE,       -- SHA-256(raw_key), hex-encoded
  name         TEXT      NOT NULL,              -- "LeadGen tool", "Zapier", etc.
  permissions  TEXT[]    NOT NULL DEFAULT '{}', -- read_leads|write_leads|trigger_campaigns|read_analytics
  last_used_at TIMESTAMP,
  created_at   TIMESTAMP DEFAULT now()
);

CREATE INDEX api_keys_client_id_idx ON api_keys (client_id);
CREATE INDEX api_keys_hash_idx      ON api_keys (key_hash);

-- RLS: clients can only manage their own API keys
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY "api_keys_isolation"
  ON api_keys FOR ALL
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- webhooks
-- Registered webhook endpoints per client.
-- Each webhook subscribes to one or more event types.
-- ------------------------------------------------------------
CREATE TABLE webhooks (
  id         UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id  TEXT      NOT NULL,
  url        TEXT      NOT NULL,
  events     TEXT[]    NOT NULL DEFAULT '{}',  -- see supported events below
  secret     TEXT      NOT NULL,               -- HMAC-SHA256 signing secret (random, shown once)
  active     BOOLEAN   DEFAULT true,
  created_at TIMESTAMP DEFAULT now()
);

-- Supported events:
--   lead.replied          — a prospect replied to a campaign email
--   lead.interested       — reply classified as "interested"
--   lead.bounced          — hard or soft bounce for a lead
--   lead.unsubscribed     — lead opted out
--   inbox.warmup_complete — inbox reached ready status (rep >= 70)
--   campaign.completed    — all leads in a campaign reached final step

CREATE INDEX webhooks_client_id_idx ON webhooks (client_id);

ALTER TABLE webhooks ENABLE ROW LEVEL SECURITY;
CREATE POLICY "webhooks_isolation"
  ON webhooks FOR ALL
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- webhook_logs
-- Delivery audit trail. Every dispatch attempt is logged here.
-- Used by the dispatcher for retry logic and the dashboard.
-- ------------------------------------------------------------
CREATE TABLE webhook_logs (
  id              UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  webhook_id      UUID      REFERENCES webhooks(id) ON DELETE CASCADE,
  client_id       TEXT,                          -- denormalised for fast client queries
  event_type      TEXT      NOT NULL,
  payload         JSONB,
  response_status INT,                           -- HTTP status returned by the target
  response_body   TEXT,                          -- first 500 chars of response
  attempt_count   INT       DEFAULT 1,
  next_retry_at   TIMESTAMP,                     -- null = no retry needed
  success         BOOLEAN   DEFAULT false,
  timestamp       TIMESTAMP DEFAULT now()
);

CREATE INDEX webhook_logs_webhook_id_idx    ON webhook_logs (webhook_id);
CREATE INDEX webhook_logs_client_id_idx     ON webhook_logs (client_id);
CREATE INDEX webhook_logs_next_retry_at_idx ON webhook_logs (next_retry_at) WHERE success = false;

-- webhook_logs uses service role only — no RLS needed (backend-only table)
-- If you want frontend visibility, add:
-- ALTER TABLE webhook_logs ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "webhook_logs_isolation"
--   ON webhook_logs FOR SELECT
--   USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- webhook_events
-- Event queue: the backend writes here, the dispatcher reads.
-- Decouples event emission from delivery.
-- ------------------------------------------------------------
CREATE TABLE webhook_events (
  id          UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT      NOT NULL,
  event_type  TEXT      NOT NULL,
  payload     JSONB     NOT NULL,
  dispatched  BOOLEAN   DEFAULT false,
  created_at  TIMESTAMP DEFAULT now()
);

CREATE INDEX webhook_events_pending_idx ON webhook_events (created_at) WHERE dispatched = false;
CREATE INDEX webhook_events_client_idx  ON webhook_events (client_id)  WHERE dispatched = false;
