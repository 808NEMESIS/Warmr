-- ============================================================
-- Warmr — Supabase Schema
-- Run this once against your Supabase project via the SQL Editor.
-- ============================================================

-- ------------------------------------------------------------
-- clients
-- One row per Warmr user account. Linked to Supabase Auth.
-- ------------------------------------------------------------
CREATE TABLE clients (
  id           UUID    PRIMARY KEY REFERENCES auth.users(id), -- Supabase Auth user UUID — used as client_id everywhere else
  company_name TEXT,                                           -- Display name of the client's company
  email        TEXT,                                           -- Primary contact email (mirrors auth.users.email)
  plan         TEXT    DEFAULT 'trial',                        -- Subscription tier: trial | starter | pro | agency
  max_inboxes  INT     DEFAULT 5,                              -- Maximum number of inboxes allowed under this plan
  max_domains  INT     DEFAULT 2,                              -- Maximum number of domains allowed under this plan
  created_at   TIMESTAMP DEFAULT now()                         -- Timestamp when this account was created
);

-- ------------------------------------------------------------
-- inboxes
-- Every sending inbox with its warmup state and reputation.
-- ------------------------------------------------------------
CREATE TABLE inboxes (
  id                   UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Stable UUID for this inbox
  email                TEXT    NOT NULL UNIQUE,                        -- Full sending address (e.g. outreach@yourdomain.nl)
  domain               TEXT    NOT NULL,                               -- Domain portion used to join with domains table
  provider             TEXT,                                           -- Email provider: google | microsoft | other
  warmup_active        BOOLEAN DEFAULT true,                           -- Controls whether warmup_engine processes this inbox
  warmup_start_date    DATE,                                           -- Date warmup began; used to calculate current warmup week
  daily_warmup_target  INT     DEFAULT 10,                             -- Target warmup sends per day; auto-updated by warmup_engine each run
  daily_campaign_target INT    DEFAULT 0,                              -- Allowed campaign sends per day once inbox is ready
  daily_sent           INT     DEFAULT 0,                              -- Running count of emails sent today; reset to 0 at midnight by daily_reset
  reputation_score     FLOAT   DEFAULT 50,                             -- Inbox health score 0–100; starts at 50 for new inboxes
  open_rate            FLOAT   DEFAULT 0,                              -- Rolling open rate as a decimal (0.0–1.0)
  reply_rate           FLOAT   DEFAULT 0,                              -- Rolling reply rate as a decimal (0.0–1.0)
  spam_rescues         INT     DEFAULT 0,                              -- Total emails rescued from spam/junk folder (lifetime)
  spam_complaints      INT     DEFAULT 0,                              -- Total spam complaints received (lifetime)
  last_spam_incident   TIMESTAMP,                                      -- Timestamp of the most recent spam complaint
  status               TEXT    DEFAULT 'warmup',                       -- Lifecycle state: warmup | ready | paused | retired
  client_id            TEXT,                                           -- Foreign key to clients.id (stored as text for RLS cast compatibility)
  notes                TEXT,                                           -- Free-text operational notes (e.g. "paused — MX issue")
  created_at           TIMESTAMP DEFAULT now(),                        -- When this inbox was registered
  updated_at           TIMESTAMP DEFAULT now()                         -- Last time any field on this row was changed
);

-- ------------------------------------------------------------
-- domains
-- DNS configuration and blacklist health per sending domain.
-- ------------------------------------------------------------
CREATE TABLE domains (
  id                   UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Stable UUID for this domain
  domain               TEXT    NOT NULL UNIQUE,                        -- The domain name (e.g. yourdomain.nl)
  registrar            TEXT,                                           -- Domain registrar name (TransIP, Namecheap, etc.)
  tld                  TEXT,                                           -- Top-level domain suffix: .nl | .be | .com | etc.
  spf_configured       BOOLEAN DEFAULT false,                          -- Whether a valid SPF TXT record exists
  dkim_configured      BOOLEAN DEFAULT false,                          -- Whether DKIM TXT record is published and verified
  dmarc_phase          TEXT    DEFAULT 'none',                         -- Current DMARC rollout phase: none | quarantine | enforce
  blacklisted          BOOLEAN DEFAULT false,                          -- Whether this domain appears on any known blacklist
  last_blacklist_check TIMESTAMP,                                      -- Timestamp of the most recent MXToolbox check
  client_id            TEXT,                                           -- Foreign key to clients.id
  created_at           TIMESTAMP DEFAULT now()                         -- When this domain was added
);

