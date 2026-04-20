# CLAUDE.md — Warmr

## Project Overview

**Warmr** is a self-hosted B2B outbound email infrastructure platform, built as a SaaS alternative to tools like Instantly, Smartlead, and Lemlist. It is developed under Aerys and designed to be sold to other businesses as a standalone product. The system handles email warmup, deliverability monitoring, inbox reputation tracking, bounce handling, and reply classification — all automated via Python scripts and n8n workflows, with Supabase as the database and auth layer.

Warmr is built for the BENELUX market (Netherlands, Belgium, Luxembourg) but is architected to support any market or language. Each client logs in to their own isolated dashboard and manages their own inboxes and domains.

---

## Core Philosophy

- **Self-hosted over SaaS** — no vendor lock-in, full data ownership, GDPR compliant by design
- **White-label first** — no hardcoded company names, branding, or assumptions; everything is configurable via `.env` and database records
- **Gradual scaling** — the warmup engine follows a strict week-by-week volume schedule; never rush reputation building
- **Claude API for content** — all warmup emails and reply content is generated via Claude Haiku to ensure uniqueness and natural language variation
- **Supabase as single source of truth** — all inbox state, logs, reputation scores, and scheduling live in Supabase

---

## Tech Stack

| Layer | Tool |
|---|---|
| Automation | Python 3.11+ |
| Workflow orchestration | n8n (self-hosted on Railway or Hetzner VPS) |
| Database | Supabase (PostgreSQL) |
| Email sending | SMTP via `smtplib` |
| Email receiving | IMAP via `imaplib` |
| AI content generation | Anthropic Claude API (Haiku for speed/cost) |
| Inbox providers | Google Workspace, Microsoft 365 |
| Domain registrar | TransIP (.nl/.be), Namecheap (.com) |
| Monitoring | MXToolbox, Google Postmaster |
| Dashboard | HTML/CSS/JS frontend connected to Supabase REST API |

---

## File Structure

