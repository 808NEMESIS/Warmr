-- personal_workflow_migration.sql
-- Decision log + experiment tracker tables

-- ---------------------------------------------------------------------------
-- decision_log — every significant configuration change
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_log (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               TEXT,
    decision_type           TEXT NOT NULL,   -- campaign_activated | campaign_paused | inbox_paused |
                                             -- inbox_added | sequence_edited | ab_applied |
                                             -- send_time_changed | suggestion_applied | domain_phase_changed
    entity_type             TEXT,            -- campaign | inbox | sequence_step | domain
    entity_id               UUID,
    entity_name             TEXT,
    before_state            JSONB,
    after_state             JSONB,
    reason                  TEXT,
    effect                  JSONB,           -- {metric: {before, after, delta}}
    effect_calculated_at    TIMESTAMP,
    made_by                 TEXT,            -- client email or 'system'
    created_at              TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decision_log_client      ON decision_log(client_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_entity      ON decision_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_created     ON decision_log(created_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_no_effect   ON decision_log(created_at) WHERE effect IS NULL;

ALTER TABLE decision_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "decision_log_client_isolation"
    ON decision_log FOR ALL
    USING (client_id = auth.uid()::text);


-- ---------------------------------------------------------------------------
-- experiments — hypothesis-driven campaign experiments
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiments (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               TEXT NOT NULL,
    name                    TEXT NOT NULL,
    hypothesis              TEXT,            -- "I believe X will result in Y because Z"
    metric                  TEXT DEFAULT 'reply_rate',  -- reply_rate | open_rate | meeting_rate
    control_campaign_id     UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    variant_campaign_id     UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    min_sample_size         INT DEFAULT 100,
    status                  TEXT DEFAULT 'active',   -- active | concluded | inconclusive | cancelled
    result                  TEXT,                    -- control_wins | variant_wins | inconclusive
    result_summary          TEXT,                    -- Claude-generated plain Dutch conclusion
    started_at              TIMESTAMP DEFAULT now(),
    concluded_at            TIMESTAMP,
    learnings               TEXT
);

CREATE INDEX IF NOT EXISTS idx_experiments_client ON experiments(client_id);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);

ALTER TABLE experiments ENABLE ROW LEVEL SECURITY;
CREATE POLICY "experiments_client_isolation"
    ON experiments FOR ALL
    USING (client_id = auth.uid()::text);
