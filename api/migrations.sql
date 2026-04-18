-- ============================================================
-- Warmr — Campaign Engine Migrations
-- Run AFTER supabase_schema.sql.
-- Supersedes the thin campaigns/leads tables from the previous version.
-- ============================================================

-- ------------------------------------------------------------
-- campaigns
-- One row per outbound email campaign.
-- Full config: schedule, send window, safety thresholds.
-- ------------------------------------------------------------
CREATE TABLE campaigns (
  id                 UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Stable campaign UUID
  client_id          TEXT,                                           -- Foreign key to clients.id
  name               TEXT    NOT NULL,                               -- Human-readable campaign name
  status             TEXT    DEFAULT 'draft',                        -- Lifecycle: draft | active | paused | completed
  language           TEXT    DEFAULT 'nl',                           -- Language for Claude-generated content: nl | en | fr
  daily_limit        INT     DEFAULT 50,                             -- Max emails sent per day across all inboxes for this campaign
  timezone           TEXT    DEFAULT 'Europe/Amsterdam',             -- IANA timezone for scheduling (send window and next_send_at)
  send_days          TEXT    DEFAULT '1,2,3,4,5',                    -- Comma-separated ISO weekdays to send: 1=Mon … 7=Sun
  send_window_start  TIME    DEFAULT '08:00',                        -- Earliest allowed local send time
  send_window_end    TIME    DEFAULT '17:00',                        -- Latest allowed local send time
  stop_on_reply      BOOLEAN DEFAULT true,                           -- Auto-complete lead on first reply
  stop_on_unsubscribe BOOLEAN DEFAULT true,                          -- Auto-complete lead on unsubscribe keyword
  bounce_threshold   FLOAT   DEFAULT 0.03,                           -- Auto-pause campaign if bounce_rate exceeds this (0.03 = 3%)
  created_at         TIMESTAMP DEFAULT now()
);

ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
CREATE POLICY "campaigns_isolation"
  ON campaigns FOR ALL
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- sequence_steps
-- Ordered email templates within a campaign.
-- A/B variants share the same step_number but differ in ab_variant.
-- ------------------------------------------------------------
CREATE TABLE sequence_steps (
  id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Unique step ID
  campaign_id       UUID    REFERENCES campaigns(id) ON DELETE CASCADE,  -- Parent campaign
  step_number       INT     NOT NULL,                               -- Order within campaign (1 = first email)
  subject           TEXT,                                           -- Email subject (supports spintax and {{variables}})
  body              TEXT,                                           -- Email body (supports spintax, {{variables}}, HTML or plain)
  wait_days         INT     DEFAULT 3,                              -- Days to wait after the previous step before sending this one
  is_reply_thread   BOOLEAN DEFAULT false,                          -- If true, send as a reply on the same email thread (In-Reply-To)
  ab_variant        TEXT,                                           -- A/B test variant label: 'A' | 'B' | null (null = no test)
  ab_weight         INT     DEFAULT 50,                             -- Percentage probability of selecting this variant (0–100)
  spintax_enabled   BOOLEAN DEFAULT true                            -- Whether to process {option1|option2} spintax in this step
);

-- Unique constraint: (campaign_id, step_number, ab_variant)
-- Prevents two identical variants for the same step
CREATE UNIQUE INDEX sequence_steps_unique
  ON sequence_steps (campaign_id, step_number, COALESCE(ab_variant, 'none'));

ALTER TABLE sequence_steps ENABLE ROW LEVEL SECURITY;
CREATE POLICY "sequence_steps_isolation"
  ON sequence_steps FOR ALL
  USING (
    campaign_id IN (
      SELECT id FROM campaigns WHERE client_id = auth.uid()::text
    )
  );

-- ------------------------------------------------------------
-- leads
-- Prospect contacts. email is unique per client_id.
-- custom_fields is a free-form JSONB dict for {{custom:key}} substitution.
-- ------------------------------------------------------------
CREATE TABLE leads (
  id            UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Stable lead UUID
  client_id     TEXT    NOT NULL,                               -- Foreign key to clients.id
  email         TEXT    NOT NULL,                               -- Prospect's email address
  first_name    TEXT,                                           -- First name for {{first_name}} substitution
  last_name     TEXT,                                           -- Last name for {{last_name}} substitution
  company       TEXT,                                           -- Company name for {{company}} substitution
  domain        TEXT,                                           -- Domain derived from email (e.g. prospect.nl)
  job_title     TEXT,                                           -- Job title for {{job_title}} substitution
  linkedin_url  TEXT,                                           -- LinkedIn profile URL
  phone         TEXT,                                           -- Phone number
  country       TEXT    DEFAULT 'NL',                           -- ISO 3166-1 alpha-2 country code
  custom_fields JSONB,                                          -- Arbitrary key/value pairs: {"revenue": "10M", "pain": "deliverability"}
  status        TEXT    DEFAULT 'new',                          -- Lifecycle: new|contacted|replied|interested|not_interested|unsubscribed|bounced
  verified      BOOLEAN DEFAULT false,                          -- Whether email address has been verified (MX check or bounce-free history)
  enriched      BOOLEAN DEFAULT false,                          -- Whether lead was enriched via external data source
  created_at    TIMESTAMP DEFAULT now()
);