```
/
├── CLAUDE.md                  ← You are here
├── WARMR_AUDIT.md             ← Architecture audit report (April 2026)
├── README.md                  ← GitHub-facing project description
├── .env / .env.example        ← Credentials (never commit .env)
├── requirements.txt
├── full_schema.sql            ← Full Supabase schema (single-file)
├── supabase_schema.sql        ← Legacy schema (use full_schema.sql)
│
├── CORE ENGINES ───────────────────────────────────────────
├── warmup_engine.py           ← Sends warmup emails via SMTP; self-healing counters
├── imap_processor.py          ← Spam rescue + multi-turn warmup replies
├── campaign_scheduler.py      ← Campaign sender: rotation, spintax, A/B, tracking
├── bounce_handler.py          ← DSN + ARF classification (hard/soft/complaint)
├── reply_classifier.py        ← Claude Haiku reply categorisation
├── daily_reset.py             ← Midnight reset + engagement decay + nurture check
├── weekly_report.py           ← Monday HTML email via Resend
│
├── SUPPORT ENGINES ────────────────────────────────────────
├── inbox_rotator.py           ← Inbox selection (rep + load + provider)
├── ab_optimizer.py            ← Auto-promote A/B winners (z-test)
├── ab_test_engine.py          ← Deterministic variant selection per lead
├── spintax_engine.py          ← {opt1|opt2} + {{var}} rendering
├── send_time_optimizer.py     ← Best-time-to-send per campaign
├── sequence_analyzer.py       ← Weekly sequence performance review
├── placement_tester.py        ← Gmail/Outlook/Yahoo seed-account placement
├── content_scorer.py          ← Rule + AI content spam-score
├── dns_monitor.py             ← SPF/DKIM/DMARC drift + blacklist checks
├── diagnostics_engine.py      ← Reputation drift + SMTP-error auto-pause
├── daily_briefing.py          ← AI morning digest via Resend
├── funnel_engine.py           ← Stage transitions + reply routing
├── engagement_scorer.py       ← Per-lead score (+/- events + daily decay)
├── enrichment_engine.py       ← Email verify + Claude opener generation
├── enrichment_queue.py        ← Async enrichment worker
├── crm_dispatcher.py          ← HubSpot/Pipedrive/webhook sync
├── webhook_dispatcher.py      ← Outbound webhook delivery + HMAC + circuit breaker
├── test_connections.py        ← SMTP/IMAP connectivity smoke test
│
├── SCHEDULERS ─────────────────────────────────────────────
├── crontab_warmr.sh           ← Legacy cron installer (replaced by launchd on macOS)
├── install_launchd.sh         ← macOS launchd agent installer (recommended)
│
├── api/                       ← FastAPI middleware layer
│   ├── main.py                ← All 70+ endpoints
│   ├── auth.py                ← JWT validation (HS256 + ES256), suspension, impersonation
│   ├── public_api.py          ← External-facing API (for Heatr); API-key auth
│   ├── dns_check.py           ← Live DNS queries
│   ├── models.py              ← Pydantic models
│   └── *_migration.sql        ← Incremental schema migrations
│
├── frontend/
│   ├── index.html             ← Login / signup
│   ├── dashboard.html         ← Reputation + activity + forecast badges
│   ├── inboxes.html           ← Inbox management (add/pause/delete)
│   ├── domains.html           ← DNS status + recovery steps
│   ├── campaigns.html         ← Campaign builder (AI sequence writer, templates)
│   ├── campaign-performance.html ← Per-campaign SVG trend chart
│   ├── leads.html             ← Priority-sorted leads + bulk actions
│   ├── funnel.html            ← Kanban cold→warm→hot→meeting
│   ├── unified-inbox.html     ← Reply inbox + AI reply suggestions
│   ├── suppression.html       ← Do-not-contact list
│   ├── settings.html          ← Profile + CRM integrations + sync log
│   ├── decisions.html         ← Decision log viewer
│   ├── experiments.html       ← A/B experiment management
│   ├── admin.html             ← Admin client management (is_admin only)
│   ├── onboarding.html        ← Epic intro + 4-step wizard
│   ├── app.js                 ← Supabase auth + polling + keyboard shortcuts
│   ├── config.js              ← Runtime config (anon key, API base)
│   └── style.css              ← Design system + dark mode
│
├── utils/
│   ├── cost_tracker.py        ← Claude API budget enforcement
│   ├── startup_validator.py   ← Boot-time config validation
│   ├── password_policy.py     ← Signup password strength
│   ├── secrets_vault.py       ← Fernet encryption for SMTP passwords
│   ├── service_audit.py       ← Service-role query audit trail
│   ├── structured_logging.py  ← JSON logs + correlation IDs
│   └── metrics.py             ← Prometheus /metrics endpoint
│
├── tests/                     ← 80 unit + 2 live RLS integration tests
│   ├── test_spintax_engine.py       (20)
│   ├── test_funnel_engine.py        (11)
│   ├── test_engagement_scorer.py    (11)
│   ├── test_suppression.py          (8)
│   ├── test_heatr_integration.py    (9)
│   ├── test_bounce_handler.py       (21)
│   ├── test_rls_isolation.py        (2, live Supabase)
│   ├── run_all.py                   ← Runs all test modules
│   └── test_{smtp,imap,claude,supabase}_connection.py ← Smoke tests
│
└── n8n/                       ← Alternative scheduler (launchd preferred on macOS)
    └── *.json                 ← 14 workflow definitions
```

---

## Environment Variables

All configuration lives in `.env`. Never hardcode credentials. The system supports multiple inboxes dynamically loaded from numbered env vars.