-- ------------------------------------------------------------
-- warmup_logs
-- Immutable audit trail of every warmup action and error.
-- ------------------------------------------------------------
CREATE TABLE warmup_logs (
  id                       UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Unique log entry ID
  inbox_id                 UUID    REFERENCES inboxes(id),                 -- Which client inbox this action belongs to
  action                   TEXT    NOT NULL,                               -- Event type: sent | received | spam_rescued | replied | opened | error
  counterpart_email        TEXT,                                           -- The other party in this exchange (warmup network address or real recipient)
  subject                  TEXT,                                           -- Subject line of the email involved
  warmup_week              INT,                                            -- Warmup week number at the time of this event (1, 2, 3 …)
  daily_volume             INT,                                            -- Total sends from this inbox on this day at the moment of logging
  reputation_score_at_time FLOAT,                                          -- Inbox reputation_score snapshot at the moment of this event
  landed_in_spam           BOOLEAN DEFAULT false,                          -- Whether this email was detected in the spam/junk folder
  was_rescued              BOOLEAN DEFAULT false,                          -- Whether this email was moved from spam to inbox
  was_replied              BOOLEAN DEFAULT false,                          -- Whether a reply was generated and sent for this email
  notes                    TEXT,                                           -- Extra context; for action='error' this holds the exception message
  timestamp                TIMESTAMP DEFAULT now()                         -- When this event occurred
);

-- ------------------------------------------------------------
-- sending_schedule
-- Campaign email queue — one row per scheduled outbound email.
-- ------------------------------------------------------------
CREATE TABLE sending_schedule (
  id                   UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Unique queue entry ID
  inbox_id             UUID    REFERENCES inboxes(id),                 -- Which client inbox will send this email
  campaign_id          UUID,                                           -- Groups emails belonging to the same campaign
  lead_email           TEXT,                                           -- Recipient's email address
  lead_name            TEXT,                                           -- Recipient's first or full name for personalisation
  company_name         TEXT,                                           -- Recipient's company name for personalisation
  personalized_opener  TEXT,                                           -- Custom first line written for this specific lead
  email_body           TEXT,                                           -- Full email body (HTML or plain text)
  subject              TEXT,                                           -- Email subject line
  sequence_step        INT     DEFAULT 1,                              -- Position in the email sequence (1 = initial, 2 = first follow-up, …)
  scheduled_at         TIMESTAMP,                                      -- When this email is due to be sent
  sent_at              TIMESTAMP,                                      -- Actual send timestamp; NULL means not yet sent
  status               TEXT    DEFAULT 'pending',                      -- Queue state: pending | sent | bounced | replied | unsubscribed
  client_id            TEXT                                            -- Foreign key to clients.id
);

-- ------------------------------------------------------------
-- bounce_log
-- Records every hard/soft bounce and spam complaint.
-- ------------------------------------------------------------
CREATE TABLE bounce_log (
  id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),  -- Unique bounce record ID
  inbox_id          UUID    REFERENCES inboxes(id),                 -- Which client inbox sent the email that bounced
  lead_email        TEXT,                                           -- The recipient address that triggered the bounce
  bounce_type       TEXT,                                           -- Classification: hard | soft | spam_complaint
  raw_response      TEXT,                                           -- Raw SMTP error text or complaint payload for debugging
  soft_bounce_count INT     DEFAULT 0,                              -- How many times this address has soft-bounced (retry gate: max 3)
  resolved          BOOLEAN DEFAULT false,                          -- Whether this bounce has been actioned (removed from queue, etc.)
  timestamp         TIMESTAMP DEFAULT now()                         -- When the bounce was detected
);

-- ============================================================
-- Row Level Security (RLS)
-- Every table is locked to the authenticated user.
-- Python backend scripts use the service_role key (bypasses RLS).
-- Frontend uses the anon key and relies entirely on these policies.
-- ============================================================

-- clients: each user can only see and modify their own record
ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
CREATE POLICY "clients_isolation"
  ON clients FOR ALL
  USING (id = auth.uid());

-- inboxes: restricted to the owning client
ALTER TABLE inboxes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "inboxes_isolation"
  ON inboxes FOR ALL
  USING (client_id = auth.uid()::text);

-- domains: restricted to the owning client
ALTER TABLE domains ENABLE ROW LEVEL SECURITY;
CREATE POLICY "domains_isolation"
  ON domains FOR ALL
  USING (client_id = auth.uid()::text);

-- warmup_logs: restricted to logs that belong to the client's own inboxes
ALTER TABLE warmup_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "warmup_logs_isolation"
  ON warmup_logs FOR ALL
  USING (
    inbox_id IN (
      SELECT id FROM inboxes WHERE client_id = auth.uid()::text
    )
  );

-- sending_schedule: restricted to the owning client
ALTER TABLE sending_schedule ENABLE ROW LEVEL SECURITY;
CREATE POLICY "sending_schedule_isolation"
  ON sending_schedule FOR ALL
  USING (client_id = auth.uid()::text);

-- bounce_log: restricted to bounces from the client's own inboxes
ALTER TABLE bounce_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "bounce_log_isolation"
  ON bounce_log FOR ALL
  USING (
    inbox_id IN (
      SELECT id FROM inboxes WHERE client_id = auth.uid()::text
    )
  );
