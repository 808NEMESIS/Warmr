-- ============================================================
-- Warmr — Full Database Schema
-- Single file combining all migrations in dependency order.
-- Run once in the Supabase SQL Editor to set up the full schema.
--
-- Order:
--   1. Core tables (clients, inboxes, domains, warmup_logs, sending_schedule, bounce_log)
--   2. Campaign engine (campaigns, sequence_steps, leads, campaign_leads, email_events, reply_inbox)
--   3. Analytics cache
--   4. Admin role columns + RLS updates
--   5. Public API + Webhook system
--   6. Enrichment system
--   7. Intelligence layer (diagnostics, network health, sequence suggestions)
--   8. Deliverability tools (placement tests, content scores, DNS monitor, blacklist recovery)
--   9. Personal workflow (decision log, experiments)
-- ============================================================


-- ============================================================
-- 1. CORE TABLES
-- ============================================================

-- ------------------------------------------------------------
-- clients
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clients (
  id           UUID    PRIMARY KEY REFERENCES auth.users(id),
  company_name TEXT,
  email        TEXT,
  plan         TEXT    DEFAULT 'trial',
  max_inboxes  INT     DEFAULT 5,
  max_domains  INT     DEFAULT 2,
  is_admin     BOOLEAN NOT NULL DEFAULT false,
  suspended    BOOLEAN NOT NULL DEFAULT false,
  notes        TEXT,
  created_at   TIMESTAMP DEFAULT now()
);

-- ------------------------------------------------------------
-- inboxes
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inboxes (
  id                    UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  email                 TEXT    NOT NULL UNIQUE,
  domain                TEXT    NOT NULL,
  provider              TEXT,
  warmup_active         BOOLEAN DEFAULT true,
  warmup_start_date     DATE,
  daily_warmup_target   INT     DEFAULT 10,
  daily_campaign_target INT     DEFAULT 0,
  daily_sent            INT     DEFAULT 0,
  reputation_score      FLOAT   DEFAULT 50,
  open_rate             FLOAT   DEFAULT 0,
  reply_rate            FLOAT   DEFAULT 0,
  spam_rescues          INT     DEFAULT 0,
  spam_complaints       INT     DEFAULT 0,
  last_spam_incident    TIMESTAMP,
  status                TEXT    DEFAULT 'warmup',
  client_id             TEXT,
  notes                 TEXT,
  auto_pause_count_24h  INT     NOT NULL DEFAULT 0,
  auto_pause_reset_at   TIMESTAMP,
  created_at            TIMESTAMP DEFAULT now(),
  updated_at            TIMESTAMP DEFAULT now()
);

-- ------------------------------------------------------------
-- domains
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS domains (
  id                   UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  domain               TEXT    NOT NULL UNIQUE,
  registrar            TEXT,
  tld                  TEXT,
  spf_configured       BOOLEAN DEFAULT false,
  dkim_configured      BOOLEAN DEFAULT false,
  dmarc_phase          TEXT    DEFAULT 'none',
  blacklisted          BOOLEAN DEFAULT false,
  last_blacklist_check TIMESTAMP,
  spf_expected         TEXT,
  dkim_selector        TEXT    DEFAULT 'google',
  last_dns_check       TIMESTAMP,
  dns_check_status     TEXT    DEFAULT 'unknown',
  client_id            TEXT,
  created_at           TIMESTAMP DEFAULT now()
);

-- ------------------------------------------------------------
-- warmup_logs
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warmup_logs (
  id                       UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  inbox_id                 UUID    REFERENCES inboxes(id),
  action                   TEXT    NOT NULL,
  counterpart_email        TEXT,
  subject                  TEXT,
  warmup_week              INT,
  daily_volume             INT,
  reputation_score_at_time FLOAT,
  landed_in_spam           BOOLEAN DEFAULT false,
  was_rescued              BOOLEAN DEFAULT false,
  was_replied              BOOLEAN DEFAULT false,
  notes                    TEXT,
  timestamp                TIMESTAMP DEFAULT now()
);