```env
# ── INBOXES ──────────────────────────────────────────
# Add as many as needed, increment the number
INBOX_1_EMAIL=placeholder@yourdomain.nl
INBOX_1_PASSWORD=placeholder_app_password
INBOX_1_PROVIDER=google                    # google | microsoft | other
INBOX_1_DOMAIN=yourdomain.nl

INBOX_2_EMAIL=placeholder2@yourdomain.nl
INBOX_2_PASSWORD=placeholder_app_password
INBOX_2_PROVIDER=google
INBOX_2_DOMAIN=yourdomain.nl

# ── WARMUP NETWORK ────────────────────────────────────
# Gmail accounts used as warmup network (not sending domains)
WARMUP_NETWORK_1_EMAIL=warmupaccount1@gmail.com
WARMUP_NETWORK_1_PASSWORD=placeholder_app_password
# ... up to 20-30 accounts

# ── SUPABASE ──────────────────────────────────────────
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_service_role_key

# ── ANTHROPIC ─────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...

# ── SETTINGS ──────────────────────────────────────────
WARMUP_LANGUAGE=nl                         # nl | en | fr
TARGET_MARKET=BENELUX                      # For logging/reporting context
REPLY_RATE=0.35                            # 35% of warmup emails get a reply
MAX_DAILY_WARMUP=80                        # Hard cap per inbox per day
SEND_WINDOW_START=07:00
SEND_WINDOW_END=19:00
SEND_DAYS=1,2,3,4,5                        # Mon-Fri only (1=Monday)
```

---

## Supabase Schema

### `inboxes` table
Tracks every sending inbox, its warmup state, and reputation.

```sql
CREATE TABLE inboxes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT NOT NULL UNIQUE,
  domain TEXT NOT NULL,
  provider TEXT,                            -- google | microsoft | other
  warmup_active BOOLEAN DEFAULT true,
  warmup_start_date DATE,
  daily_warmup_target INT DEFAULT 10,       -- increases per week
  daily_campaign_target INT DEFAULT 0,
  daily_sent INT DEFAULT 0,
  reputation_score FLOAT DEFAULT 50,        -- 0-100, starts at 50 for new inbox
  open_rate FLOAT DEFAULT 0,
  reply_rate FLOAT DEFAULT 0,
  spam_rescues INT DEFAULT 0,
  spam_complaints INT DEFAULT 0,
  last_spam_incident TIMESTAMP,
  status TEXT DEFAULT 'warmup',             -- warmup | ready | paused | retired
  client_id TEXT,                           -- for white-label multi-client use
  notes TEXT,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
);
```

### `warmup_logs` table
Full audit trail of every warmup send, receive, and engagement action.

```sql
CREATE TABLE warmup_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inbox_id UUID REFERENCES inboxes(id),
  action TEXT NOT NULL,                     -- sent | received | spam_rescued | replied | opened
  counterpart_email TEXT,                   -- who was the other inbox
  subject TEXT,
  warmup_week INT,                          -- week number of warmup
  daily_volume INT,                         -- total sent that day at time of log
  reputation_score_at_time FLOAT,
  landed_in_spam BOOLEAN DEFAULT false,
  was_rescued BOOLEAN DEFAULT false,
  was_replied BOOLEAN DEFAULT false,
  timestamp TIMESTAMP DEFAULT now()
);
```

### `domains` table
Tracks DNS configuration and health per sending domain.

```sql
CREATE TABLE domains (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain TEXT NOT NULL UNIQUE,
  registrar TEXT,
  tld TEXT,
  spf_configured BOOLEAN DEFAULT false,
  dkim_configured BOOLEAN DEFAULT false,
  dmarc_phase TEXT DEFAULT 'none',          -- none | quarantine | enforce
  blacklisted BOOLEAN DEFAULT false,
  last_blacklist_check TIMESTAMP,
  client_id TEXT,
  created_at TIMESTAMP DEFAULT now()
);
```

### `sending_schedule` table
Campaign email queue — used by the campaign scheduler workflow.

```sql
CREATE TABLE sending_schedule (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inbox_id UUID REFERENCES inboxes(id),
  campaign_id UUID,
  lead_email TEXT,
  lead_name TEXT,
  company_name TEXT,
  personalized_opener TEXT,
  email_body TEXT,
  subject TEXT,
  sequence_step INT DEFAULT 1,
  scheduled_at TIMESTAMP,
  sent_at TIMESTAMP,
  status TEXT DEFAULT 'pending',            -- pending | sent | bounced | replied | unsubscribed
  client_id TEXT
);
```

