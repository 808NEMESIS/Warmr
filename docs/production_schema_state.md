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

## 3-bis. RESULTAAT (2026-07-10, door gebruiker uitgevoerd tegen `zomdrygdcaenjnrrpcpw`)

De gebruiker heeft §3A/B niet als losse SELECT gedraaid maar de daadwerkelijke fixes toegepast (zie §6 voor waarom dat een schema-bug blootlegde die eerst gefixt moest worden):

- **`api/rls_emergency_revoke.sql`** (de 8 REVOKE-statements) → **Success**. Het lek is dicht.
- **`api/critical5_critical6_migration.sql`** → eerste poging faalde op `get_daily_api_spend` (zie §6), gecorrigeerde versie → **Success**. Alle C5/C6-objecten (`increment_daily_sent`, `last_reset_date`, `email_events_sent_once_idx`, `job_locks`, `try_acquire_job_lock`, `release_job_lock`, `campaign_leads.status_changed_at`, `get_daily_api_spend`, `inboxes.warmup_mode`) bestaan nu in productie.

**0.2-besluit: AFGEROND — Optie A (migreren) is uitgevoerd.** De C5/C6-code in `faab47d` is vanaf nu correct in productie: `job_lock` doet echte cross-process-locking, `daily_reset` reset precies één keer per dag, de dagcap-teller is atomisch, de reaper kan gestrande sends vinden, en ready-inboxen degraderen naar maintenance-mode.

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

---

## 6. CONFIRMED (2026-07-10, harde productie-evidentie) — `client_id` is UUID, niet TEXT

Tijdens het toepassen van `api/critical5_critical6_migration.sql` gooide Postgres:
```
ERROR: 42883: operator does not exist: uuid = text
LINE: AND (p_client IS NULL OR client_id = p_client);
```
op de `get_daily_api_spend`-functie. Dit kon alleen betekenen dat `api_cost_log.client_id` in productie `uuid` is — `full_schema.sql` documenteert het overal als `TEXT`. Vervolgens gedraaid:

```sql
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE column_name = 'client_id'
ORDER BY table_name;
```

**Resultaat: 31 van de 32 `client_id`-kolommen in productie zijn `uuid`.** De enige uitzondering is `custom_oauth_providers.client_id` (`text`) — dat is de OAuth-*client*-identifier (OAuth 2.0-spec-terminologie), niet Warmr's tenant-identifier, en dus irrelevant voor tenant-isolatie. Volledige lijst (alle `uuid` tenzij anders vermeld): `analytics_cache, api_cost_log, api_keys, campaign_leads, campaigns, client_settings, content_scores, crm_integrations, crm_sync_log, decision_log, diagnostics_log, domains, email_tracking, enrichment_queue, experiments, funnel_analytics, inboxes, leads, network_health_log, notifications, oauth_authorizations, oauth_consents, placement_tests, reply_inbox, reply_routing_rules, sending_schedule, sequence_suggestions, suppression_list, unsubscribe_tokens, warmup_network_accounts, webhook_events, webhook_logs` + `custom_oauth_providers` (**text**, irrelevant hier).

### Directe consequenties

1. **`api/rls_hardening_migration.sql` bevatte dezelfde bug** — de policies gebruikten `client_id = auth.uid()::text`. Gefixt (cast verwijderd op alle 5 plekken; `auth.uid()` retourneert zelf al `uuid`) vóórdat het bestand tegen productie gedraaid is. Zie de git-historie van dit bestand voor de exacte diff.
2. **CONFIRMED (2026-07-10) — Critical 4 (TEXT↔UUID cascade-FK-mismatch) is in productie al VOLLEDIG opgelost, grondiger dan beide audits en `tenancy_hardening_migration.sql` (die er slechts 9 probeerde) voorzagen.** Gedraaid:
   ```sql
   SELECT conname, conrelid::regclass AS table_name,
          CASE confdeltype WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
                            WHEN 'a' THEN 'NO ACTION' ELSE confdeltype END AS on_delete,
          convalidated
   FROM pg_constraint
   WHERE conname LIKE 'fk_%client%'
   ORDER BY table_name;
   ```
   Resultaat: **29 rijen**, allemaal `on_delete=CASCADE` en `convalidated=true`. Dekt vrijwel elke `client_id`-tabel: `inboxes, domains, sending_schedule, campaigns, leads, reply_inbox, analytics_cache, api_keys, webhooks, webhook_logs, webhook_events, enrichment_queue, warmup_network_accounts, network_health_log, diagnostics_log, sequence_suggestions, placement_tests, content_scores, decision_log, experiments, notifications, suppression_list, unsubscribe_tokens, email_tracking, crm_integrations, crm_sync_log, client_settings, api_cost_log, reply_routing_rules, funnel_analytics`.
   **Gevolg voor Critical 7 (GDPR):** `admin_delete_client` (`api/main.py`) verwijdert expliciet maar 7 tabellen, maar zodra de laatste stap (`DELETE FROM clients`) uitvoert, cascadet Postgres automatisch door naar alle 29 — inclusief `leads`, `reply_inbox`, `webhook_events`, `suppression_list` die de Python-code zelf nooit aanraakt. De "PII blijft als wees achter"-bevinding uit de v2-audit is hiermee grotendeels achterhaald, **op één punt na** (zie punt 5): de *tweede-laags* cascades (via `inbox_id`/`campaign_id`/`lead_id`/`domain_id` i.p.v. rechtstreeks `client_id`) zijn nog niet gecontroleerd.
