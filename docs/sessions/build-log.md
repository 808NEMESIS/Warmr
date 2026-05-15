# Warmr build log

Append-only log of operationally significant changes — security carve-outs,
schema migrations applied to production, deploy events. NOT a changelog;
git history covers that. This file is for the *why* of decisions whose
context lives outside the code.

---

## 2026-05-08 · Localhost exception in webhook URL validator

**Change:** `utils/url_safety.assert_url_safe` accepts `http://` for
hostnames `localhost` and `127.0.0.1` only, and skips the loopback/
private-IP guard for those two hosts.

**Why:** Local Heatr development. `http://localhost:8001/webhooks/warmr`
needs to round-trip through Warmr's webhook system without globally
flipping `WARMR_URL_ALLOW_HTTP=1` (which would weaken the scheme check
for all hosts).

**Scope of carve-out:**
- `http://localhost*` → allowed
- `http://127.0.0.1*` → allowed
- `https://localhost*` and `https://127.0.0.1*` → also allowed
  (otherwise blocked by the loopback IP check)
- `http://*` for every other host → still rejected
- `::1` (IPv6 loopback) → still rejected (not in exception list — spec
  was 127.0.0.1 only)
- Hostnames containing "localhost" as a substring (e.g.
  `localhost.attacker.com`) → still go through full SSRF policy

**Files touched:**
- `utils/url_safety.py` — added `_LOCALHOST_HOSTS` set + `_is_localhost()`
  helper; conditional scheme allowance + private-IP guard skip in
  `assert_url_safe()`.
- `tests/test_security_hardening.py` — replaced
  `test_localhost_literal_rejected` (which asserted the old
  always-reject behaviour) with `test_ipv6_loopback_still_rejected` and
  added 4 new carve-out tests.

**Production risk:** Negligible. The carve-out only kicks in when the
hostname is *literally* "localhost" or "127.0.0.1". An attacker creating
a webhook in production has no reason to use those values — webhooks
fire from Warmr's host, not the attacker's, so localhost would point at
Warmr itself. The existing log + circuit-breaker would catch any abuse
pattern. If the threat model later changes, gate the carve-out behind
`os.getenv("WARMR_LOCAL_DEV", "0") == "1"`.

**Validated:** all 192 tests pass after the change.

---

## 2026-05-08 · Schema-mismatch fix in leads inserts (Fix C — selective mapping)

**Probleem:** Heatr's `_build_lead_payload` stuurt velden volgens Warmr's
Pydantic `LeadIn`-model, maar Warmr's insert-paden faalden met PGRST204
(`Could not find the 'company_name' column of 'leads'`). Twee mismatches
bleken er te zijn:

- Pydantic-veld `company_name` ↔ DB-kolom `company`
- Pydantic/code-veld `imported_at` bestaat niet in DB (wel `created_at`
  met `DEFAULT now()`)

**Waarom Fix C i.p.v. Fix A (volledige rename in DB):**
- Free Plan Supabase, geen automatisch backup-vangnet — DB-mutaties
  hebben hoger risico op niet-recoverable fouten.
- Heatr's externe contract (`POST /leads` met `company_name` payload)
  blijft intact — geen breaking change voor de integratie.
- Spintax-engine + campaign_scheduler lezen `lead["company"]` direct uit
  DB en blijven ongewijzigd werken (DB heeft nog steeds de `company`-kolom).
- Solo-project — naming-debt tussen API-laag (`company_name`) en DB-laag
  (`company`) is acceptabel zolang er één persoon onderhoud doet.

**Wijzigingen (Fix C scope — selective insert-mapping):**

1. `api/public_api.py` `_insert_lead_single`: `"company_name" → "company"`,
   `imported_at` gedropt.
2. `api/public_api.py` `_insert_leads_bulk`: idem.
3. `api/main.py` CSV-import handler (regels ~2625-2636):
   `"company_name" → "company"`, `imported_at` gedropt. **NB:** `notes`
   en `campaign_id` blijven in deze dict en worden door PostgREST
   geweigerd — backlog (zie hieronder).
4. `api/main.py:681` replies-list nested select:
   `leads(first_name,last_name,company_name) → leads(first_name,last_name,company)`.

**Validated:** 193/193 tests pass. Heatr's contract werkt: `POST /leads`
met `{"company_name": "Acme"}` payload schrijft nu naar `leads.company`
zonder PGRST204.

**Wat backlog blijft:**

- **Fix A — volledige rename** in DB (`company → company_name`,
  `created_at → imported_at`). Lost de naming-debt op, maar vereist
  ALTER TABLE + meebewegen van spintax-engine `_BUILTIN_VARS["company"]`
  + campaign_scheduler reads + Heatr's payload-bouwer. Niet doen tot
  je tijd hebt voor één rename-rondje.
- **`LeadResponse` uitbreiden** (`api/models.py:177`) — declareert nog
  `company_name`, `notes`, `imported_at`, `campaign_id` die niet
  rechtstreeks op `leads` bestaan. Frontend krijgt `None`-waardes
  totdat dit klopt.
- **`notes`-veld werkend maken** — komt zowel in `LeadPatch` als
  `CSV-import` voor. Vereist `ALTER TABLE leads ADD COLUMN notes TEXT`.
  CSV-import faalt nu nog op deze kolom (en op `campaign_id`).
- **`PATCH /leads/{id}`** — gebruikt `LeadPatch` met `notes` —
  zelfde issue als hierboven.
- **`q.order("imported_at", desc=True)`** in `api/public_api.py:665`
  (`GET /api/v1/leads` lijst-endpoint). Niet aangepast in deze fix —
  out-of-spec — maar zal 500 gooien tot ofwel de kolom bestaat ofwel
  de order naar `created_at` verschuift.

**Niet gewijzigd (bewust):**
- Pydantic `LeadIn` / `LeadResponse` / `LeadPatch` — externe contract
  intact.
- Spintax-engine `_BUILTIN_VARS` — leest `lead["company"]` rechtstreeks.
- `campaign_scheduler` lead-rendering — idem.
- Frontend.
- DB-migraties.
- Geen uvicorn restart.