### `bounce_log` table

```sql
CREATE TABLE bounce_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inbox_id UUID REFERENCES inboxes(id),
  lead_email TEXT,
  bounce_type TEXT,                         -- hard | soft | spam_complaint
  raw_response TEXT,
  soft_bounce_count INT DEFAULT 0,
  resolved BOOLEAN DEFAULT false,
  timestamp TIMESTAMP DEFAULT now()
);
```

---

## Warmup Schedule (Per Inbox)

Follow this strictly. Never skip weeks. Never rush.

| Week | Daily Volume | Strategy |
|---|---|---|
| Week 1 | 5–10 emails/day | Warmup only, no campaigns. Simulate high reply rate. |
| Week 2 | 15–25 emails/day | Mix warmup + 1–2 real prospects max. Monitor spam folder. |
| Week 3 | 30–40 emails/day | Gradually increase. Up to 10–15% campaign emails allowed. |
| Week 4 | 40–50 emails/day | Inbox ready for limited campaigns. Keep warmup running. |
| Week 5+ | 50–80 emails/day | Fully operational. Never exceed 80–100/day per inbox. |

The `daily_warmup_target` in the `inboxes` table should be updated automatically by the warmup engine based on `warmup_start_date`.

---

## Warmup Engine Logic (`warmup_engine.py`)

```
1. Load all active inboxes from Supabase where warmup_active = true AND daily_sent < daily_warmup_target
2. For each inbox:
   a. Calculate current week based on warmup_start_date
   b. Set daily target based on week schedule above
   c. Select random recipient from warmup network pool (never same as sender)
   d. Generate unique email content via Claude Haiku (Dutch or configured language)
   e. Send via SMTP
   f. Log to warmup_logs
   g. Update daily_sent counter in inboxes table
3. Randomize send times within SEND_WINDOW_START–SEND_WINDOW_END
4. Never send to same recipient twice in one day
```

### Claude Haiku prompt for warmup content
```
Generate a short professional business email in {WARMUP_LANGUAGE} (80–120 words).
From: {sender_name}, To: {recipient_name}.
Topic: one of [project update, meeting follow-up, quick question, feedback request, brief check-in, resource share].
Sound completely natural and human. No marketing language. No template-like phrases.
Vary sentence length. Use a natural greeting and sign-off.
Return only the email body, no subject line.
```

---

## IMAP Processor Logic (`imap_processor.py`)

```
Every 10 minutes:
1. Connect to all inboxes via IMAP
2. Check spam/junk folder:
   - Move ALL emails back to inbox
   - Mark as "Not Spam"
   - Mark as "Important"
   - Log spam_rescued action to warmup_logs
3. Check inbox for unread warmup emails:
   - Mark as read (simulates open)
   - 35% chance: generate reply via Claude Haiku and send
   - Log received + replied actions
4. Update reputation_score:
   - +0.5 per successful warmup exchange
   - +1.0 per spam rescue (proves inbox is legitimate)
   - -20 per spam complaint
   - Cap at 100, floor at 0
```

---

## Reputation Score Logic

The reputation score (0–100) is a composite metric, not just spam complaints:

| Event | Score Change |
|---|---|
| Warmup email sent successfully | +0.2 |
| Warmup reply received | +0.5 |
| Email rescued from spam | +1.0 |
| Email opened (header read) | +0.3 |
| Soft bounce | -2.0 |
| Hard bounce | -5.0 |
| Spam complaint | -20.0 |

Inbox is considered **ready for campaigns** when:
- `reputation_score >= 70`
- `warmup_start_date` is at least 28 days ago
- `spam_complaints = 0` in the last 14 days
- `reply_rate >= 25%`

---

## Reply Classifier (`reply_classifier.py`)

Classify every incoming reply from real prospects into one of:

