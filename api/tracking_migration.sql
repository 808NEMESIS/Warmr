-- tracking_migration.sql
-- Run this in Supabase SQL Editor.
-- Adds: email_tracking table for open & click events.
-- Safe to run multiple times (IF NOT EXISTS guards).

CREATE TABLE IF NOT EXISTS email_tracking (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       TEXT NOT NULL,
  campaign_id     TEXT,
  lead_id         TEXT,
  lead_email      TEXT,
  event_type      TEXT NOT NULL,              -- open | click
  tracking_token  TEXT NOT NULL,
  link_url        TEXT,                       -- original URL (for clicks)
  ip_address      TEXT,
  user_agent      TEXT,
  created_at      TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tracking_token ON email_tracking(tracking_token);
CREATE INDEX IF NOT EXISTS idx_tracking_campaign ON email_tracking(client_id, campaign_id, event_type);
CREATE INDEX IF NOT EXISTS idx_tracking_lead ON email_tracking(lead_id, event_type);

ALTER TABLE email_tracking ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tracking_client_isolation" ON email_tracking;
CREATE POLICY "tracking_client_isolation"
  ON email_tracking FOR ALL
  USING (client_id = auth.uid()::text);
