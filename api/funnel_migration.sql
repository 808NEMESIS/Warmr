-- funnel_migration.sql
-- Adds funnel stage tracking and reply routing rules.

-- ── Funnel stage on leads ────────────────────────────────────────────────────
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funnel_stage TEXT DEFAULT 'cold';
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funnel_entered_at TIMESTAMP;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funnel_updated_at TIMESTAMP;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS auto_reply_sent BOOLEAN DEFAULT false;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS nurture_until TIMESTAMP;

-- funnel_stage values: cold | warm | hot | meeting | nurture | lost | unsubscribed

CREATE INDEX IF NOT EXISTS idx_leads_funnel ON leads(client_id, funnel_stage);

-- ── Reply routing rules (per client, configurable) ───────────────────────────
CREATE TABLE IF NOT EXISTS reply_routing_rules (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       TEXT NOT NULL,
  classification  TEXT NOT NULL,
  action          TEXT NOT NULL,
  auto_reply_template TEXT,
  notify          BOOLEAN DEFAULT true,
  active          BOOLEAN DEFAULT true,
  created_at      TIMESTAMP DEFAULT now(),
  UNIQUE(client_id, classification)
);

-- classification: interested | not_interested | question | out_of_office | referral | unsubscribe | other
-- action: send_calendar | notify_only | stop_sequence | reschedule | create_referral | suppress

ALTER TABLE reply_routing_rules ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "reply_routing_client_isolation" ON reply_routing_rules;
CREATE POLICY "reply_routing_client_isolation"
  ON reply_routing_rules FOR ALL
  USING (client_id = auth.uid()::text);

-- ── Funnel analytics cache ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS funnel_analytics (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT NOT NULL,
  date        DATE NOT NULL,
  stage       TEXT NOT NULL,
  lead_count  INT DEFAULT 0,
  moved_in    INT DEFAULT 0,
  moved_out   INT DEFAULT 0,
  created_at  TIMESTAMP DEFAULT now(),
  UNIQUE(client_id, date, stage)
);

CREATE INDEX IF NOT EXISTS idx_funnel_analytics ON funnel_analytics(client_id, date);

ALTER TABLE funnel_analytics ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "funnel_analytics_client_isolation" ON funnel_analytics;
CREATE POLICY "funnel_analytics_client_isolation"
  ON funnel_analytics FOR ALL
  USING (client_id = auth.uid()::text);
