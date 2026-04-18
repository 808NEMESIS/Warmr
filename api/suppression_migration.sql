-- suppression_migration.sql
-- Run this in Supabase SQL Editor.
-- Adds: suppression_list table + unsubscribe_tokens table
-- Safe to run multiple times (IF NOT EXISTS guards).

-- ── Suppression list (do-not-contact) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS suppression_list (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT NOT NULL,
  email       TEXT NOT NULL,
  domain      TEXT,
  reason      TEXT DEFAULT 'unsubscribe',   -- unsubscribe | bounce | manual | complaint
  source      TEXT,                          -- campaign_id or 'manual' or 'import'
  created_at  TIMESTAMP DEFAULT now(),
  UNIQUE(client_id, email)
);

CREATE INDEX IF NOT EXISTS idx_suppression_client ON suppression_list(client_id, email);
CREATE INDEX IF NOT EXISTS idx_suppression_domain ON suppression_list(client_id, domain);

ALTER TABLE suppression_list ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "suppression_client_isolation" ON suppression_list;
CREATE POLICY "suppression_client_isolation"
  ON suppression_list FOR ALL
  USING (client_id = auth.uid()::text);

-- ── Unsubscribe tokens (one-time use, maps to lead + campaign) ───────────────
CREATE TABLE IF NOT EXISTS unsubscribe_tokens (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token        TEXT NOT NULL UNIQUE,
  client_id    TEXT NOT NULL,
  lead_id      TEXT NOT NULL,
  lead_email   TEXT NOT NULL,
  campaign_id  TEXT,
  used         BOOLEAN DEFAULT false,
  created_at   TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_unsub_token ON unsubscribe_tokens(token);

-- No RLS on unsubscribe_tokens — needs public read for unsubscribe page
-- Service role handles writes