CREATE UNIQUE INDEX leads_email_client_unique ON leads (email, client_id);

ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
CREATE POLICY "leads_isolation"
  ON leads FOR ALL
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- campaign_leads
-- Many-to-many join between campaigns and leads.
-- Tracks where each lead is in the email sequence.
-- ------------------------------------------------------------
CREATE TABLE campaign_leads (
  id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Unique join row ID
  campaign_id       UUID    REFERENCES campaigns(id) ON DELETE CASCADE,  -- Parent campaign
  lead_id           UUID    REFERENCES leads(id) ON DELETE CASCADE,      -- The lead
  current_step      INT     DEFAULT 1,                              -- Which sequence step to send next
  next_send_at      TIMESTAMP,                                      -- When to send the next step (UTC, within send window)
  status            TEXT    DEFAULT 'active',                       -- active | paused | completed | unsubscribed | bounced
  thread_message_id TEXT,                                           -- RFC 2822 Message-ID of step 1 for reply-thread chaining

  UNIQUE (campaign_id, lead_id)                                     -- Lead can only appear once per campaign
);

ALTER TABLE campaign_leads ENABLE ROW LEVEL SECURITY;
CREATE POLICY "campaign_leads_isolation"
  ON campaign_leads FOR ALL
  USING (
    campaign_id IN (
      SELECT id FROM campaigns WHERE client_id = auth.uid()::text
    )
  );

-- ------------------------------------------------------------
-- email_events
-- Immutable event log: one row per email send/open/click/reply/bounce.
-- Used for campaign analytics and A/B test evaluation.
-- ------------------------------------------------------------
CREATE TABLE email_events (
  id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Unique event ID
  campaign_id       UUID    REFERENCES campaigns(id),               -- Which campaign generated this event
  lead_id           UUID    REFERENCES leads(id),                   -- Which lead the event is for
  sequence_step_id  UUID    REFERENCES sequence_steps(id),          -- Which step was active
  inbox_id          UUID    REFERENCES inboxes(id),                 -- Which sending inbox was used
  event_type        TEXT    NOT NULL,                               -- sent | opened | clicked | replied | bounced | unsubscribed
  ab_variant        TEXT,                                           -- Which A/B variant was used ('A' | 'B' | null)
  message_id        TEXT,                                           -- RFC 2822 Message-ID for de-duplication
  timestamp         TIMESTAMP DEFAULT now()                         -- When this event occurred (UTC)
);

-- Index for fast campaign-level analytics
CREATE INDEX email_events_campaign_idx ON email_events (campaign_id, event_type);
CREATE INDEX email_events_inbox_idx    ON email_events (inbox_id, timestamp);

ALTER TABLE email_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "email_events_isolation"
  ON email_events FOR ALL
  USING (
    campaign_id IN (
      SELECT id FROM campaigns WHERE client_id = auth.uid()::text
    )
  );

-- ------------------------------------------------------------
-- reply_inbox
-- Unified inbox: every real reply from a prospect, with classification.
-- The frontend reads this table to show the "Replies" view.
-- ------------------------------------------------------------
CREATE TABLE reply_inbox (
  id             UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Unique reply ID
  client_id      TEXT,                                           -- Foreign key to clients.id (for direct RLS)
  campaign_id    UUID    REFERENCES campaigns(id),               -- Which campaign the reply came from
  lead_id        UUID    REFERENCES leads(id),                   -- Which lead replied
  inbox_id       UUID    REFERENCES inboxes(id),                 -- Which client inbox received the reply
  from_email     TEXT,                                           -- Sender address of the reply
  subject        TEXT,                                           -- Reply subject line
  body           TEXT,                                           -- Full reply body text
  classification TEXT,                                           -- Claude classification: interested|not_interested|out_of_office|referral|unsubscribe|question|other
  is_read        BOOLEAN DEFAULT false,                          -- Whether the client has viewed this reply in the dashboard
  received_at    TIMESTAMP DEFAULT now()                         -- When the reply was received (UTC)
);

ALTER TABLE reply_inbox ENABLE ROW LEVEL SECURITY;
CREATE POLICY "reply_inbox_isolation"
  ON reply_inbox FOR ALL
  USING (client_id = auth.uid()::text);