-- ------------------------------------------------------------
-- sending_schedule
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sending_schedule (
  id                  UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  inbox_id            UUID    REFERENCES inboxes(id),
  campaign_id         UUID,
  lead_email          TEXT,
  lead_name           TEXT,
  company_name        TEXT,
  personalized_opener TEXT,
  email_body          TEXT,
  subject             TEXT,
  sequence_step       INT     DEFAULT 1,
  scheduled_at        TIMESTAMP,
  sent_at             TIMESTAMP,
  status              TEXT    DEFAULT 'pending',
  client_id           TEXT
);

-- ------------------------------------------------------------
-- bounce_log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bounce_log (
  id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  inbox_id          UUID    REFERENCES inboxes(id),
  lead_email        TEXT,
  bounce_type       TEXT,
  raw_response      TEXT,
  soft_bounce_count INT     DEFAULT 0,
  resolved          BOOLEAN DEFAULT false,
  timestamp         TIMESTAMP DEFAULT now()
);


-- ============================================================
-- 2. CAMPAIGN ENGINE
-- ============================================================

-- ------------------------------------------------------------
-- campaigns
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
  id                  UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id           TEXT,
  name                TEXT    NOT NULL,
  status              TEXT    DEFAULT 'draft',
  language            TEXT    DEFAULT 'nl',
  daily_limit         INT     DEFAULT 50,
  timezone            TEXT    DEFAULT 'Europe/Amsterdam',
  send_days           TEXT    DEFAULT '1,2,3,4,5',
  send_window_start   TIME    DEFAULT '08:00',
  send_window_end     TIME    DEFAULT '17:00',
  stop_on_reply       BOOLEAN DEFAULT true,
  stop_on_unsubscribe BOOLEAN DEFAULT true,
  bounce_threshold    FLOAT   DEFAULT 0.03,
  created_at          TIMESTAMP DEFAULT now()
);

-- ------------------------------------------------------------
-- sequence_steps
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sequence_steps (
  id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id     UUID    REFERENCES campaigns(id) ON DELETE CASCADE,
  step_number     INT     NOT NULL,
  subject         TEXT,
  body            TEXT,
  wait_days       INT     DEFAULT 3,
  is_reply_thread BOOLEAN DEFAULT false,
  ab_variant      TEXT,
  ab_weight       INT     DEFAULT 50,
  spintax_enabled BOOLEAN DEFAULT true
);

CREATE UNIQUE INDEX IF NOT EXISTS sequence_steps_unique
  ON sequence_steps (campaign_id, step_number, COALESCE(ab_variant, 'none'));

ALTER TABLE sequence_steps
  ADD COLUMN IF NOT EXISTS condition_type TEXT DEFAULT 'always',
  ADD COLUMN IF NOT EXISTS condition_step INT,
  ADD COLUMN IF NOT EXISTS condition_skip_to INT;

-- ------------------------------------------------------------
-- leads
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
  id            UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id     TEXT    NOT NULL,
  email         TEXT    NOT NULL,
  first_name    TEXT,
  last_name     TEXT,
  company       TEXT,
  domain        TEXT,
  job_title     TEXT,
  linkedin_url  TEXT,
  phone         TEXT,
  country       TEXT    DEFAULT 'NL',
  custom_fields JSONB   DEFAULT '{}',
  status        TEXT    DEFAULT 'new',
  verified      BOOLEAN DEFAULT NULL,
  enriched      BOOLEAN DEFAULT false,
  created_at    TIMESTAMP DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS leads_email_client_unique ON leads (email, client_id);

-- ------------------------------------------------------------
-- campaign_leads
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign_leads (
  id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id       UUID    REFERENCES campaigns(id) ON DELETE CASCADE,
  lead_id           UUID    REFERENCES leads(id) ON DELETE CASCADE,
  current_step      INT     DEFAULT 1,
  next_send_at      TIMESTAMP,
  status            TEXT    DEFAULT 'active',
  thread_message_id TEXT,

  UNIQUE (campaign_id, lead_id)
);