| Category | Description |
|---|---|
| `interested` | Wants more info or a meeting |
| `not_interested` | Explicit lack of interest |
| `out_of_office` | Auto-reply or absence |
| `referral` | Refers to another person |
| `unsubscribe` | Wants no further contact |
| `question` | Has a question |
| `other` | Anything else |

On `unsubscribe`: immediately update `sending_schedule` status to `unsubscribed` for all pending emails to that domain. Log to bounce_log.

---

## Bounce Handling (`bounce_handler.py`)

```
Hard bounce → remove from list immediately, blacklist domain in domains table
Soft bounce → retry 3x with 24h interval, then remove
Spam complaint → stop all sends from that inbox for 24h, reduce reputation_score by 20, alert via log
```

Stop ALL sending immediately if bounce rate exceeds 3% on any single inbox.

---

## DNS Configuration (per new domain)

Always configure in this order before any warmup starts:

1. **SPF** — `v=spf1 include:_spf.google.com ~all` (use `~all` during setup, switch to `-all` after testing)
2. **DKIM** — enable in Google Workspace Admin, add TXT record to DNS
3. **DMARC phase 1** — `v=DMARC1; p=none; rua=mailto:dmarc@yourdomain.com`
4. **DMARC phase 2** (week 3–4) — `p=quarantine; pct=50`
5. **DMARC phase 3** (week 5+) — `p=reject`
6. **MX records** — Google Workspace MX records
7. Validate everything with MXToolbox before first send
8. Test score on mail-tester.com (minimum 8/10 required)

---

## n8n Workflows

### `warm-up-sender`
- Trigger: every 20 minutes, 07:00–19:00, weekdays only
- Queries Supabase for inboxes with remaining daily capacity
- Calls warmup_engine.py via Execute Command node or HTTP Request

### `warm-up-receiver`
- Trigger: every 10 minutes
- Calls imap_processor.py
- Logs results to Supabase

### `campaign-scheduler`
- Trigger: every 5 minutes
- Fetches pending rows from `sending_schedule` where `scheduled_at <= NOW()`
- Checks inbox daily capacity before sending
- Randomizes delay 2–8 minutes between sends
- Max 1 email per inbox per 3 minutes

### `bounce-processor`
- Trigger: every 30 minutes
- Calls bounce_handler.py

### `blacklist-monitor`
- Trigger: daily at 06:00
- Checks all domains against MXToolbox API
- Updates `blacklisted` field in domains table

### `daily-reset`
- Trigger: midnight every day
- Resets `daily_sent = 0` for all inboxes

### `weekly-report`
- Trigger: Monday 08:00
- Aggregates warmup_logs for the past 7 days
- Outputs summary: emails sent, avg reputation score, spam rescues, complaints

---

## White-Label Multi-Client Usage

Every table has a `client_id` TEXT field. When running this for multiple companies:

- Set `client_id` to a short identifier per client (e.g. `curio`, `clientabc`)
- All queries filter by `client_id`
- Each client gets their own set of domains and inboxes in the same Supabase project
- Dashboard filters by `client_id` via URL param or login context
- Credentials per client are stored as separate numbered env vars or in a separate `.env.{client_id}` file

---

## SaaS Auth (Supabase Auth)

Warmr uses Supabase Auth for user login and multi-tenancy. Each user account is a Warmr client. All data is isolated per client via `client_id` which maps to the Supabase `auth.users` UUID.

### Auth flow
1. User signs up via `frontend/index.html` using Supabase Auth (email + password)
2. On signup, a row is inserted into the `clients` table with their UUID as `id`
3. On login, Supabase returns a session token stored in `localStorage`
4. All frontend API calls pass the session token — Supabase Row Level Security (RLS) enforces data isolation automatically
5. All Python backend scripts use the `service_role` key (bypasses RLS) — never expose this key to the frontend

