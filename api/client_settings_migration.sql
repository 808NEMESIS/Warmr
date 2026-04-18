-- client_settings_migration.sql
-- Per-client global settings (booking link, sender name, signature, etc.)
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS client_settings (
  client_id        TEXT PRIMARY KEY,
  booking_url      TEXT,                  -- Cal.com / Calendly / etc.
  sender_name      TEXT,                  -- default sender name for emails
  email_signature  TEXT,                  -- HTML/plain signature appended to all sends
  company_name     TEXT,
  reply_to_email   TEXT,                  -- override Reply-To header
  unsubscribe_text TEXT DEFAULT 'Niet meer ontvangen? Uitschrijven',
  created_at       TIMESTAMP DEFAULT now(),
  updated_at       TIMESTAMP DEFAULT now()
);

ALTER TABLE client_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "client_settings_isolation" ON client_settings;
CREATE POLICY "client_settings_isolation"
  ON client_settings FOR ALL
  USING (client_id = auth.uid()::text);
