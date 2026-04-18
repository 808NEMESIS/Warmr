-- notifications_migration.sql
-- Run this in Supabase SQL Editor to add the notifications table.
-- Safe to run multiple times (IF NOT EXISTS guards).

CREATE TABLE IF NOT EXISTS notifications (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT NOT NULL,
  type        TEXT NOT NULL,
  entity_id   TEXT,
  entity_type TEXT,
  message     TEXT NOT NULL,
  priority    TEXT DEFAULT 'medium',
  read        BOOLEAN DEFAULT false,
  timestamp   TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notifications_client ON notifications(client_id, read, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_entity ON notifications(entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(client_id) WHERE read = false;

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "notifications_client_isolation" ON notifications;
CREATE POLICY "notifications_client_isolation"
  ON notifications FOR ALL
  USING (client_id = auth.uid()::text);
