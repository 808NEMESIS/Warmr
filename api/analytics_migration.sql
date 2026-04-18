-- ============================================================
-- Warmr — Analytics Cache Migration
-- Run AFTER api/migrations.sql.
-- ============================================================

-- ------------------------------------------------------------
-- analytics_cache
-- Pre-aggregated daily metric snapshots.
-- One row per (client_id, entity_type, entity_id, date).
-- The analytics_engine.py script writes here; the API reads from here.
-- Direct real-time queries fall back to email_events and warmup_logs
-- when no cache row exists yet for a given date.
-- ------------------------------------------------------------
CREATE TABLE analytics_cache (
  id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Stable row ID
  client_id   TEXT    NOT NULL,                               -- Foreign key to clients.id
  entity_type TEXT    NOT NULL,                               -- campaign | inbox | sequence_step
  entity_id   UUID    NOT NULL,                               -- UUID of the campaign, inbox, or sequence_step
  date        DATE    NOT NULL,                               -- Calendar date this row covers (UTC)
  metrics     JSONB   NOT NULL DEFAULT '{}',                  -- All metric values as a JSONB dict (see analytics_engine.py for schema)
  updated_at  TIMESTAMP DEFAULT now()                         -- When this cache row was last written

  -- Unique per entity per day — allows efficient UPSERT
  , CONSTRAINT analytics_cache_unique UNIQUE (entity_id, entity_type, date)
);

-- Index for the primary API query pattern: client + entity_type + date range
CREATE INDEX analytics_cache_client_idx ON analytics_cache (client_id, entity_type, date DESC);

ALTER TABLE analytics_cache ENABLE ROW LEVEL SECURITY;
CREATE POLICY "analytics_cache_isolation"
  ON analytics_cache FOR ALL
  USING (client_id = auth.uid()::text);
