# Fase 0.1 — Productie-schemastaat (zonder aannames)

**Datum:** 2026-07-10
**Doel:** vaststellen wat er *werkelijk* live is, vóór het 0.2-besluit (migratie draaien vs. C5/C6-code terugdraaien). Niets in dit document is een aanname; wat niet geverifieerd kon worden staat expliciet als **ONGEVERIFIEERD** met de exacte query om het te bewijzen.

---

## 1. Toegangsstatus — live productie is NIET bereikbaar vanaf hier

- Warmr's productie-project = **`zomdrygdcaenjnrrpcpw`** (bron: `.env` `SUPABASE_URL` én `frontend/config.js`; bevestigd door de localStorage-sleutel `sb-zomdrygdcaenjnrrpcpw-auth-token`).
- De gekoppelde Supabase-MCP-account bevat **4 andere projecten** (org `jlesfctpmqnzsodexmhk`): `glsoqwmxylafigrcuvhw` (PersonalOS, eu-west-1, INACTIVE), `tlqjbkwmmluvfoywltqo` (Aerys, eu-central-1, INACTIVE), `lggxxsvuidierknlskjy` (Studio Lumen, eu-central-1, ACTIVE), `tossjfxdvvrkceblbqqw` (AerysOS, eu-central-1, ACTIVE). **`zomdrygdcaenjnrrpcpw` zit er niet bij.**
- `list_migrations(zomdrygdcaenjnrrpcpw)` → **`permission denied`** (geverifieerd, 2026-07-10).
- **Gevolg:** ik kan de live schemastaat niet uitlezen en zal **geen** ander project als proxy gebruiken (dat zou precies de "schema ≠ productie"-fout uit de audit zijn). §3 bevat de exacte queries; §5 hoe je mij 0.1 alsnog laat afmaken.

---

## 2. CONFIRMED (statisch, zeker) — de code↔migratie-kloof

De toegepaste C5/C6-code hangt af van **8 database-objecten die in de un-run migratie `api/critical5_critical6_migration.sql` staan en in `full_schema.sql` volledig ontbreken** (0/8 aanwezig, geverifieerd). Dit is het risico-oppervlak: als de migratie in prod niet gedraaid is, doen deze runtime-paden hun **fallback/faal-gedrag**.

| Object (migratieregel) | Runtime-afhankelijkheid | Gedrag als object ONTBREEKT in prod |
|---|---|---|
| `increment_daily_sent(inbox_uuid)` (`:22`) | `warmup_engine.py:197` | RPC gooit → **non-atomaire read-modify-write** fallback (`:201`) → lost updates → **dagcap te omzeilen** |
| `get_daily_api_spend(p_client)` (`:135`) | `utils/cost_tracker.py:87` | RPC gooit → **full-scan van `api_cost_log`** fallback (`:93-105`) → O(N²) budget-check (niet gefixt in prod) |
| `try_acquire_job_lock(...)` (`:85`) | `utils/job_lock.py:89` | RPC gooit → lock **faalt OPEN** → **geen cross-process-lock** → dubbele scheduler-runs mogelijk |
| `release_job_lock(...)` (`:114`) | `utils/job_lock.py:108` | n.v.t. (lock nooit verkregen) |
| `job_locks` tabel (`:78`) | via bovenstaande RPC's | idem — plus: als de tabel er wél is maar zonder RLS, is het een DoS-oppervlak |
| `inboxes.last_reset_date` (`:43`) | `daily_reset.py:63-72` | UPDATE gooit "column does not exist" → geslikt → **`daily_reset` reset niets** (regressie t.o.v. de oude werkende reset) |
| `inboxes.warmup_mode` (`:158`) | `warmup_engine.py:600-606` | `inbox.get("warmup_mode")` → altijd `None` → default `'full'` → **maintenance-mode nooit actief** → ready-inboxen blijven vol-volume warmen (kosten-regressie) |
| `email_events_sent_once_idx` (`:58`) | idempotency-backstop | ontbreekt → **geen unieke `sent`-constraint** → dubbele send-log mogelijk |

> Dezelfde logica geldt voor de **RLS-hardening** (`api/rls_hardening_migration.sql`, ook un-run): tot die draait zijn 8 tabellen (`webhook_logs, webhook_events, warmup_network_accounts, network_health_log, placement_test_results, dns_check_log, blacklist_recoveries, unsubscribe_tokens`) **publiek leesbaar/schrijfbaar via de anon-key** — mits Supabase's default-grants nog actief zijn (te verifiëren in §3).

**Dit is exact de "hybride toestand" uit de audit: code die de migratie veronderstelt, draait tegen een schema dat 'm (vermoedelijk) mist.**

---

## 3. ONGEVERIFIEERD — productie-grondwaarheid (draai dit tegen `zomdrygdcaenjnrrpcpw`)

Eén read-only query beantwoordt het volledige 0.2-besluit. Plak in de Supabase SQL-editor van het **Warmr**-project:

