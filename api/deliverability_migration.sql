-- deliverability_migration.sql
-- Tables and columns for: placement tester, content scorer, DNS monitor, blacklist recovery

-- ---------------------------------------------------------------------------
-- placement_tests — one record per inbox placement test run
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS placement_tests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       TEXT NOT NULL,
    inbox_id        UUID REFERENCES inboxes(id) ON DELETE CASCADE,
    subject         TEXT,
    body_preview    TEXT,
    status          TEXT DEFAULT 'pending',  -- pending | running | completed | failed
    created_at      TIMESTAMP DEFAULT now(),
    completed_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_placement_tests_client ON placement_tests(client_id);
CREATE INDEX IF NOT EXISTS idx_placement_tests_inbox  ON placement_tests(inbox_id);
CREATE INDEX IF NOT EXISTS idx_placement_tests_status ON placement_tests(status);

ALTER TABLE placement_tests ENABLE ROW LEVEL SECURITY;
CREATE POLICY "placement_tests_client_isolation"
    ON placement_tests FOR ALL
    USING (client_id = auth.uid()::text);


-- ---------------------------------------------------------------------------
-- placement_test_results — one row per seed account per test
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS placement_test_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_id         UUID REFERENCES placement_tests(id) ON DELETE CASCADE,
    seed_provider   TEXT NOT NULL,      -- gmail | outlook | yahoo | icloud
    seed_email      TEXT NOT NULL,
    placement       TEXT,               -- primary | promotions | spam | missing
    checked_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_placement_results_test ON placement_test_results(test_id);


-- ---------------------------------------------------------------------------
-- content_scores — spam scoring results per email
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS content_scores (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           TEXT NOT NULL,
    campaign_id         UUID,
    sequence_step_id    UUID,
    subject             TEXT,
    rule_based_score    FLOAT,          -- 0–100 (higher = spammier)
    rule_based_flags    JSONB,          -- {flag_name: value/true}
    claude_score        FLOAT,          -- 0–100, set only if deep analysis run
    claude_flags        JSONB,          -- specific issues Claude identified
    claude_suggestions  JSONB,          -- rewrite suggestions
    overall_score       FLOAT,          -- weighted composite
    created_at          TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_content_scores_client   ON content_scores(client_id);
CREATE INDEX IF NOT EXISTS idx_content_scores_campaign ON content_scores(campaign_id);

ALTER TABLE content_scores ENABLE ROW LEVEL SECURITY;
CREATE POLICY "content_scores_client_isolation"
    ON content_scores FOR ALL
    USING (client_id = auth.uid()::text);


-- ---------------------------------------------------------------------------
-- dns_check_log — audit trail of every DNS drift check
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dns_check_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id       UUID REFERENCES domains(id) ON DELETE CASCADE,
    check_type      TEXT NOT NULL,      -- spf | dkim | dmarc | mx | blacklist
    result          TEXT NOT NULL,      -- ok | changed | missing | blacklisted
    expected_value  TEXT,
    actual_value    TEXT,
    timestamp       TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dns_check_log_domain    ON dns_check_log(domain_id);
CREATE INDEX IF NOT EXISTS idx_dns_check_log_timestamp ON dns_check_log(timestamp);


-- ---------------------------------------------------------------------------
-- blacklist_recoveries — recovery workflow per blacklist hit
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blacklist_recoveries (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id                   UUID REFERENCES domains(id) ON DELETE CASCADE,
    blacklist_name              TEXT NOT NULL,
    delisting_url               TEXT,
    detected_at                 TIMESTAMP DEFAULT now(),
    estimated_resolution_days   INT,
    recovery_steps              JSONB,      -- [{step, description, completed, completed_at}]
    resolved                    BOOLEAN DEFAULT false,
    resolved_at                 TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_blacklist_recoveries_domain    ON blacklist_recoveries(domain_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_recoveries_resolved  ON blacklist_recoveries(resolved);


-- ---------------------------------------------------------------------------
-- ALTER domains — add columns for DNS monitoring
-- ---------------------------------------------------------------------------
ALTER TABLE domains
    ADD COLUMN IF NOT EXISTS spf_expected       TEXT,
    ADD COLUMN IF NOT EXISTS dkim_selector      TEXT DEFAULT 'google',
    ADD COLUMN IF NOT EXISTS last_dns_check     TIMESTAMP,
    ADD COLUMN IF NOT EXISTS dns_check_status   TEXT DEFAULT 'unknown';   -- ok | warning | error | unknown