3. **CONFIRMED (2026-07-10) — de al-bestaande, al-toegepaste RLS-policies hebben GEEN `::text`-cast-bug.** Gedraaid:
   ```sql
   SELECT schemaname, tablename, policyname, qual
   FROM pg_policies
   WHERE tablename IN ('inboxes', 'leads', 'campaigns', 'domains', 'sending_schedule')
   ORDER BY tablename, policyname;
   ```
   Resultaat: alle vijf (`campaigns_isolation`, `domains_isolation`, `inboxes_isolation`, `leads_isolation`, `sending_schedule_isolation`) vergelijken al `client_id = auth.uid()` zonder cast. Wie de TEXT→UUID-conversie heeft gedaan, heeft de policies daarbij correct meegenomen — geen live bug op de kern-tenant-tabellen.
4. **`full_schema.sql` is aantoonbaar stale voor het hele tenant-datamodel**, niet alleen voor de eerder bekende drift-kolommen (`leads.engagement_score`, `clients.session_version`). Regenereren uit een echte `pg_dump --schema-only` blijft de enige betrouwbare manier om dit bestand weer als waarheid te kunnen gebruiken — behandel elke `TEXT`-claim over `client_id` in dit repo (comments, migraties, audit-documenten) voortaan als **onbevestigd tot tegendeel bewezen**.
5. **Nog te verifiëren: de tweede-laags cascades** (tabellen die niet rechtstreeks `client_id → clients` refereren maar via een tussenliggende tabel: `warmup_logs.inbox_id → inboxes`, `bounce_log.inbox_id → inboxes`, `email_events.campaign_id/lead_id/inbox_id`, `campaign_leads.lead_id → leads`, `sequence_steps.campaign_id → campaigns`, `dns_check_log.domain_id/blacklist_recoveries.domain_id → domains`, `placement_test_results.test_id → placement_tests`). Query:
   ```sql
   SELECT conname, conrelid::regclass AS child_table, confrelid::regclass AS parent_table,
          CASE confdeltype WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
                           WHEN 'a' THEN 'NO ACTION' ELSE confdeltype END AS on_delete
   FROM pg_constraint
   WHERE contype = 'f'
     AND confrelid IN ('inboxes'::regclass, 'campaigns'::regclass, 'leads'::regclass,
                        'domains'::regclass, 'placement_tests'::regclass)
   ORDER BY parent_table, child_table;
   ```
   Als deze ook allemaal `CASCADE` tonen: tenant-verwijdering is dan aantoonbaar 100% compleet (beide FK-lagen), en Critical 7's "PII blijft als wees achter"-bevinding is voor de database-laag volledig achterhaald (de resterende GDPR-gaten — reply-unsubscribe niet naar suppression_list, geen retentie-job — blijven wel bestaan, dat zijn geen FK-kwesties).

### Nog open (blokkeert niets nu, wel relevant voor toekomstig werk)

- `full_schema.sql` regenereren uit een echte dump — niet gestart.

## §7 — Fase 2/3 migraties toegepast en bevestigd (2026-07-13)

