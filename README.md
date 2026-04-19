# Warmr

Self-hosted B2B email deliverability platform. Warms up inboxes, monitors reputation, runs outbound campaigns, and handles replies — without vendor lock-in.

Built as a replacement for Instantly, Smartlead, and Lemlist.

---

## What it does

- **Inbox warmup** — gradual volume ramp-up over 5 weeks, AI-generated natural emails, multi-turn conversations with a warmup network to simulate real engagement
- **Outbound campaigns** — multi-step sequences with A/B testing, spintax, conditional branching, inbox rotation per step
- **Deliverability** — SPF/DKIM/DMARC monitoring, blacklist checks, content scoring, placement testing
- **Funnel** — automated cold → warm → hot → meeting progression with reply-based routing
- **Engagement scoring** — per-lead score (+5 open, +10 click, +25 reply, −2/day decay)
- **Tracking** — HMAC-signed open pixels and click redirects, custom tracking domain support
- **GDPR** — unsubscribe system, suppression list, data export + right to erasure
- **CRM sync** — HubSpot, Pipedrive, generic webhooks

---

## Architecture

```
┌────────────┐     ┌──────────────┐     ┌────────────┐
│  Frontend  │────▶│  FastAPI     │────▶│  Supabase  │
│ (vanilla)  │     │  (70+ routes)│     │ (Postgres) │
└────────────┘     └──────────────┘     └────────────┘
                          │
                          ├─▶ SMTP (Google Workspace / M365)
                          ├─▶ IMAP (reply detection, spam rescue)
                          ├─▶ Claude API (content, classifier, optimizer)
                          └─▶ Webhooks (CRM integrations)
```

**Engines** (Python 3.11+, run via cron or n8n):
- `warmup_engine.py` — warmup sends
- `imap_processor.py` — inbox receiver + reply generator + spam rescue
- `campaign_scheduler.py` — campaign sender with funnel + rotation
- `diagnostics_engine.py` — reputation monitoring + auto-pause
- `funnel_engine.py` — stage transitions + reply routing
- `engagement_scorer.py` — lead engagement scoring with decay
- `dns_monitor.py` — SPF/DKIM/DMARC drift detection
- `bounce_handler.py`, `ab_optimizer.py`, `sequence_analyzer.py`, `daily_briefing.py`, etc.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Database | Supabase (PostgreSQL) with Row Level Security |
| Auth | Supabase Auth (ES256 JWT) |
| AI | Anthropic Claude (Haiku for bulk, Sonnet for quality) |
| Frontend | Vanilla HTML/CSS/JS, Supabase JS SDK |
| Scheduling | cron or n8n workflows |
| Deployment | Any Linux VPS (tested on macOS for dev) |

---

## Quickstart

### Prerequisites

- Python 3.11+
- Supabase project
- Anthropic API key
- Google Workspace or Microsoft 365 inboxes with app passwords
- 10+ Gmail accounts with 2FA + app passwords (warmup network)

### Install

```bash
git clone https://github.com/808NEMESIS/Warmr.git
cd Warmr
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY, inbox credentials
```

### Database setup

Run `full_schema.sql` in the Supabase SQL Editor. This creates all tables with RLS policies.

### Verify connections

```bash
python test_connections.py
```

Checks SMTP + IMAP for all configured inboxes and warmup accounts.

### Validate startup

```bash
python -c "import asyncio; from utils.startup_validator import validate_startup; asyncio.run(validate_startup())"
```

Checks env vars, packages, Supabase tables, Anthropic key.

### Run the API

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/index.html` to log in.

### Schedule the engines

Use the provided cron installer:

```bash
bash crontab_warmr.sh
```

Installs 14 schedules covering warmup, IMAP processing, campaign sending, diagnostics, daily reset, and more.

---

## Documentation

- [CLAUDE.md](CLAUDE.md) — full architecture spec and design decisions
- Swagger UI at `/docs` — all 70+ API endpoints
- `/.well-known/security.txt` — responsible disclosure

---

## Key design choices

- **Self-hosted** — no third-party warmup networks, no vendor lock-in, GDPR-compliant by design
- **Gradual scaling** — week-by-week volume schedule, never exceed 80 sends/inbox/day
- **Multi-language** — content generation supports NL/EN/FR out of the box
- **Cost-aware** — daily Anthropic budget cap (`DAILY_API_BUDGET_EUR=2.00`), all Claude calls tracked
- **Safety-first** — HMAC-signed tokens, suppression checks, company-level dedup, auto-pause on reputation drop, rate-limited endpoints

---

## Integration with Heatr

Warmr pairs with [Heatr](https://github.com/808NEMESIS/Heatr) (lead discovery + enrichment). Heatr pushes enriched leads to Warmr via `/api/v1/leads`; Warmr pushes engagement events back via webhooks.

---

## License

Proprietary — developed by Aerys.
