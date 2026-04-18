-- crm_migration.sql
-- Adds: crm_integrations table for HubSpot/Pipedrive/Salesforce sync.
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS crm_integrations (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id     TEXT NOT NULL,
  provider      TEXT NOT NULL,           -- hubspot | pipedrive | salesforce | webhook
  api_key       TEXT,                     -- encrypted recommended in production
  webhook_url   TEXT,
  config        JSONB DEFAULT '{}',       -- provider-specific settings (pipeline_id, stage_id, etc.)
  active        BOOLEAN DEFAULT true,
  sync_on_reply BOOLEAN DEFAULT true,
  sync_on_interested BOOLEAN DEFAULT true,
  sync_on_meeting BOOLEAN DEFAULT true,
  created_at    TIMESTAMP DEFAULT now(),
  updated_at    TIMESTAMP DEFAULT now(),
  UNIQUE(client_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_crm_client ON crm_integrations(client_id, active);

ALTER TABLE crm_integrations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "crm_client_isolation" ON crm_integrations;
CREATE POLICY "crm_client_isolation"
  ON crm_integrations FOR ALL
  USING (client_id = auth.uid()::text);


CREATE TABLE IF NOT EXISTS crm_sync_log (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id     TEXT NOT NULL,
  integration_id TEXT,
  lead_id       TEXT,
  event_type    TEXT,                  -- reply | interested | meeting | bounce
  status        TEXT,                  -- success | failed
  response      TEXT,
  created_at    TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crm_log_client ON crm_sync_log(client_id, created_at DESC);

ALTER TABLE crm_sync_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "crm_log_client_isolation" ON crm_sync_log;
CREATE POLICY "crm_log_client_isolation"
  ON crm_sync_log FOR ALL
  USING (client_id = auth.uid()::text);
