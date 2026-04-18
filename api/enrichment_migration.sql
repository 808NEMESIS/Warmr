-- ============================================================
-- Warmr — Enrichment system migration
-- Run AFTER api/public_migration.sql
-- ============================================================

-- Add verified / enriched columns to leads (idempotent)
ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS verified    BOOLEAN DEFAULT NULL,   -- null = unchecked, true/false = result
  ADD COLUMN IF NOT EXISTS enriched    BOOLEAN DEFAULT false,
  ADD COLUMN IF NOT EXISTS custom_fields JSONB  DEFAULT '{}';

-- ------------------------------------------------------------
-- enrichment_queue
-- Async queue for lead enrichment jobs.
-- The enrichment worker polls this table for pending rows.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrichment_queue (
  id            UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id       UUID      REFERENCES leads(id) ON DELETE CASCADE,
  client_id     TEXT      NOT NULL,           -- denormalised for fast priority queries
  status        TEXT      NOT NULL DEFAULT 'pending', -- pending|processing|completed|failed
  priority      INT       NOT NULL DEFAULT 5, -- lower = higher priority (1 = highest)
  attempts      INT       NOT NULL DEFAULT 0,
  error_message TEXT,
  queued_at     TIMESTAMP DEFAULT now(),
  started_at    TIMESTAMP,
  completed_at  TIMESTAMP
);

CREATE INDEX enrichment_queue_pending_idx
  ON enrichment_queue (priority, queued_at)
  WHERE status = 'pending';

CREATE INDEX enrichment_queue_lead_id_idx
  ON enrichment_queue (lead_id);

CREATE INDEX enrichment_queue_client_id_idx
  ON enrichment_queue (client_id);

-- Unique constraint: one pending/processing job per lead at a time
CREATE UNIQUE INDEX enrichment_queue_lead_active_idx
  ON enrichment_queue (lead_id)
  WHERE status IN ('pending', 'processing');

-- RLS: clients can only see their own queue entries
ALTER TABLE enrichment_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY "enrichment_queue_isolation"
  ON enrichment_queue FOR ALL
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- Add lead.enriched to the valid webhook events (documentation only —
-- the actual check is in public_api.py VALID_EVENTS frozenset)
-- ------------------------------------------------------------
-- Supported events (updated):
--   lead.replied          — a prospect replied to a campaign email
--   lead.interested       — reply classified as "interested"
--   lead.bounced          — hard or soft bounce for a lead
--   lead.unsubscribed     — lead opted out
--   lead.enriched         — enrichment pipeline completed for a lead  ← NEW
--   inbox.warmup_complete — inbox reached ready status (rep >= 70)
--   campaign.completed    — all leads in a campaign reached final step