```sql
-- 3A. Bestaan de 8 C5/C6-objecten? (verwacht bij un-run migratie: allemaal 'MISSING')
SELECT 'increment_daily_sent'  AS obj, to_regprocedure('increment_daily_sent(uuid)')      IS NOT NULL AS present
UNION ALL SELECT 'get_daily_api_spend', to_regprocedure('get_daily_api_spend(text)')       IS NOT NULL
UNION ALL SELECT 'try_acquire_job_lock', to_regprocedure('try_acquire_job_lock(text,text,int)') IS NOT NULL
UNION ALL SELECT 'job_locks (table)',    to_regclass('public.job_locks')                   IS NOT NULL
UNION ALL SELECT 'inboxes.last_reset_date', EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name='inboxes' AND column_name='last_reset_date')
UNION ALL SELECT 'inboxes.warmup_mode',     EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name='inboxes' AND column_name='warmup_mode')
UNION ALL SELECT 'email_events_sent_once_idx', to_regclass('public.email_events_sent_once_idx') IS NOT NULL;

-- 3B. RLS-staat van de 8 verdachte tabellen (verwacht bij un-run hardening: rls_enabled=false)
SELECT c.relname AS tabel, c.relrowsecurity AS rls_enabled,
       (SELECT count(*) FROM pg_policies p WHERE p.tablename=c.relname) AS policies
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relname IN
  ('webhook_logs','webhook_events','warmup_network_accounts','network_health_log',
   'placement_test_results','dns_check_log','blacklist_recoveries','unsubscribe_tokens')
ORDER BY rls_enabled, tabel;

-- 3C. Zijn de anon/authenticated grants nog actief op die tabellen? (bepaalt of het lek écht open is)
SELECT table_name, grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema='public' AND grantee IN ('anon','authenticated')
  AND table_name IN ('unsubscribe_tokens','webhook_events','webhook_logs')
ORDER BY table_name, grantee;

-- 3D. Bewijs "schema ≠ productie": bestaan de drift-kolommen die runtime gebruikt maar geen SQL-bestand definieert?
SELECT 'leads.engagement_score' AS kolom, EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name='leads' AND column_name='engagement_score') AS present
UNION ALL SELECT 'clients.session_version', EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name='clients' AND column_name='session_version')
UNION ALL SELECT 'leads.funnel_stage', EXISTS (SELECT 1 FROM information_schema.columns
        WHERE table_name='leads' AND column_name='funnel_stage');
```

**Interpretatie:**
- **3A alles `false`** → migratie NIET gedraaid → de C5/C6-code regresseert nu → ga naar §4.
- **3A alles `true`** → migratie WEL gedraaid → C5/C6 is correct; verwijder dit als risico (maar controleer 3B/3C apart).
- **3B `rls_enabled=false` op ≥1 tabel + 3C toont grants** → het cross-tenant-lek is **live**; P0-C is dringend.
- **3D verwacht:** `engagement_score`/`session_version` = `true` in prod (anders werkt de app niet) terwijl ze in geen SQL-bestand staan → bewijst de drift; regenereer `full_schema.sql` uit `pg_dump`.

---

## 4. Het 0.2-besluit (afhankelijk van §3A)

**Aanbeveling: Optie A (migratie draaien) — mits §3A "MISSING" toont.** Reden: de C5/C6-migratie is **puur additief en idempotent** (`CREATE … IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), de argument-namen/kolommen matchen de al-getoetste code (`inbox_uuid`, `p_client`, `warmup_mode`, `last_reset_date` — geverifieerd), en 205/205 tests dekken het gedrag ná migratie. Optie A un-regresseert productie zonder werkende, geteste code weg te gooien; Optie B (rollback) verliest die code en laat de onderliggende bugs (uurlijkse reset, geen lock) terugkeren.

**Werkwijze (veilig):**
1. Draai §3A/3B/3C op prod → leg de output vast onder dit document.
2. Draai de migratie **eerst op een Supabase-branch** (`create_branch`), draai daar §3A opnieuw → alles `true`, en de integratietests.
3. Merge/apply op prod in een rustig venster; draai §3A nogmaals ter bevestiging.
4. **Pas hierna** is de C5/C6-code "correct"; het hoort als één ATOMIC RELEASE bij deze migratie (zie release-discipline).

**Als §3A onverwacht "true" toont** (migratie al gedraaid): dan is er géén hybride toestand voor C5/C6 en verschuift P0 naar RLS (§3B) + de niet-migratie-bugs (bounce cross-tenant, XSS, reaper).

---

## 5. Hoe 0.1 automatisch af te maken

Ik kan §3 zelf draaien en dit document met echte cijfers vullen zodra één van deze waar is:
- de Supabase-MCP-account krijgt toegang tot project `zomdrygdcaenjnrrpcpw` (uitnodigen in de org die aan deze MCP hangt, of de Warmr-org aan deze MCP koppelen); **of**
- je draait de queries in §3 en plakt de output hier — dan verwerk ik de interpretatie + het definitieve 0.2-advies.

*Tot dan blijft §3 ONGEVERIFIEERD en is §2 (statisch) het enige harde feit. Geen aannames over de live staat.*