### `clients` table
```sql
CREATE TABLE clients (
  id UUID PRIMARY KEY REFERENCES auth.users(id),
  company_name TEXT,
  email TEXT,
  plan TEXT DEFAULT 'trial',               -- trial | starter | pro | agency
  max_inboxes INT DEFAULT 5,
  max_domains INT DEFAULT 2,
  created_at TIMESTAMP DEFAULT now()
);
```

### Row Level Security (RLS)
Enable RLS on all tables. Each table with a `client_id` column gets this policy:

```sql
-- Example for inboxes table
ALTER TABLE inboxes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can only see their own inboxes"
  ON inboxes FOR ALL
  USING (client_id = auth.uid()::text);
```

Apply the same pattern to: `domains`, `warmup_logs`, `sending_schedule`, `bounce_log`.

### Frontend auth pattern (`app.js`)
```javascript
const supabase = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// Login
async function login(email, password) {
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) throw error;
  window.location.href = '/dashboard.html';
}

// Protect pages — call at top of every page except index.html
async function requireAuth() {
  const { data: { session } } = await supabase.auth.getSession();
  if (!session) window.location.href = '/index.html';
  return session;
}

// Fetch client's own inboxes (RLS handles filtering automatically)
async function getInboxes() {
  const { data, error } = await supabase.from('inboxes').select('*');
  return data;
}
```

### Pages
- `index.html` — login + signup form, redirects to dashboard on success
- `dashboard.html` — warmup overview, stats, domain health (requires auth)
- `inboxes.html` — add/remove/pause inboxes, view warmup progress (requires auth)
- `domains.html` — DNS status per domain, DMARC phase tracker (requires auth)
- `campaigns.html` — view and schedule campaign sends (requires auth)

### Design
- Clean, minimal, premium aesthetic — light background, soft purple/lavender gradient accents
- Font: a distinctive display font for headings, clean sans-serif for UI
- Mobile responsive
- No frameworks — vanilla HTML/CSS/JS + Supabase JS SDK via CDN

---

## GDPR Compliance (BENELUX)

- Only process business email addresses (never personal Gmail/Hotmail addresses)
- Legal basis: legitimate interest for B2B cold outreach
- Always include opt-out option in campaign emails
- Honor unsubscribe requests immediately — update `sending_schedule` status within the same processing cycle
- Maintain a processing register documenting what data is stored and why
- Delete all data for opted-out contacts within 30 days
- Never store app passwords in plain text in the database — use environment variables only

---

## Coding Conventions

- Python 3.11+
- All scripts read config from `.env` via `python-dotenv`
- Use `supabase-py` for all database operations
- Use `anthropic` SDK for Claude API calls — always use `claude-haiku-4-5-20251001` unless speed is not a concern
- All SMTP connections via SSL (port 465)
- All IMAP connections via SSL (port 993)
- Every function must have a docstring
- Log all errors to `warmup_logs` with `action = 'error'` and include the exception message in `notes`
- Never raise unhandled exceptions — always catch, log, and continue
- Use type hints throughout

---

## What NOT to Build (MVP Scope)

- No payment processing (Stripe integration is post-MVP)
- No frontend email composer (campaign content is generated via Claude API)
- No integrations with Instantly/Lemlist/Smartlead (this replaces them)
- Do not use any third-party warmup networks or APIs — the warmup network is entirely self-contained
- No admin panel for Aerys to manage all clients (post-MVP)

---

## Current Status

- [ ] Supabase project created
- [ ] Schema migrated (all tables + RLS policies)
- [ ] `.env` populated with placeholder credentials
- [ ] `warmup_engine.py` built and tested locally
- [ ] `imap_processor.py` built and tested locally
- [ ] `bounce_handler.py` built
- [ ] `reply_classifier.py` built
- [ ] n8n workflows imported
- [ ] Frontend login/signup page built (`index.html`)
- [ ] Dashboard built and connected to Supabase Auth (`dashboard.html`)
- [ ] Inboxes, domains, campaigns pages built
- [ ] Real inbox credentials added
- [ ] Warmup started (Day 1)

---

*This file should be updated as the project evolves. Always keep the Current Status checklist up to date.*