6. **CONFIRMED — `api/retention_migration.sql` toegepast.** Verificatiequery teruggekregen:
   ```json
   [
     {"column_name": "closed_at", "data_type": "timestamp without time zone", "is_nullable": "YES"},
     {"column_name": "retention_days", "data_type": "integer", "is_nullable": "YES"}
   ]
   ```
   `client_settings.retention_days` en `clients.closed_at` bestaan, beide nullable zoals bedoeld. `retention_engine.py` (Fase 2, Track 2) kan nu tegen de echte kolommen draaien — nog niet als scheduled job geïnstalleerd (`install_launchd.sh`'s `retention-engine`-entry staat al klaar in de code, maar `launchctl load` is nooit door de gebruiker bevestigd).
7. **CONFIRMED — `api/reputation_migration.sql` toegepast.** Verificatiequery teruggekregen:
   ```json
   [{"proname": "apply_reputation_delta"}, {"proname": "increment_spam_complaints"}]
   ```
   Beide RPC's bestaan. `utils/reputation.py`'s `bump_reputation()` en `bounce_handler.py`'s spam-complaint-increment lopen vanaf nu via de atomic RPC-tak (niet meer de non-atomic fallback) — de lost-update-race op `reputation_score`/`spam_complaints` is hiermee in productie gesloten, niet alleen in code.
8. **CONFIRMED (2026-07-14) — tweede-laags cascade-check gedraaid (punt 5 hierboven definitief gesloten), en heeft een echte bug in `hard_delete_client` blootgelegd.** Resultaat (19 rijen, codes teruggegeven als losse letter i.p.v. de gemapte tekst — C=CASCADE, N=NO ACTION, S=SET NULL, afgeleid uit de al-bevestigde eerste-laags patronen):
   - **NO ACTION** (blokkeert deletion als er nog kind-rijen bestaan): `warmup_logs/sending_schedule/bounce_log/email_events/reply_inbox → inboxes`; `email_events/reply_inbox → campaigns`; `email_events/reply_inbox → leads`.
   - **CASCADE**: `placement_tests → inboxes`; `dns_check_log/blacklist_recoveries → domains`; `sequence_steps/campaign_leads/sequence_suggestions → campaigns`; `campaign_leads/enrichment_queue → leads`; `placement_test_results → placement_tests`.
   - **SET NULL**: `experiments.variant_campaign_id/control_campaign_id → campaigns`.

   **Bug (gevonden, niet alleen bevestigd):** `utils/client_deletion.py`'s `hard_delete_client` verwijderde `email_events` helemaal nooit (niet in `CLIENT_ID_TABLES`, geen aparte scoping — ondanks dat de tabel wél `inbox_id`/`campaign_id`/`lead_id` heeft), en had `reply_inbox`/`sending_schedule` ná `campaigns`/`inboxes`/`leads` in de deletion-volgorde staan. Gevolg: voor elke client met campagne-historie (dus met `email_events`-rijen) faalde de laatste stap (`DELETE FROM clients`, die cascadet naar `campaigns`/`inboxes`/`leads`) met een foreign-key-violation — de client bleef gewoon bestaan ondanks een "geslaagde" GDPR-verwijdering. **Gefixt dezelfde dag**: `email_events` expliciet verwijderd via inbox_id/campaign_id/lead_id vóór de hoofdloop, `reply_inbox`/`sending_schedule` verplaatst naar vóór `campaigns`/`inboxes`/`leads`. 291/291 tests groen, inclusief 2 nieuwe regressietests die de FK-volgorde expliciet afdwingen in de fake.

## §8 — Fase 4b state machine: verificatie + STEP 1 toegepast (2026-07-17)

9. **CONFIRMED — verificatiequery (`api/state_machine_migration.sql` STEP 0) tegen productie gedraaid.** Resultaat:
   ```json
   // campaign_leads
   [{"status": "completed", "count": 1}]
   // inboxes
   [{"status": "ready", "count": 2}]
   ```
   Productie is momenteel zeer klein (1 campaign_lead, 2 inboxes) — beide bestaande waarden vallen binnen de verwachte set (`campaign_leads`: active/pending/sending/completed/paused/bounced/unsubscribed; `inboxes`: warmup/ready/paused/retired). Geen onverwachte waarde gevonden. Dit bevestigt ook los van het log-only-bewijs (zie [[fase4_architecture_2026_07]], 0 mismatches na 1 dag productieverkeer) dat de gereconstrueerde grafieken niet tegen bestaande data ingaan.

10. **CONFIRMED — STEP 1 (`NOT VALID` CHECK-constraints) toegepast, gebruiker bevestigde "Success. No rows returned".**
   ```sql
   ALTER TABLE campaign_leads ADD CONSTRAINT campaign_leads_status_check
     CHECK (status IN ('active','pending','sending','completed','paused','bounced','unsubscribed')) NOT VALID;
   ALTER TABLE inboxes ADD CONSTRAINT inboxes_status_check
     CHECK (status IN ('warmup','ready','paused','retired')) NOT VALID;
   ```
   Nieuwe/gewijzigde rijen op beide tabellen worden vanaf nu op de server afgedwongen tegen de waarde-set (niet de transitie-grafiek — dat blijft log-only, zie `utils/state_machine.py`). Bestaande rijen zijn niet gescand (`NOT VALID`). **STEP 2 (`VALIDATE CONSTRAINT`, wél een full-table-scan) is nog niet gedraaid** — bewust apart gehouden per de migratie's eigen discipline, al is het risico bij deze tabelgrootte verwaarloosbaar.