-- ------------------------------------------------------------
-- email_events
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_events (
  id               UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id      UUID    REFERENCES campaigns(id),
  lead_id          UUID    REFERENCES leads(id),
  sequence_step_id UUID    REFERENCES sequence_steps(id),
  inbox_id         UUID    REFERENCES inboxes(id),
  event_type       TEXT    NOT NULL,
  ab_variant       TEXT,
  message_id       TEXT,
  timestamp        TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_events_campaign_idx ON email_events (campaign_id, event_type);
CREATE INDEX IF NOT EXISTS email_events_inbox_idx    ON email_events (inbox_id, timestamp);

-- ------------------------------------------------------------
-- reply_inbox
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reply_inbox (
  id             UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id      TEXT,
  campaign_id    UUID    REFERENCES campaigns(id),
  lead_id        UUID    REFERENCES leads(id),
  inbox_id       UUID    REFERENCES inboxes(id),
  from_email     TEXT,
  subject        TEXT,
  body           TEXT,
  classification TEXT,
  is_read        BOOLEAN DEFAULT false,
  received_at    TIMESTAMP DEFAULT now()
);


-- ============================================================
-- 3. ANALYTICS CACHE
-- ============================================================

CREATE TABLE IF NOT EXISTS analytics_cache (
  id          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT    NOT NULL,
  entity_type TEXT    NOT NULL,
  entity_id   UUID    NOT NULL,
  date        DATE    NOT NULL,
  metrics     JSONB   NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMP DEFAULT now(),

  CONSTRAINT analytics_cache_unique UNIQUE (entity_id, entity_type, date)
);

CREATE INDEX IF NOT EXISTS analytics_cache_client_idx ON analytics_cache (client_id, entity_type, date DESC);


-- ============================================================
-- 4. PUBLIC API + WEBHOOK SYSTEM
-- ============================================================

-- ------------------------------------------------------------
-- api_keys
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
  id           UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id    TEXT      NOT NULL,
  key_hash     TEXT      NOT NULL UNIQUE,
  name         TEXT      NOT NULL,
  permissions  TEXT[]    NOT NULL DEFAULT '{}',
  last_used_at TIMESTAMP,
  created_at   TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS api_keys_client_id_idx ON api_keys (client_id);
CREATE INDEX IF NOT EXISTS api_keys_hash_idx      ON api_keys (key_hash);

-- ------------------------------------------------------------
-- webhooks
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhooks (
  id         UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id  TEXT      NOT NULL,
  url        TEXT      NOT NULL,
  events     TEXT[]    NOT NULL DEFAULT '{}',
  secret     TEXT      NOT NULL,
  active     BOOLEAN   DEFAULT true,
  created_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS webhooks_client_id_idx ON webhooks (client_id);

-- ------------------------------------------------------------
-- webhook_logs
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_logs (
  id              UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  webhook_id      UUID      REFERENCES webhooks(id) ON DELETE CASCADE,
  client_id       TEXT,
  event_type      TEXT      NOT NULL,
  payload         JSONB,
  response_status INT,
  response_body   TEXT,
  attempt_count   INT       DEFAULT 1,
  next_retry_at   TIMESTAMP,
  success         BOOLEAN   DEFAULT false,
  timestamp       TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS webhook_logs_webhook_id_idx    ON webhook_logs (webhook_id);
CREATE INDEX IF NOT EXISTS webhook_logs_client_id_idx     ON webhook_logs (client_id);
CREATE INDEX IF NOT EXISTS webhook_logs_next_retry_at_idx ON webhook_logs (next_retry_at) WHERE success = false;

-- ------------------------------------------------------------
-- webhook_events
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_events (
  id          UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT      NOT NULL,
  event_type  TEXT      NOT NULL,
  payload     JSONB     NOT NULL,
  dispatched  BOOLEAN   DEFAULT false,
  created_at  TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS webhook_events_pending_idx ON webhook_events (created_at) WHERE dispatched = false;
CREATE INDEX IF NOT EXISTS webhook_events_client_idx  ON webhook_events (client_id)  WHERE dispatched = false;


-- ============================================================
-- 5. ENRICHMENT SYSTEM
-- ============================================================

CREATE TABLE IF NOT EXISTS enrichment_queue (
  id            UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id       UUID      REFERENCES leads(id) ON DELETE CASCADE,
  client_id     TEXT      NOT NULL,
  status        TEXT      NOT NULL DEFAULT 'pending',
  priority      INT       NOT NULL DEFAULT 5,
  attempts      INT       NOT NULL DEFAULT 0,
  error_message TEXT,
  queued_at     TIMESTAMP DEFAULT now(),
  started_at    TIMESTAMP,
  completed_at  TIMESTAMP
);

CREATE INDEX IF NOT EXISTS enrichment_queue_pending_idx
  ON enrichment_queue (priority, queued_at)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS enrichment_queue_lead_id_idx    ON enrichment_queue (lead_id);
CREATE INDEX IF NOT EXISTS enrichment_queue_client_id_idx  ON enrichment_queue (client_id);

CREATE UNIQUE INDEX IF NOT EXISTS enrichment_queue_lead_active_idx
  ON enrichment_queue (lead_id)
  WHERE status IN ('pending', 'processing');


-- ============================================================
-- 6. INTELLIGENCE LAYER
-- ============================================================

-- ------------------------------------------------------------
-- warmup_network_accounts
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warmup_network_accounts (
  id                  UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id           TEXT,
  email               TEXT      NOT NULL UNIQUE,
  provider            TEXT      NOT NULL DEFAULT 'gmail',
  status              TEXT      NOT NULL DEFAULT 'active',
  last_login_check    TIMESTAMP,
  last_login_success  TIMESTAMP,
  failure_count       INT       NOT NULL DEFAULT 0,
  created_at          TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS warmup_network_accounts_status_idx ON warmup_network_accounts (status);
CREATE INDEX IF NOT EXISTS warmup_network_accounts_client_idx ON warmup_network_accounts (client_id);

-- ------------------------------------------------------------
-- network_health_log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS network_health_log (
  id              UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       TEXT,
  total_accounts  INT       NOT NULL,
  active_accounts INT       NOT NULL,
  health_score    FLOAT     NOT NULL,
  timestamp       TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS network_health_log_client_idx ON network_health_log (client_id);
CREATE INDEX IF NOT EXISTS network_health_log_ts_idx     ON network_health_log (timestamp);

-- ------------------------------------------------------------
-- diagnostics_log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diagnostics_log (
  id          UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT,
  check_type  TEXT      NOT NULL,
  entity_id   UUID,
  result      TEXT      NOT NULL,
  details     JSONB,
  timestamp   TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS diagnostics_log_client_idx     ON diagnostics_log (client_id);
CREATE INDEX IF NOT EXISTS diagnostics_log_check_type_idx ON diagnostics_log (check_type);
CREATE INDEX IF NOT EXISTS diagnostics_log_entity_idx     ON diagnostics_log (entity_id);
CREATE INDEX IF NOT EXISTS diagnostics_log_ts_idx         ON diagnostics_log (timestamp DESC);

-- ------------------------------------------------------------
-- sequence_suggestions
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sequence_suggestions (
  id                   UUID      PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id            TEXT      NOT NULL,
  campaign_id          UUID      REFERENCES campaigns(id)      ON DELETE CASCADE,
  sequence_step_id     UUID      REFERENCES sequence_steps(id) ON DELETE CASCADE,
  suggestion_type      TEXT      NOT NULL,
  current_performance  JSONB,
  suggestion_text      TEXT,
  claude_reasoning     TEXT,
  status               TEXT      NOT NULL DEFAULT 'pending',
  created_at           TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sequence_suggestions_campaign_idx ON sequence_suggestions (campaign_id);
CREATE INDEX IF NOT EXISTS sequence_suggestions_status_idx   ON sequence_suggestions (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS sequence_suggestions_client_idx   ON sequence_suggestions (client_id);


-- ============================================================
-- 7. DELIVERABILITY TOOLS
-- ============================================================

-- ------------------------------------------------------------
-- placement_tests
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS placement_tests (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id    TEXT NOT NULL,
  inbox_id     UUID REFERENCES inboxes(id) ON DELETE CASCADE,
  subject      TEXT,
  body_preview TEXT,
  status       TEXT DEFAULT 'pending',
  created_at   TIMESTAMP DEFAULT now(),
  completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_placement_tests_client ON placement_tests(client_id);
CREATE INDEX IF NOT EXISTS idx_placement_tests_inbox  ON placement_tests(inbox_id);
CREATE INDEX IF NOT EXISTS idx_placement_tests_status ON placement_tests(status);

-- ------------------------------------------------------------
-- placement_test_results
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS placement_test_results (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  test_id       UUID REFERENCES placement_tests(id) ON DELETE CASCADE,
  seed_provider TEXT NOT NULL,
  seed_email    TEXT NOT NULL,
  placement     TEXT,
  checked_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_placement_results_test ON placement_test_results(test_id);

-- ------------------------------------------------------------
-- content_scores
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS content_scores (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id           TEXT NOT NULL,
  campaign_id         UUID,
  sequence_step_id    UUID,
  subject             TEXT,
  rule_based_score    FLOAT,
  rule_based_flags    JSONB,
  claude_score        FLOAT,
  claude_flags        JSONB,
  claude_suggestions  JSONB,
  overall_score       FLOAT,
  created_at          TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_content_scores_client   ON content_scores(client_id);
CREATE INDEX IF NOT EXISTS idx_content_scores_campaign ON content_scores(campaign_id);

-- ------------------------------------------------------------
-- dns_check_log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dns_check_log (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id      UUID REFERENCES domains(id) ON DELETE CASCADE,
  check_type     TEXT NOT NULL,
  result         TEXT NOT NULL,
  expected_value TEXT,
  actual_value   TEXT,
  timestamp      TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dns_check_log_domain    ON dns_check_log(domain_id);
CREATE INDEX IF NOT EXISTS idx_dns_check_log_timestamp ON dns_check_log(timestamp);

-- ------------------------------------------------------------
-- blacklist_recoveries
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blacklist_recoveries (
  id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id                 UUID REFERENCES domains(id) ON DELETE CASCADE,
  blacklist_name            TEXT NOT NULL,
  delisting_url             TEXT,
  detected_at               TIMESTAMP DEFAULT now(),
  estimated_resolution_days INT,
  recovery_steps            JSONB,
  resolved                  BOOLEAN DEFAULT false,
  resolved_at               TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_blacklist_recoveries_domain   ON blacklist_recoveries(domain_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_recoveries_resolved ON blacklist_recoveries(resolved);


-- ============================================================
-- 8. PERSONAL WORKFLOW
-- ============================================================

-- ------------------------------------------------------------
-- decision_log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_log (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id            TEXT,
  decision_type        TEXT NOT NULL,
  entity_type          TEXT,
  entity_id            UUID,
  entity_name          TEXT,
  before_state         JSONB,
  after_state          JSONB,
  reason               TEXT,
  effect               JSONB,
  effect_calculated_at TIMESTAMP,
  made_by              TEXT,
  created_at           TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decision_log_client    ON decision_log(client_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_entity    ON decision_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_created   ON decision_log(created_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_no_effect ON decision_log(created_at) WHERE effect IS NULL;

-- ------------------------------------------------------------
-- experiments
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiments (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id           TEXT NOT NULL,
  name                TEXT NOT NULL,
  hypothesis          TEXT,
  metric              TEXT DEFAULT 'reply_rate',
  control_campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
  variant_campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
  min_sample_size     INT  DEFAULT 100,
  status              TEXT DEFAULT 'active',
  result              TEXT,
  result_summary      TEXT,
  started_at          TIMESTAMP DEFAULT now(),
  concluded_at        TIMESTAMP,
  learnings           TEXT
);

CREATE INDEX IF NOT EXISTS idx_experiments_client ON experiments(client_id);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);


-- ============================================================
-- ROW LEVEL SECURITY
-- All tables locked to the authenticated user.
-- Python backend uses service_role key (bypasses RLS).
-- Frontend uses anon key — RLS enforces isolation.
-- ============================================================

-- clients
ALTER TABLE clients ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "clients_isolation"               ON clients;
DROP POLICY IF EXISTS "Users can see their own client row" ON clients;
DROP POLICY IF EXISTS "Users can update their own client row" ON clients;
DROP POLICY IF EXISTS "Admins can see all client rows"  ON clients;

CREATE POLICY "clients_self"
  ON clients FOR SELECT
  USING (id = auth.uid());

CREATE POLICY "clients_self_update"
  ON clients FOR UPDATE
  USING (id = auth.uid());

CREATE POLICY "clients_admin_all"
  ON clients FOR ALL
  USING (
    id = auth.uid()
    OR EXISTS (
      SELECT 1 FROM clients c2
      WHERE c2.id = auth.uid() AND c2.is_admin = true
    )
  );

-- inboxes
ALTER TABLE inboxes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "inboxes_isolation"             ON inboxes;
DROP POLICY IF EXISTS "Admins can access all inboxes" ON inboxes;
CREATE POLICY "inboxes_isolation"
  ON inboxes FOR ALL
  USING (
    client_id = auth.uid()::text
    OR EXISTS (
      SELECT 1 FROM clients WHERE clients.id = auth.uid() AND clients.is_admin = true
    )
  );

-- domains
ALTER TABLE domains ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "domains_isolation"             ON domains;
DROP POLICY IF EXISTS "Admins can access all domains" ON domains;
CREATE POLICY "domains_isolation"
  ON domains FOR ALL
  USING (
    client_id = auth.uid()::text
    OR EXISTS (
      SELECT 1 FROM clients WHERE clients.id = auth.uid() AND clients.is_admin = true
    )
  );

-- warmup_logs
ALTER TABLE warmup_logs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "warmup_logs_isolation" ON warmup_logs;
CREATE POLICY "warmup_logs_isolation"
  ON warmup_logs FOR ALL
  USING (
    inbox_id IN (
      SELECT id FROM inboxes WHERE client_id = auth.uid()::text
    )
  );

-- sending_schedule
ALTER TABLE sending_schedule ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "sending_schedule_isolation" ON sending_schedule;
CREATE POLICY "sending_schedule_isolation"
  ON sending_schedule FOR ALL
  USING (client_id = auth.uid()::text);

-- bounce_log
ALTER TABLE bounce_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "bounce_log_isolation" ON bounce_log;
CREATE POLICY "bounce_log_isolation"
  ON bounce_log FOR ALL
  USING (
    inbox_id IN (
      SELECT id FROM inboxes WHERE client_id = auth.uid()::text
    )
  );

-- campaigns
ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "campaigns_isolation"             ON campaigns;
DROP POLICY IF EXISTS "Admins can access all campaigns" ON campaigns;
CREATE POLICY "campaigns_isolation"
  ON campaigns FOR ALL
  USING (
    client_id = auth.uid()::text
    OR EXISTS (
      SELECT 1 FROM clients WHERE clients.id = auth.uid() AND clients.is_admin = true
    )
  );

-- sequence_steps
ALTER TABLE sequence_steps ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "sequence_steps_isolation" ON sequence_steps;
CREATE POLICY "sequence_steps_isolation"
  ON sequence_steps FOR ALL
  USING (
    campaign_id IN (
      SELECT id FROM campaigns WHERE client_id = auth.uid()::text
    )
  );

-- leads
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "leads_isolation" ON leads;
CREATE POLICY "leads_isolation"
  ON leads FOR ALL
  USING (client_id = auth.uid()::text);

-- campaign_leads
ALTER TABLE campaign_leads ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "campaign_leads_isolation" ON campaign_leads;
CREATE POLICY "campaign_leads_isolation"
  ON campaign_leads FOR ALL
  USING (
    campaign_id IN (
      SELECT id FROM campaigns WHERE client_id = auth.uid()::text
    )
  );

-- email_events
ALTER TABLE email_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "email_events_isolation" ON email_events;
CREATE POLICY "email_events_isolation"
  ON email_events FOR ALL
  USING (
    campaign_id IN (
      SELECT id FROM campaigns WHERE client_id = auth.uid()::text
    )
  );

-- reply_inbox
ALTER TABLE reply_inbox ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "reply_inbox_isolation" ON reply_inbox;
CREATE POLICY "reply_inbox_isolation"
  ON reply_inbox FOR ALL
  USING (client_id = auth.uid()::text);

-- analytics_cache
ALTER TABLE analytics_cache ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "analytics_cache_isolation" ON analytics_cache;
CREATE POLICY "analytics_cache_isolation"
  ON analytics_cache FOR ALL
  USING (client_id = auth.uid()::text);

-- api_keys
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "api_keys_isolation" ON api_keys;
CREATE POLICY "api_keys_isolation"
  ON api_keys FOR ALL
  USING (client_id = auth.uid()::text);

-- webhooks
ALTER TABLE webhooks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "webhooks_isolation" ON webhooks;
CREATE POLICY "webhooks_isolation"
  ON webhooks FOR ALL
  USING (client_id = auth.uid()::text);

-- enrichment_queue
ALTER TABLE enrichment_queue ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "enrichment_queue_isolation" ON enrichment_queue;
CREATE POLICY "enrichment_queue_isolation"
  ON enrichment_queue FOR ALL
  USING (client_id = auth.uid()::text);

-- diagnostics_log (read-only for clients)
ALTER TABLE diagnostics_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "diagnostics_log_isolation" ON diagnostics_log;
CREATE POLICY "diagnostics_log_isolation"
  ON diagnostics_log FOR SELECT
  USING (client_id = auth.uid()::text);

-- sequence_suggestions
ALTER TABLE sequence_suggestions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "sequence_suggestions_isolation" ON sequence_suggestions;
CREATE POLICY "sequence_suggestions_isolation"
  ON sequence_suggestions FOR ALL
  USING (client_id = auth.uid()::text);

-- placement_tests
ALTER TABLE placement_tests ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "placement_tests_client_isolation" ON placement_tests;
CREATE POLICY "placement_tests_client_isolation"
  ON placement_tests FOR ALL
  USING (client_id = auth.uid()::text);

-- content_scores
ALTER TABLE content_scores ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "content_scores_client_isolation" ON content_scores;
CREATE POLICY "content_scores_client_isolation"
  ON content_scores FOR ALL
  USING (client_id = auth.uid()::text);

-- decision_log
ALTER TABLE decision_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "decision_log_client_isolation" ON decision_log;
CREATE POLICY "decision_log_client_isolation"
  ON decision_log FOR ALL
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- notifications
-- System and diagnostic notifications per client.
-- Used by diagnostics_engine.py and experiment-monitor workflow.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT NOT NULL,
  type        TEXT NOT NULL,     -- error | spam_rescue | low_reputation | high_bounce
                                 -- complaint | warning | info | experiment_ready
  entity_id   TEXT,              -- inbox_id, campaign_id, domain_id, experiment_id
  entity_type TEXT,              -- inbox | campaign | domain | experiment
  message     TEXT NOT NULL,
  priority    TEXT DEFAULT 'medium',  -- low | medium | high | urgent
  read        BOOLEAN DEFAULT false,
  timestamp   TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notifications_client   ON notifications(client_id, read, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_entity   ON notifications(entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_unread   ON notifications(client_id) WHERE read = false;

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "notifications_client_isolation" ON notifications;
CREATE POLICY "notifications_client_isolation"
  ON notifications FOR ALL
  USING (client_id = auth.uid()::text);

-- experiments
ALTER TABLE experiments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "experiments_client_isolation" ON experiments;
CREATE POLICY "experiments_client_isolation"
  ON experiments FOR ALL
  USING (client_id = auth.uid()::text);


-- ============================================================
-- 9. API COST TRACKING
-- ============================================================

-- ------------------------------------------------------------
-- api_cost_log — per-call Claude API cost tracking
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_cost_log (
  id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       TEXT,
  date            DATE        NOT NULL DEFAULT current_date,
  model           TEXT        NOT NULL,
  prompt_tokens   INT         NOT NULL DEFAULT 0,
  response_tokens INT         NOT NULL DEFAULT 0,
  cost_eur        NUMERIC(10,6) NOT NULL DEFAULT 0,
  context         TEXT,       -- e.g. 'warmup_content', 'reply_generation', 'content_scoring'
  inbox_id        UUID,
  created_at      TIMESTAMP   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_api_cost_log_client_date ON api_cost_log(client_id, date);
CREATE INDEX IF NOT EXISTS idx_api_cost_log_date        ON api_cost_log(date);

ALTER TABLE api_cost_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "api_cost_log_client_isolation" ON api_cost_log;
CREATE POLICY "api_cost_log_client_isolation"
  ON api_cost_log FOR ALL
  USING (client_id = auth.uid()::text);


-- ============================================================
-- 10. SUPPRESSION & UNSUBSCRIBE
-- ============================================================

CREATE TABLE IF NOT EXISTS suppression_list (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id   TEXT NOT NULL,
  email       TEXT NOT NULL,
  domain      TEXT,
  reason      TEXT DEFAULT 'unsubscribe',
  source      TEXT,
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


-- ============================================================
-- 11. EMAIL TRACKING (open + click)
-- ============================================================

CREATE TABLE IF NOT EXISTS email_tracking (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id       TEXT NOT NULL,
  campaign_id     TEXT,
  lead_id         TEXT,
  lead_email      TEXT,
  event_type      TEXT NOT NULL,
  tracking_token  TEXT NOT NULL,
  link_url        TEXT,
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


-- ============================================================
-- 12. CLIENT SETTINGS, CRM, SEQUENCE CONDITIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS client_settings (
  client_id        TEXT PRIMARY KEY,
  booking_url      TEXT,
  sender_name      TEXT,
  email_signature  TEXT,
  company_name     TEXT,
  reply_to_email   TEXT,
  unsubscribe_text TEXT DEFAULT 'Niet meer ontvangen? Uitschrijven',
  created_at       TIMESTAMP DEFAULT now(),
  updated_at       TIMESTAMP DEFAULT now()
);

ALTER TABLE client_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "client_settings_isolation" ON client_settings;
CREATE POLICY "client_settings_isolation"
  ON client_settings FOR ALL
  USING (client_id = auth.uid()::text);

CREATE TABLE IF NOT EXISTS crm_integrations (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id     TEXT NOT NULL,
  provider      TEXT NOT NULL,
  api_key       TEXT,
  webhook_url   TEXT,
  config        JSONB DEFAULT '{}',
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
  event_type    TEXT,
  status        TEXT,
  response      TEXT,
  created_at    TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crm_log_client ON crm_sync_log(client_id, created_at DESC);

ALTER TABLE crm_sync_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "crm_log_client_isolation" ON crm_sync_log;
CREATE POLICY "crm_log_client_isolation"
  ON crm_sync_log FOR ALL
  USING (client_id = auth.uid()::text);


-- ============================================================
-- 13. ADMIN AUDIT LOG
-- ============================================================

CREATE TABLE IF NOT EXISTS admin_audit_log (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  admin_id      TEXT NOT NULL,
  action        TEXT NOT NULL,
  target_type   TEXT,
  target_id     TEXT,
  payload       JSONB DEFAULT '{}',
  ip_address    TEXT,
  user_agent    TEXT,
  created_at    TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_log(admin_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON admin_audit_log(target_type, target_id);

ALTER TABLE admin_audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "audit_admin_read" ON admin_audit_log;
CREATE POLICY "audit_admin_read"
  ON admin_audit_log FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM clients
      WHERE clients.id = auth.uid() AND clients.is_admin = true
    )
  );


-- ============================================================
-- POST-SETUP: promote first admin
-- After your first signup, run this manually with your email:
--
--   UPDATE clients SET is_admin = true WHERE email = 'jouw@email.nl';
--
-- ============================================================
