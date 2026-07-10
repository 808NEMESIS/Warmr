# Warmr — Enterprise Audit (Q3 2026)

**Datum:** 2026-07-09
**Scope:** volledige codebase (~28.6K regels Python, frontend, n8n, schema, schedulers)
**Methode:** eerste-hands lezen van kernbestanden + zes parallelle specialist-audits (security/OWASP, database/multi-tenancy, warmup/deliverability, Heater-lifecycle, scheduler/queue/performance/kosten, GDPR/observability). Elke conclusie is onderbouwd met `file:line`.
**Verhouding tot `WARMR_AUDIT.md` (apr 2026):** die was overwegend "groen/opgelost". Deze audit is bewust adversarieel en vindt materiële problemen die daar niet in staan (o.a. 8 tabellen zonder RLS, cascade-FK's die stil falen, zero-click XSS, promotie-trigger zonder aanroeper).

---

## 0. Managementsamenvatting

Warmr is een verrassend volledig product: ~40 Python-engines, 130 API-endpoints, een doordacht frontend met reputatie-sparklines en een week-1→5 tijdlijn, en écht werkende bouwstenen (HMAC-webhooks met circuit breaker, Fernet-encryptie, RLS op de kernpaden, GDPR-export/erase-endpoints, DNS-drift-monitoring, seed-based placement-tests). Voor een self-hosted tool is dat sterk.

Maar gemeten langs de lat die je zelf stelt — *honderdduizenden mailboxen, volledig GDPR-compliant, minimale cloudkosten, automatische samenwerking met Heater, beter dan Instantly/Smartlead/Lemlist* — zijn er **structurele barsten** die je vóór opschaling moet dichten:

1. **De kern-belofte (automatische overdracht naar Heater) bestaat niet.** De promotie-functie die een mailbox op `ready` zet heeft **geen enkele aanroeper** (geen cron, geen UI, en het endpoint is JWT-only dus Heater kán het niet eens aanroepen). Er wordt **nooit** een `inbox.warmup_complete`-event verstuurd. Mailboxen worden in de praktijk nooit "ready", en Heater krijgt niets te zien. Bovendien is de architectuur omgekeerd t.o.v. je beschrijving: Heater is géén aparte sender die warme mailboxen consumeert — het is een lead-leverancier, en Warmr verstuurt zélf (`campaign_scheduler.py`). Warmr en Heater zitten ook **niet** in dezelfde monorepo (Heater is een aparte GitHub-repo).

2. **Multi-tenant isolatie lekt aan de randen.** 8 tenant-tabellen hebben **RLS uitgeschakeld** (o.a. `webhook_events`, `webhook_logs`, `unsubscribe_tokens`) → elke ingelogde tenant kan via de anon-key andermans data lezen. De "isolation by construction" cascade-FK's worden door een TEXT↔UUID-mismatch **stil nooit aangemaakt**.

3. **Een zero-click stored XSS** via e-mail-onderwerp/afzender kaapt de sessie van de operator (token in localStorage, CSP staat `unsafe-inline` toe) → volledige overname en, via impersonation, cross-tenant compromittering.

4. **Kosten schalen catastrofaal.** Elke warmup-mail én elke reply doet een verse Claude-call, de budget-check full-scant `api_cost_log` vóór élke call (O(N²)), en warmup stopt nooit voor "ready" mailboxen. Model: **~€200k/maand aan LLM alleen bij 100k mailboxen**.

5. **De warmup zelf zou een moderne spamfilter waarschijnlijk niet misleiden** (en mogelijk netto-negatief zijn): geen threading-headers (nep-"Re:"-conversaties), een gesloten all-Gmail-loop, een constante reply-ratio, een trapfunctie-volume, en weekend/feestdag-verzending standaard aan.

6. **GDPR faalt op de fundamenten** ondanks goede losse bouwstenen: geen afgedwongen bewaartermijn (terwijl de API klanten vertelt dat data na 30 dagen weg is), tenant-verwijdering laat de meeste PII als wees achter, en reply-gebaseerde "uitschrijven" wordt genegeerd.

7. **Er is geen enkele scheduler-veiligheid.** Drie overlappende schedulers (cron + launchd + n8n), geen locking, niet-atomaire tellers, geen idempotente sends → dubbele/drievoudige verzending = reputatievernietiging.

**Totaalscore: 3,9 / 10** (zie §18). Dit is geen "afbreken" — het fundament is bruikbaar — maar het is nog **niet enterprise-ready** en zeker nog niet klaar voor 100k mailboxen. De roadmap in §17 zet de volgorde.

---

## 1. Architectuur

**Score: 4/10.**

### 1.1 Platte structuur, geen lagen (geen Clean Architecture / DDD)
- **Probleem:** Er is geen domein-, service- of repository-laag. 24 modules roepen zelf `create_client()` aan en laden zelf `.env`; `api/main.py` doet **208 directe `_supabase.table()`-calls**. Business-logica, data-access en transport zitten door elkaar.
- **Waarom een probleem:** Geen dependency-inversion; elke engine kent de DB-vorm direct. Een schemawijziging raakt tientallen bestanden. Onmogelijk om data-access te mocken/testen zonder een echte Supabase.
- **Impact:** Hoge coupling, lage testbaarheid, trage evolutie.
- **Risico:** Elke refactor is riskant; schema-drift (§6) glipt er ongemerkt in.
- **Oplossing:** Introduceer een dunne repository-laag (`repositories/inboxes.py`, `…/leads.py`) die alle Supabase-calls inkapselt; engines praten met repositories, niet met de client. Eén gedeelde `core/db.py` + `core/config.py`.
- **Stappen:** (1) `core/db.py` met één gecachte client + settings; (2) per aggregaat een repository; (3) engines herbedraden; (4) verbied directe `.table()` buiten repositories via een lint-check (bestaat al deels: `scripts/check_service_role_queries.py`).
- **Prioriteit:** High.

### 1.2 `api/main.py` is een God-object (6.475 regels, 130 endpoints)
- **Probleem:** Alle routes, middleware, business-logica en 208 DB-calls in één bestand.
- **Impact:** Merge-conflicten, onvindbaarheid, cognitieve last; onmogelijk om een subteam een bounded context te geven.
- **Oplossing:** Splits in FastAPI `APIRouter`-modules per context: `inboxes`, `campaigns`, `leads`, `funnel`, `analytics`, `gdpr`, `admin`, `tracking`. (De `public_api.py`-splitsing bewijst dat het patroon al bekend is.)
- **Prioriteit:** High.

### 1.3 Verantwoordelijkheden die opgesplitst moeten
- `imap_processor.py` (1.179 r) doet spam-rescue **én** reply-generatie **én** reputatie-update **én** prospect-reply-detectie **én** webhook-emissie. Splits in `SpamRescueService`, `WarmupReplyService`, `ReputationService`, `InboundReplyRouter`.
- `campaign_scheduler.py` (1.186 r) doet queue-selectie, suppressie, spintax, A/B, tracking, SMTP, throttling. Splits sender ↔ scheduler ↔ personalisatie.
- `diagnostics_engine.py` (905 r) is tegelijk monitor, auto-pause-actor en auto-resume-actor — het muteert status zonder centrale state-machine (zie §3/§4).

### 1.4 Ontbrekende modules
- **Centrale state-machine** voor `inboxes.status` (nu muteren 6 plekken de status met last-write-wins — §4).
- **Retention-engine** (GDPR — §11).
- **Metrics-exporter** voor business-metrics (§13).
- **Reputatie-ingest** van externe bronnen (Postmaster/SNDS — §2/§12).
- **Outbox/relay** voor betrouwbare events naar Heater (§4).

### 1.5 CQRS / Event-Driven
- **CQRS lite** is zinvol voor analytics: schrijf-pad (sends/logs) is write-heavy; lees-pad (dashboards) doet nu `count(exact)` op grote tabellen (§7). Splits met een materialized read-model (`analytics_cache` bestaat al — bouw erop verder).
- **Event-driven** is de juiste ruggengraat voor Warmr↔Heater (§4): er is al een `webhook_events` outbox-tabel en een dispatcher met HMAC + circuit breaker — het patroon is er, alleen de emitters ontbreken.

---

## 2. Mail-warmup-logica

**Score (deliverability): 4/10. Verdict: zou een 2026 Gmail/Microsoft-filter waarschijnlijk niet betrouwbaar misleiden — mogelijk netto-negatief.**

Hoe de parameters nu bepaald worden en waar het detecteerbaar wordt:

| Parameter | Hoe bepaald nu | Probleem |
|---|---|---|
| Mails/dag | Vaste lookup `{1:10,2:20,3:35,4:45}`, week5+=60 (`warmup_engine.py:53-59,283-290`), week = `(dagen//7)+1` | Perfecte trapfunctie, identiek per inbox, springt op dag 7/14/21 tegelijk voor de hele cohort → **volume-fingerprint** |
| Replies / reply-ratio | Globale constante `REPLY_RATE=0.35` en `REPLY_BACK_RATE=0.50` (`imap_processor.py:59,904,1034,1093`) | Elke mailbox exact 35% → statistisch signaal; harde 3-turn-cap (`re_depth>=3`) i.p.v. lange-staart |
| Randomisatie/timing | Enkel `random.uniform(1,30)` s (`:625`); cron-tick elke 1200 s | Vlakke jitter + cron-clustering → inter-send-histogram piekt op ~1200 s = **machinecadans** |
| Weekends | **Niet onderdrukt** in de engine; `is_within_send_window` checkt alleen HH:MM (`:466-474`) | Warmup draait za/zo onder launchd; `SEND_DAYS` wordt **nergens in Python gelezen** (dode config) |
| Feestdagen | **Geen kalender** | Zakelijke "quick question" op Koningsdag/25 dec = harde tell (BENELUX-markt!) |
| Tijdzones | Naïeve server-`datetime.now()` (`:473`) | Eén globale tz; een "Belgische" en "Nederlandse" mailbox zijn identiek in verzendtijd |
| Mailbox-/domein-/IP-reputatie | **Wordt nergens gelezen om volume te gaten** | Nieuw domein op mogelijk-vervuild gedeeld Google-IP ramt op identiek schema |

### Kritieke bevindingen (volledig)

**2.1 (Critical) Geen threading-headers op warmup-mail → nep-conversaties.** `warmup_engine.py:497-505` en de reply in `imap_processor.py:433-443` zetten **geen** `Message-ID`/`In-Reply-To`/`References`; de "reply" is enkel een `"Re: "`-string, en thread-diepte wordt geteld met `subject.count("re:")`. Gmail threadt op `References`, niet op onderwerp — dus elke "reply" is een losstaande wees. *Het campagne-pad doet dit wél goed (`campaign_scheduler.py:689-694`)* — kopieer die code. **Oplossing:** vang de originele `Message-ID` bij ontvangst, rijg `In-Reply-To`+`References` door, zet expliciet `Message-ID`+`Date` op de initiële send.

**2.2 (Critical, architecturaal) Gesloten all-Gmail reciproke loop.** Elke client-inbox mailt dezelfde gedeelde pool `WARMUP_NETWORK_*` accounts, die **altijd Gmail** zijn (`imap_processor.py:130`), en die mailen terug. Google ziet **beide** kanten van vrijwel elke uitwisseling: een dichte bipartiete kliek, ~100% wederkerigheid, nooit contact buiten de ring. Dit is precies de topologie die warmup-detectie herkent. **Oplossing (hard):** diversifieer providers (Outlook/M365/eigen domeinen), roteer/vergroot de pool, injecteer niet-wederkerig en eenrichtingsverkeer, verlaag reciprociteit ver onder 100%. Realistisch: positioneer warmup als reputatie-*onderhoud*, niet als magische vertrouwensgenerator.

**2.3 (Critical) Reputatiescore is interne fictie die echte sends stuurt.** `reputation_score` komt volledig uit Warmr's eigen warmup-events (`imap_processor.py:231-258`); **niets leest Google Postmaster, SNDS of Talos**. Toch beslist dit getal echte outbound: `inbox_rotator.py:156-158` laat warmup-inboxen toe tot campagnes bij `rep>=70`. De `placement_tester` produceert wél grondwaarheid (inbox/spam/missing) maar wordt **nooit teruggekoppeld** naar de score. Een inbox kan "78/100, ready" lezen terwijl hij bij echte ontvangers in spam zit. **Oplossing:** herbaseer readiness op meetbare signalen (Postmaster/SNDS + placement-resultaten + echte bounce/complaint), degradeer de interne score tot hooguit een kleine input.

**2.4 (Critical) Geen DNS-preflight vóór warmup.** `warmup_engine.process_inbox` raadpleegt de `domains`-tabel/`dns_check` nooit; een domein zonder SPF/DKIM/DMARC warmt op volle schema op. `dns_monitor.py` draait apart en **notificeert enkel** — het zet nooit `warmup_active=false`. Aanwezig vs. afwezig: SPF/DKIM/DMARC/DNSBL gecheckt; **MX wordt door de drift-monitor niet aangeroepen**; **rDNS/PTR, ARC, BIMI, MTA-STS, HELO-consistentie afwezig**. **Oplossing:** harde preflight in `process_inbox` (SPF+DKIM+DMARC+MX groen, DMARC-alignment) vóór de eerste send; koppel `dns_monitor`-falen aan auto-pause.

**2.5 (High) Zelf-rescue uit spam wordt over-gecrediteerd.** `+1.0` reputatie per rescue is de grootste positieve delta in het systeem (`imap_processor.py:65-73`) → een inbox die steeds in zijn **eigen** spam belandt en zichzelf redt, stijgt het snelst. In spam belanden hoort readiness te *verlagen*. Voor niet-Gmail-ontvangers traint "not spam" alleen je lokale filter, niet de afzenderreputatie. **Oplossing:** rescue-krediet netto-negatief maken t.o.v. spam-landing; alleen tellen waar de afzender ook Gmail is.

**2.6 (High) Detecteerbare-patronen-bundel** (2.x volume-trap + constante reply-ratio + cron-timing + weekend/feestdag standaard aan). Elk apart overleefbaar, samen een leerboek-warmup-signatuur. **Oplossing:** smooth volume-curve met per-inbox ruis; reply-propensity uit Beta-verdeling per paar; Poisson-verdeelde verzendtijden gewogen met een bimodale kantooruren-curve; weekday+feestdag-gate (PyPI `holidays` NL/BE/LU); consumeer `SEND_DAYS`.

*Nevenbug:* week5+ capt op 60 i.p.v. gedocumenteerde 50–80, en omdat `select_recipient` geen herhaling per dag toestaat is 60/dag **fysiek onbereikbaar** met een pool van 20–30 — inboxen stallen stil op poolgrootte (`:591-597`).

---

## 3. Wanneer is een mailbox "warm"? — Maturity-model

**Huidige realiteit:** het `inboxes.status`-veld kent in de praktijk **3 waarden** (`warmup`, `ready`, `paused`) plus een **fantoom** `retired` dat overal wordt weggefilterd maar **nergens wordt geschreven**. Er is geen `CHECK`-constraint of enum. READY en ACTIVE zijn niet te onderscheiden. Je kunt uit de DB niet aflezen of een mailbox nu door een externe sender gebruikt wordt.

**Huidige promotie-criteria** (`auto_promote.py:47-90`, ALLE waar): `warmup_active`, `days_in_warmup>=28`, `reputation_score>=70` (met `int()`-truncatie-bug, `:54`), `last_spam_incident` leeg of >14d, `auto_pause_count_24h==0`, `daily_warmup_target>=50`. **De gedocumenteerde `reply_rate>=25%` wordt niet gecontroleerd** (stil weggevallen).

### Voorgesteld 7-staten-model + exacte triggers

Voeg toe aan `inboxes`: Postgres-enum op `status`, plus `status_reason TEXT`, `previous_status TEXT`, `active_owner TEXT DEFAULT 'warmr'`, `held_by TEXT`, `held_until TIMESTAMPTZ`.

| Transitie | Voorwaarde | Getriggerd door |
|---|---|---|
| — → **NOT_READY** | inbox aangemaakt | `POST /inboxes` |
| NOT_READY → **WARMING** | eerste warmup-send / `warmup_start_date` gezet | `warmup_engine` |
| WARMING → **ALMOST_READY** | rep≥60, leeftijd≥21d, spam_clear | promotie-sweep (nieuw) |
| ALMOST_READY → **READY** | volledige criteria **incl. reply_rate≥25%**, `float` i.p.v. `int` | `check_and_promote_inbox` event-driven vanuit reputatie-update + backstop-sweep → **emit `inbox.warmup_complete`** |
| READY → **ACTIVE** | Heater claimt lease | `POST /inboxes/{id}/claim` (Heater, API-key) |
| ACTIVE → READY | lease vrijgegeven/verlopen | `POST /release` of `held_until` |
| WARMING/READY/ACTIVE → **PAUSED** | SMTP-burst≥3/60m, rep<35, handmatig; bewaar `previous_status`; **advisory-only tijdens ACTIVE** (emit `inbox.health_degraded`, niet grijpen) | `diagnostics_engine`, handmatig |
| PAUSED → previous_status | auto-resume-venster of handmatig; **herstel `previous_status`, niet hardcoded WARMING** | `diagnostics_engine` |
| any → **RETIRED** | blacklist, hard-bounce>3%, herhaalde complaints, Heater `/report burn` → **emit `inbox.retired`** | `bounce_handler`, `dns_monitor`, Heater-report |
| RETIRED → NOT_READY | alleen handmatig | dashboard |

**Alle** transities lopen via één helper `transition_inbox(id, allowed_from:set, to, reason)` met een conditionele `UPDATE … WHERE status = ANY(allowed_from)`; 0 rijen = no-op + geen event. Dit dood de dubbele-activatie en clobber-races in §4.

---

## 4. Automatische overdracht naar Heater

**Score (integratie-robuustheid): 3/10.** Het plumbing dat bestaat is goed gebouwd (HMAC, replay-nonce, retries, circuit breaker, SSRF-guard, decision-logging). Maar de lifecycle-overdracht zelf is grotendeels **inert of onveilig**.

**Architecturale realiteitscheck:** in de huidige build verstuurt **Warmr zelf** (`campaign_scheduler.py` + `inbox_rotator.select_inbox`). Heater is een lead-leverancier die leads instuurt via `POST /api/v1/leads` en `GET /api/v1/inboxes?status=ready` **leest**. De mailbox verlaat Warmr's procesgrens nooit (`public_api.py:702`: *"Read-only — inbox management happens in the dashboard"*). "READY → overdracht" is vandaag "Warmr zet een statusveld dat Heater misschien pollt".

### 4.1 (Critical) De promotie-trigger heeft geen enkele aanroeper
- **Probleem:** `check_and_promote_inbox` wordt alleen aangeroepen door `POST /inboxes/{id}/check-promotion` (`main.py:581-594`). Grep over `install_launchd.sh`, `crontab_warmr.sh`, `n8n/*.json` en de frontend: **niets roept het aan**. Het endpoint is bovendien `ClientId`-gated (JWT), dus Heater (API-key) kán het niet aanroepen.
- **Impact:** Inboxen worden in de praktijk **nooit** `ready`. `GET /inboxes?status=ready` blijft leeg; Warmr's eigen sender gebruikt ondertussen `warmup`-inboxen bij rep≥70 (`inbox_rotator.py:158`) → stille divergentie tussen wat Heater ziet en wat Warmr doet.
- **Oplossing:** roep promotie event-driven aan vanuit `imap_processor` na elke reputatie-delta + een backstop launchd-sweep over alle `warmup`-inboxen; voeg `reply_rate≥0.25` toe; `float()` i.p.v. `int()`.
- **Prioriteit:** Critical.

### 4.2 (Critical) `inbox.warmup_complete` wordt gedeclareerd maar nooit ge-emit
- **Probleem:** Het event is gedeclareerd (`public_api.py:73`), gedocumenteerd (`public_migration.sql:51`) en ge-test (`test_heatr_integration.py:366`) — maar **nergens ge-emit**. Alleen `lead.replied/interested/enriched/clicked` worden echt verstuurd. Ook `lead.bounced`, `lead.unsubscribed`, `campaign.completed` zijn gedeclareerd-maar-nooit-ge-emit. Ontdekking is puur **pull**; er is geen push op READY.
- **Oplossing:** emit via de bestaande outbox (`webhook_events`) + dispatcher (HMAC/retries/breaker aanwezig) vanuit het promotie-pad; houd de poll-endpoint als reconciliatie-fallback.
- **Prioriteit:** Critical.

### 4.3 (Critical/High) Dubbele activatie: geen state-guard op de write
- **Probleem:** `auto_promote.py:154` doet `update({status:ready}).eq("id",id)` **zonder** `.eq("status","warmup")`. De idempotentie is een niet-atomaire read-check (`:127`, TOCTOU). Twee gelijktijdige calls promoveren beide; met een gekoppelde webhook → dubbele `inbox.warmup_complete` → Heater dubbel-activeert. Geen idempotency-key op de wire.
- **Oplossing:** conditionele update (`.eq("status","warmup")`, 0 rijen = "al gepromoveerd, geen event") + deterministische `idempotency_key` per event + unique index `(inbox_id,event_type,idempotency_key)`.
- **Prioriteit:** Critical (nu Medium omdat niets het schedulet, maar P0 zodra een sweep bestaat).

### 4.4 (Critical) Race-condities: last-write-wins over veiligheid
- **Probleem:** Geen enkele status-writer gebruikt een conditionele `WHERE status=…`; alles is `.eq("id")` last-write-wins. Scenario A: diagnostics pauzeert (rep gezakt) terwijl promotie tegelijk `ready` schrijft → **promotie wint, veiligheid verliest** (en `warmup_active` blijft True). Scenario B: een `ready` inbox krijgt een transiënte SMTP-burst → pause → auto-resume zet `status="warmup"` **hardcoded** (`diagnostics_engine.py:566`) → de inbox degradeert stil READY→warmup, verdwijnt uit Heater's lijst, en moet promotie opnieuw verdienen (die nooit vuurt — §4.1).
- **Oplossing:** centrale `transition_inbox()` met conditionele updates; bewaar `previous_status` zodat resume herstelt; pause = hoogste prioriteit (promotie mag `paused` niet overschrijven).
- **Prioriteit:** Critical.

### Ontwerp — Warmr↔Heater-contract (event-driven + claim-based lease)

Hergebruik de bestaande `webhook_events`→`webhook_dispatcher`-pipeline. Voeg toe: idempotency-keys + de ontbrekende emitters. Nieuwe API (API-key-auth):

```
GET  /api/v1/mailboxes/available      → [{inbox_id,email,reputation_score,daily_remaining,leasable}]
POST /api/v1/inboxes/{id}/claim       {ttl_seconds} → conditionele UPDATE WHERE status='READY' AND active_owner='warmr';
                                        200 {lease_token, held_until} of 409 als al geclaimd    ← de lock
POST /api/v1/inboxes/{id}/release     {lease_token} → ACTIVE→READY
POST /api/v1/inboxes/{id}/usage       {lease_token, sent_count, bounces} → reconcile tellers
POST /api/v1/inboxes/{id}/report      {lease_token, kind:"burn|bounce|complaint"} → transitie + reputatie-delta + Warmr staakt warmup
```

- **Dubbele activatie voorkomen:** `claim` = conditionele update op `active_owner='warmr'`; tweede claimant krijgt 409.
- **Races voorkomen:** zolang `active_owner='heatr'` slaan `warmup_engine` + `diagnostics_engine` de mailbox over (of emitten advisory `inbox.health_degraded` i.p.v. te grijpen). `daily_reset` mag tellers van `active_owner='heatr'` niet nullen zonder reconciliatie.
- **Events (nieuw/gefixt):** `inbox.warmup_complete|paused|retired|health_degraded`, `lead.bounced|unsubscribed`, `campaign.completed` — elk met `idempotency_key`.

---

## 5. Synchronisatie tussen Heater en Warmer

**Ownership vandaag: er ís geen model.** Warmr bezit alles en muteert vrij, altijd:
- Na "ready" blijft Warmr warmup draaien (`auto_promote.py:156` laat `warmup_active=True`) — dus zelfs in de bedoelde split geeft Warmr de mailbox nooit af.
- Elke engine kan een "ready" inbox pauzeren/hervatten/patchen; `PATCH /inboxes/{id}` (`main.py:544`) laat een caller `status` op **elke willekeurige string** zetten, zonder validatie.
- Tellers/health/reputatie: alle Warmr. `daily_sent` genulld door `daily_reset`; `reputation_score` door `imap_processor`+`bounce_handler`; cooldown/auto-pause door `diagnostics_engine`. Heater kan alleen **lezen** (30 s cache), heeft geen write-pad, geen besef van "ik verstuurde N vanuit deze mailbox".

**Sync-bugs (High):**
- **Bounce-webhook kapot:** `bounce_handler.py:436` schrijft een `email_events`-rij van type `bounced`, maar **nooit** een `webhook_events`-rij → `lead.bounced` bereikt Heater nooit.
- **Mailbox-burn:** bij auto-pause/critical-pause vuurt **geen event**; een verbrande mailbox valt enkel uit de `ready`-lijst — geen positief "deze is verbrand"-signaal.
- **Heater→Warmr terug-callback bestaat niet:** de publieke inbox-endpoints zijn GET-only. Detecteert Heater een verbrande mailbox, dan kan het Warmr **niet** vertellen om warmup te pauzeren/retiren.

**Aanbevolen ownership-grens:** Warmr bezit `reputation_score` + warmup-tellers; Heater bezit campagne-sends en rapporteert via `POST /inboxes/{id}/usage`; een expliciete **lease** (`active_owner`/`held_until`) coördineert wie mag muteren.

---

## 6. Database-audit

**Score: schaalbaarheid 3/10, multi-tenant-isolatie 4/10.**

### 6.1 (Critical) 8 tenant-tabellen hebben RLS UIT → cross-tenant datalek
- **Probleem:** `webhook_logs` (`:322`), `webhook_events` (`:343`, bevat lead-payloads), `warmup_network_accounts` (`:392`), `network_health_log` (`:410`), `placement_test_results` (`:486`), `dns_check_log` (`:521`), `blacklist_recoveries` (`:537`), `unsubscribe_tokens` (`:906`) krijgen **nooit** `ENABLE ROW LEVEL SECURITY`.
- **Waarom:** Supabase geeft `anon`/`authenticated` standaard `GRANT` op alle `public`-tabellen. Zonder RLS = **elke ingelogde tenant leest/schrijft alle rijen van álle tenants** met de anon-key. `webhook_events.payload` bevat namen/e-mails/reply-inhoud; `unsubscribe_tokens` lekt `lead_email`+`client_id` en laat tokens raden.
- **Risico:** Concreet, testbaar datalek — precies wat `test_rls_isolation.py` "CRITICAL BREACH" noemt, maar die test controleert alleen `leads`.
- **Oplossing:** RLS aan + policy per tabel (direct `client_id` of via parent-subquery); `REVOKE ALL … FROM anon, authenticated` voor echt backend-only tabellen.
- **Prioriteit:** Critical.

### 6.2 (Critical) Cascade-FK's falen STIL door TEXT↔UUID-mismatch
- **Probleem:** `client_id` is overal `TEXT`, maar `clients.id` is `UUID`. `tenancy_hardening_migration.sql:12-117` voegt 9 FK's toe (`client_id(TEXT) REFERENCES clients(id)(UUID)`), elk in `EXCEPTION WHEN OTHERS THEN NULL`. Postgres weigert de type-mismatch → **geen enkele FK wordt aangemaakt**, de fout wordt geslikt.
- **Impact:** De hele "isolation by construction / cascade deletes"-claim is fictie. Geen `ON DELETE CASCADE` van client → data. Een client verwijderen laat overal wees-rijen achter (GDPR — §11).
- **Oplossing:** migreer `client_id` naar `UUID` (gebruik `auth.uid()` zonder `::text`), dán FK's toevoegen **zonder** blanket-catch; verifieer met `pg_constraint`.
- **Prioriteit:** Critical.

### 6.3 (Critical) Nul indexen op de hoogste-volume tabellen
- **Probleem:** `warmup_logs`, `sending_schedule`, `bounce_log` hebben **geen enkele index** (alleen PK). Ook `inboxes.client_id`, `domains.client_id`, `leads.client_id` zijn niet bruikbaar geïndexeerd (`leads`-unique begint met `email`).
- **Impact:** De campagne-scheduler doet elke 5 min een seq-scan over de hele wachtrij; warmup-analytics en élke RLS-subquery (`warmup_logs`→`inboxes.client_id`) scannen sequentieel. Eerste muur bij opschaling.
- **Oplossing (met `CONCURRENTLY`):**
  ```sql
  CREATE INDEX CONCURRENTLY idx_warmup_logs_inbox_ts ON warmup_logs(inbox_id, timestamp DESC);
  CREATE INDEX CONCURRENTLY idx_sched_status_time ON sending_schedule(status, scheduled_at) WHERE status='pending';
  CREATE INDEX CONCURRENTLY idx_bounce_inbox_ts ON bounce_log(inbox_id, timestamp DESC);
  CREATE INDEX CONCURRENTLY idx_inboxes_client_status ON inboxes(client_id, status);
  CREATE INDEX CONCURRENTLY idx_campaign_leads_due ON campaign_leads(status, next_send_at) WHERE status='active';
  ```
- **Prioriteit:** Critical.

### 6.4 (High) Schaal-tekortkomingen richting miljoenen
- Geen partitionering/BRIN op onbegrensde append-tabellen (`warmup_logs`, `email_events`, `email_tracking`, …) → miljarden rijen, trage vacuum, TXID-wraparound-risico. → maand-range-partitionering + BRIN + partition-drop retentie.
- Geen archivering/retentie (§11).
- `TIMESTAMP` zonder tijdzone overal → send-windows/`daily_reset` dubbelzinnig rond DST (CET↔CEST). → `TIMESTAMPTZ`.
- Willekeurige UUIDv4-PK op high-insert tabellen → write-amplificatie/index-bloat. → `BIGINT IDENTITY` of UUIDv7 voor logtabellen.

### 6.5 (High) Constraints, unique-keys, migratie-hygiëne
- **Geen enkele `CHECK`-constraint** op enum-achtige kolommen (`status`, `bounce_type`, `dmarc_phase`, …) → typfouten (`'complaint'` i.p.v. `'spam_complaint'`) worden geaccepteerd en breken filters stil.
- `sending_schedule` heeft **geen idempotency-key** → dubbele run plant dezelfde stap tweemaal in.
- `inboxes.email`/`domains.domain` zijn **globaal** uniek (niet per tenant) → enumeratie-lek dwars door RLS (constraint werkt vóór RLS) + blokkeert agency-scenario's.
- Niet-idempotente migraties (`api/migrations.sql`, `analytics_migration.sql`: kaal `CREATE TABLE`/`CREATE POLICY` zonder `IF NOT EXISTS`/`DROP`), geen versietabel, `full_schema.sql` is **verouderd** (mist `funnel_analytics`, `reply_routing_rules`, `reply_inbox`-reply-kolommen; `leads.engagement_score` staat in **geen enkel** schemabestand). → adopteer Supabase CLI-migraties + regenereer `full_schema.sql` uit `pg_dump`.
- RLS-policies zijn allemaal `FOR ALL USING` zonder `WITH CHECK`; `api_cost_log` (facturatie!) en `decision_log`/`notifications` (audit) zijn door de tenant zélf **verwijderbaar** → omzetderving + audit-manipulatie. → `FOR SELECT` + service-role-only writes op billing/audit-tabellen.

---

## 7. Performance

**Score: 3/10.**

- **(High) N+1 in `process_lead`:** ~12–15 sequentiële Supabase-round-trips per e-mail (`campaign_scheduler.py:773-991`), incl. `load_client_settings` dubbel (`:854` én `:913`). Bij 100k mailboxen = miljoenen seriële queries. → hoist per-campagne/per-client data (settings, suppressie-set, uur-tellers) vóór de lead-loop.
- **(High) `count(exact)` op onbegrensde tabellen in hot loops:** `calculate_bounce_rate` (2× full count over `email_events`), `count_inbox_sends_last_hour` **per lead**, warmup uur-cap-count per inbox per run. → rollende tellers / gematerialiseerde rollups; nooit `count(exact)` in een hot loop.
- **(High) Blokkerende SMTP/IMAP met login-per-bericht:** nieuwe `SMTP_SSL().login()` per e-mail; IMAP doet **3 logins per client-inbox per run** + 1 per warmup-account, volledig synchroon, één proces. → hergebruik één sessie per inbox per run; async workers geshard per inbox; batch IMAP SEARCH.
- **(Critical bij schaal) IMAP-poll = O(mailboxen)-verbindingsbom:** elke 10 min, ~3 logins/inbox → **432.000 IMAP-logins/dag per 1.000 mailboxen → ~43M/dag bij 100k**, en past fysiek niet in een 10-min-venster. → sharding + per-inbox-cadans + IMAP IDLE / provider-push (Gmail Pub/Sub, MS Graph webhooks).

---

## 8. Scheduler-audit

**Score: 2/10.**

- **(Critical) Drie overlappende schedulers voor dezelfde jobs:** `crontab_warmr.sh` (scripts direct), `install_launchd.sh`+plist (scripts direct), n8n (via API-endpoints die dezelfde `main()` draaien). Niets dwingt exclusiviteit af. Twee tegelijk aan = **2×–3× verzending** = reputatievernietiging. *Latente landmijn:* `api/main.py:1510` roept `campaign_scheduler.main(client_id=…)` aan terwijl de signatuur `main()` is → TypeError, stil geslikt → nu is het n8n-campagnepad een no-op; fix de signatuur en je krijgt meteen dubbele sends. → kies **één** scheduler; Postgres advisory-lock per job.
- **(Critical) Geen overlap-guard:** geen `flock`/PID/advisory-lock. `process_lead` slaapt `random.uniform(30,180)` s **per lead** → een run kan >60 min duren terwijl de plist elke 300 s vuurt. Cron vs launchd vs n8n zijn aparte processen → echt gelijktijdige instanties op dezelfde rijen. → advisory-lock bovenaan elke `main()`.
- **(Critical, geverifieerd) `daily_reset` nullt de dagteller elk uur onder launchd.** `daily_reset.py` zet onvoorwaardelijk `daily_sent=0` (geen datum-guard), en `install_launchd.sh:63` schedulet het met `StartInterval 3600` ("hourly re-check, idempotent" — **onjuist**: het is alleen een no-op als er niets verzonden is). Gevolg: de dag-cap reset elk uur → tot ~12× over-verzending in kantooruren. (Via `crontab` draait het correct om 00:05.) → geef `daily_reset` een datum-guard (`last_reset_date`) of schedule het uitsluitend om middernacht.
- **(Critical) Sends niet idempotent:** `process_lead` leest een due-lead, verstuurt SMTP, schrijft dán status — **geen** atomaire claim (`UPDATE … SET status='sending' WHERE status='active'`), geen idempotency-key. Crash na `sendmail()` vóór de status-write → **her-verzending** volgende tick. → atomaire claim + unique `(campaign_lead_id, sequence_step)` op `email_events`.
- **(High) Niet-atomaire `daily_sent`:** `warmup_engine.update_daily_sent` roept `rpc("increment_daily_sent")` aan, maar **die functie bestaat in geen enkel schema** → gooit altijd → valt terug op read-modify-write met de bij runstart gelezen waarde → lost update → cap onderschat en overschreden. → maak de SQL-functie echt (`UPDATE … SET daily_sent = daily_sent+1 RETURNING …`).

---

## 9. Queue-audit

- **(Critical) `pending`-queues zonder `SKIP LOCKED`/atomaire claim:** zowel de API-fallback (`main.py:1515-1522`) als `load_due_campaign_leads` (`:237`) selecteren met een platte filter → twee workers pakken dezelfde rijen → dubbele sends. → claim-then-work `UPDATE … SET status='claimed', worker_id=? WHERE … AND status='pending' RETURNING *`.
- **(High) Webhook-dispatcher: retries zonder claim → dubbele levering.** `process_retries` selecteert `success=false` zonder rij-claim, en er draaien **twee** dispatchers (standalone `while True` + n8n elke 1 min). HMAC/nonce beschermt tegen replay maar niet tegen echte dubbele dispatch (nonce is vers per poging). Circuit breaker is aanwezig (goed). → precies één dispatcher + claim + stabiel `event_id` voor dedup bij de ontvanger.
- **(Medium) Enrichment-queue:** claimt wél atomair (goed, + unique index tegen dubbele rijen), maar failures gaan direct terug naar `pending` **zonder `next_retry_at`/backoff** → poison-message hamert 3× snel; geen dead-letter-review; `enqueue_leads_bulk` doet 1 insert per rij. → backoff + DLQ-status + batch-insert.

---

## 10. Security-audit (OWASP Top 10 2021)

**Score: 5/10.** Sterk fundament (correcte JWT-signature-verificatie **zonder** algorithm-confusion; JWT-afgeleide tenant-isolatie zonder SQLi/IDOR op de bekeken routes; encrypted secrets; audit-logging; goede security-headers; SSRF-guard op het primaire webhook-pad). Zwaar omlaag getrokken door de top-3 hieronder.

| # | Bevinding | OWASP | Prioriteit |
|---|---|---|---|
| 1 | **Zero-click stored XSS** via inbound e-mail `subject`/`from_email` → `innerHTML` in `unified-inbox.html:365-447` + 30-s toast-poll (`app.js:135-147,389-416`). CSP staat `script-src 'unsafe-inline'` toe → geen mitigatie. Supabase-token in **localStorage** → een gecrafte e-mail exfiltreert de sessie **zonder klik** → volledige overname; via impersonation cross-tenant. | A03 | **Critical** |
| 2 | **Unguarded SSRF in CRM-pad:** `crm_dispatcher.py:137` (POST met lead-PII) en `_verify_webhook_url` (`main.py:5056-5065`, blind GET) roepen **geen** `assert_url_safe` aan; `skip_verification:true` (`:5568`) omzeilt zelfs de challenge → bereikt `169.254.169.254`/loopback. | A10 | **High** |
| 3 | **Hard-coded fallback HMAC-secret** `"fallback-secret-change-me"` (`main.py:6061,4900`, `campaign_scheduler.py:148`). Als `WARMR_API_TOKEN` leeg is, kan iedereen geldige tracking-/GDPR-export-tokens voor **elke** `client_id` maken → cross-tenant analytics-vervalsing. | A02/A08 | **High (conditioneel)** |
| 4 | **Brute-force-bescherming client-side only** (login gaat direct browser→Supabase; `/auth/login-attempt` logt slechts) **+** geen `X-Forwarded-For`-handling → IP-limieten zinloos achter Railway/Hetzner-LB. | A04 | Medium |
| 5 | **CSV/formula-injectie** in exports: `_csv_value` neutraliseert geen leidende `= + - @` → code-executie op de machine van de operator via Excel/Sheets. | A03 | Medium |

**Overige (Medium/Low):** `url_safety` localhost-exceptie live in prod (A10); open redirect op `/c/{token}` (redirect buiten `if verified`, `main.py:6124-6176`); secrets-vault **fail-open naar plaintext** zonder `WARMR_MASTER_KEY` (A02); wachtwoordbeleid niet server-side afgedwongen (A07); rate-limit-key uit **ongeverifieerde** JWT-claims → gerichte quota-DoS (A01); `session_version` wordt bijgewerkt maar **nooit gecontroleerd** → force-logout ineffectief (A01); impersonation is full-write & niet-intrekbaar (A01); `test-send` omzeilt suppressie + caps → spam-relay (A04); `/docs` standaard aan (A05); verbose error-leakage (A05); `python-jose==3.3.0` onderhouden-arm met CVE's (A06).

**Positief:** service_role-key nooit naar frontend; API-keys 256-bit, sha256-gehasht, geïndexeerde hash-lookup, scoped, intrekbaar; geen SQLi; geen IDOR op bekeken routes; `.env`/`config.js` correct git-ignored.

---

## 11. GDPR / AVG-audit

**Score: 4/10.** Goede losse bouwstenen, maar faalt op de fundamenten die een *verkoopbare EU-verwerker* moet halen.

**Werkende controls (positief):** echte art.15-export (`GET /leads/{id}/export-gdpr`, HMAC-signed, over 5 tabellen); echte art.17 per-lead-purge (`_purge_lead_by_id` over 9 tabellen + suppressie-"grafsteen"); link-klik-uitschrijven synchroon + suppressie herbekeken vóór élke send; SMTP-wachtwoorden Fernet-encrypted; RLS op kernpaden; service+admin audit-tabellen.

**Kritieke gaten:**

- **(Critical) Geen afgedwongen bewaartermijn — en de gepubliceerde policy is onwaar.** Geen enkele scheduled retention-job; `compliance_overview` retourneert de hardcoded string *"Deleted … within 30 days of account closure"* (`main.py:4864`) en CLAUDE.md belooft 30-daagse verwijdering — **geen code implementeert dit**. `leads`/`reply_inbox`/`email_events`/`email_tracking`/`bounce_log` groeien eeuwig. Schendt art.5(1)(e) + is een onjuiste transparantie-verklaring (art.13/14). → `retention_engine.py` (launchd, dagelijks) + `retention_days` in `client_settings` + `closed_at` op `clients`.
- **(Critical) Tenant-verwijdering laat de meeste PII als wees achter.** `admin_delete_client` verwijdert slechts 7 tabellen (`main.py:3561`), **niet** `leads`, `reply_inbox`, `email_events`, `email_tracking`, `campaign_leads`, `enrichment_queue`, `suppression_list`, `notifications`, `crm_integrations`, `api_cost_log`. Door de ontbrekende cascade-FK's (§6.2) worden die rijen permanent wees. Art.17 wordt bij accountsluiting **niet** voltooid. → itereer alle `client_id`-tabellen, of echte cascade-FK's.
- **(Critical) Reply-gebaseerd uitschrijven wordt genegeerd.** Een prospect die "uitschrijven" **antwoordt** wordt door `reply_classifier.py:67-72` correct gelabeld en `imap_processor.py:768-774` zet `leads.status='unsubscribed'` — maar **voegt niet toe aan `suppression_list` en annuleert geen pending sends**. `is_suppressed()` checkt alleen `suppression_list`, nooit `leads.status` → de persoon blijft mail krijgen via andere campagnes. → hergebruik het suppress+cancel-blok uit `process_unsubscribe` (`main.py:6005-6030`) in `imap_processor`.
- **(High) Plaintext reply-bodies + IPs, in DB én in ongeroteerde logs.** `reply_inbox.body`, lead-`phone`/`linkedin_url`, `email_tracking.ip_address`+`user_agent` staan onversleuteld; alleen SMTP-wachtwoorden zijn vault-encrypted. Prospect-e-mails worden ook naar `logs/*.err.log` geschreven **zonder rotatie** (live: `daily-reset.err.log` = 21 MB, echte adressen zichtbaar) — buiten RLS, buiten erasure. → encrypt reply-bodies (pgcrypto/Fernet); truncate/hash IP; masker e-mails in logs + rotatie; schrap excess `phone`/`linkedin`.
- **(High) Geen EU-data-residency-pin + ongeregelde US-sub-processors.** Supabase-regio is niet vastgezet/gevalideerd; PII stroomt naar Anthropic, Resend, Clearbit, Apify (US) en Hunter.io zonder DPA/sub-processor-register/zero-retention-config. Chapter V-transfer-risico; deal-blocker bij due diligence. → pin Supabase EU (Frankfurt), boot-check op regio, `SUBPROCESSORS.md`, DPA's, per-client opt-in voor enrichment/AI.
- **(Medium-High) Audit logt geen PII-toegang/-export; backups ongedefinieerd.** Export/erase/CSV-endpoints schrijven geen auditrij; service-reads zijn 1%-gesampled; geen backup-retentiebeleid → erasure niet bewijsbaar tegen snapshots.

---

## 12. Deliverability-audit (auth/DNS)

Gecheckt vs. afwezig (`api/dns_check.py` + `dns_monitor.py`):

| Signaal | Status |
|---|---|
| SPF | ✅ presence + drift (niet `~all` vs `-all`-validiteit) |
| DKIM | ✅ selector-based presence |
| DMARC | ✅ presence + fase |
| MX | ⚠️ functie bestaat maar **drift-monitor roept 'm niet aan** |
| DNSBL blacklist | ✅ 5 zones, reverse-IP |
| rDNS/PTR | ❌ afwezig |
| ARC | ❌ afwezig |
| BIMI/VMC | ❌ afwezig |
| HELO/EHLO-consistentie | ❌ afwezig |
| MTA-STS / TLS-RPT / DMARC-alignment | ❌ afwezig |

Kern: er is **geen preflight-gate** — een misgeconfigureerd domein warmt op en verbrandt reputatie (§2.4). → harde preflight + rDNS/MTA-STS + monitor-falen gekoppeld aan auto-pause.

---

## 13. Observability

**Score: 3/10.** Degelijke *business-event*-alerting (`diagnostics_engine` → `notifications` → Resend, 15-min-throttle; bounce>3% → urgent + auto-pause). Bijna nul *operationele* observability: JSON-logging **standaard uit** (`WARMR_JSON_LOGS=1`), correlatie-ID's alleen binnen het FastAPI-request (de batch-engines delen geen trace-context), `/metrics` exporteert **alleen HTTP-tellers** (geen mailbox-count, warmup-week, bounce/spam/reply %, queue-depth), geen tracing, geen ops-dashboard, geen infra-alerting (proces-down, 5xx-surge, queue-backlog), en **PII in ongeroteerde logs** (§11).

### Ontworpen realtime ops-dashboard (paneel → exacte bron)

| Paneel | Bron (query/metric) |
|---|---|
| Actieve mailboxen (per status) | `SELECT status, count(*) FROM inboxes GROUP BY status` |
| Warming-verdeling per week | `SELECT date_part('week', age(now(), warmup_start_date)), count(*) FROM inboxes WHERE warmup_active GROUP BY 1` |
| Reputatie-verdeling/gem. | `SELECT reputation_score FROM inboxes` (bucket) |
| Send-failures/SMTP-errors 1h/24h | `warmup_logs WHERE action='error'` + `notifications WHERE type IN ('smtp_failure','inbox_bounce_pause')` |
| Spam % (landed vs rescued) | `warmup_logs.landed_in_spam` / `action='spam_rescued'` |
| Bounce % (rollend 7d) | spiegel `bounce_handler.check_inbox_bounce_rate` op `email_events` |
| Inbox-placement-ratio | `placement_test_results` ⋈ `placement_tests` per provider |
| Reply-ratio | `email_events`: `replied/sent` |
| Queue-health | `enrichment_queue` per status + `campaign_leads WHERE status='active' AND next_send_at<now` |
| API-health (RPS/p95/5xx) | Prometheus `warmr_requests_total`, `…_duration_seconds`, `…_errors_total` |
| Open alerts | `notifications WHERE read=false ORDER BY priority` |
| Claude-kosten/budget | `api_cost_log` sum vs `DAILY_API_BUDGET_EUR` |
| Worker-liveness | `service_query_log.worker_name` + laatste `created_at`, flag indien stale |

**Bouw:** een kleine Prometheus-**exporter**-job die deze aggregaties publiceert als gauges (`warmr_inboxes_by_status`, `warmr_bounce_rate`, `warmr_enrichment_queue_depth`), dan één Grafana.

---

## 14. Kostenoptimalisatie

**Score: 2/10.** Kostenmodel (per 1.000 mailboxen/dag, steady state; en geëxtrapoleerd naar 100k):

| Resource | Per 1.000 mbx/dag | Bij 100k mbx/dag | Grootste verspilling |
|---|---|---|---|
| Claude Haiku-calls | ~76.000 | ~7,6M | **verse generatie per e-mail én reply**, geen templating (`warmup_engine.py:425`, `imap_processor.py:397,1101`) |
| LLM-€ | ~€67 | ~€6.700 (~**€200k/maand**) | **warmup stopt nooit** voor "ready" (`auto_promote.py:156` laat `warmup_active=True`) → eeuwig |
| Budget-check DB-reads | 76k scans van een groeiende tabel | miljarden row-reads | **`get_daily_spend` full-scant `api_cost_log` vóór élke call** (`cost_tracker.py:79-88`) — O(N²), de ergste defect |
| DB-writes | ~200k+ | ~20M | no-op `update_warmup_target` elke run (`:554`), per-e-mail log/token-writes |
| IMAP-logins | ~432.000 | ~43M | 3 logins/inbox/run, geen pooling (§7) |
| SMTP-sessies | ~50k+ | ~5M | nieuwe login per bericht |

**Wanneer moet warming auto-stoppen?** Nu **nooit** bij succes — alleen bij falen (complaint/rep<35/handmatig). → na "ready" naar een **maintenance-modus**: paar getemplatede mails/week, **nul LLM**, uiteindelijk retire. **Quick wins met de hoogste besparing:** (a) `get_daily_spend` → SQL `sum()` of running counter; (b) template-bank per taal + spintax (`spintax_engine.py` bestaat al), reserveer live-LLM voor een klein %; (c) budget-enforcement degradeert naar templates i.p.v. `BudgetExceededError` te gooien (die nu de warmup-send afbreekt); (d) event-driven pollers (LISTEN/NOTIFY) zodat lege tabellen niets kosten.

---

## 15. UX-audit

**Score: 6/10** (verrassend volwassen). Wat de gebruiker al ziet: gem. reputatie-statkaart, per-inbox health-grid met **reputatie-sparkline** en statusbadges (`dashboard.html:552-587`), een **"Klaar over ~X dagen"-forecast** (lineaire extrapolatie, `main.py:1231-1244`), en een **week-1→5 tijdlijn** met current/completed-states (`inboxes.html:102-122,433-437`).

**Gaten:**
- **Geen readiness-breakdown:** de gebruiker ziet niet *waarom* een inbox nog niet ready is (welke van de 6 criteria faalt) — `check_and_promote_inbox` retourneert die reden al, maar de UI toont 'm niet.
- **Geen ACTIVE/Heater-status:** READY vs "in gebruik door Heater" is niet zichtbaar (bestaat ook niet in het model — §3).
- **Pause-redenen niet gesurfaced:** SMTP-hik vs verbrande inbox zien er identiek uit.
- **Geen queue-/failure-inzicht** voor de eindgebruiker; forecast is naïef lineair (kan negatieve/absurde ETA's geven).
- **Veiligheid raakt UX:** ~150 `innerHTML`-sinks; de XSS (§10) leeft in de unified-inbox.

**Voorgesteld duidelijker dashboard:** één "Readiness-score"-kaart per inbox met de 6 criteria als checklist (✓/✗ + huidige waarde vs drempel), een eerlijke ETA (op basis van de trap-curve, niet lineair), expliciete status+reden-badges (WARMING/ALMOST_READY/READY/ACTIVE/PAUSED[reden]/RETIRED), en een placement-resultaat-widget (echte inbox/spam per provider) i.p.v. de fictieve reputatiescore.

---

## 16. AI-verbeteringen

Warmr gebruikt Claude al breed (Haiku voor warmup-content/replies/classificatie, Sonnet voor zwaarder werk). Hoogwaardige uitbreidingen:

1. **Adaptive warming** — vervang de trapfunctie (§2) door een RL/bandit-controller die volume/timing/reply-ratio per inbox stuurt op basis van *echte* placement- en bounce-feedback; injecteert de per-inbox-ruis die detectie voorkomt.
2. **Anomaly-detection** — model op `warmup_logs`/`email_events` dat reputatie-drift, plotselinge spam-landing of bounce-spikes vóór de drempel signaleert (nu pas hard-coded thresholds in `diagnostics_engine`).
3. **Spam/placement-voorspelling** — combineer `content_scorer` + historische placement om per e-mail een inbox-kans te voorspellen vóór verzenden.
4. **Reputatie-voorspelling** — vervang de lineaire ETA (§15) door een tijdreeksmodel dat "dagen tot READY" schat met onzekerheidsband.
5. **Optimale verzendtijden** — `send_time_optimizer.py` bestaat al; voed het met echte open/reply-tijden per segment i.p.v. globale heuristiek.
6. **Kosten:** al deze modellen draaien op geaggregeerde data (goedkoop), en verlagen juist de LLM-per-mail-kosten (§14) door generatie te vervangen door selectie uit een template-bank.

---

## 17. Roadmap (op impact)

### Critical — nu, vóór enige opschaling
1. **RLS aanzetten op de 8 lekkende tabellen** + `REVOKE` anon/authenticated (§6.1).
2. **Zero-click XSS fixen** (escape alle interpolaties, drop CSP `unsafe-inline`) (§10-1).
3. **Eén scheduler + advisory-lock**; `daily_reset` datum-guard; atomaire send-claim + idempotency-key (§8).
4. **Budget-check → SQL `sum()`** en **template-bank + maintenance-modus** (stopt de €200k/mnd-koers) (§14).
5. **Promotie-trigger bedraden + `inbox.warmup_complete` emitten + conditionele status-updates** (§4).
6. **GDPR-fundamenten:** reply-unsubscribe → suppressie; `admin_delete_client` compleet maken; `retention_engine.py` (§11).
7. **Cascade-FK's echt maken** (client_id→UUID) (§6.2).

### High Impact
- Indexen op de 3 hot-tabellen + tenant-kolommen (§6.3); SSRF-guard op CRM-pad + fail-closed op `WARMR_API_TOKEN`/`WARMR_MASTER_KEY` (§10); threading-headers + DNS-preflight-gate op warmup (§2); reputatie herbaseren op Postmaster/SNDS + placement (§2.3); N+1 in `process_lead` opruimen + IMAP-sessie-hergebruik (§7); business-metrics-exporter + ops-dashboard (§13); `CHECK`-constraints + Supabase-CLI-migraties (§6.5); EU-residency + DPA's/sub-processor-register (§11).

### Medium (schaalbaarheid)
- `main.py` opsplitsen in routers + repository-laag (§1); partitionering + `TIMESTAMPTZ` + retentie-drop (§6.4); enrichment/webhook backoff + DLQ (§9); proxy-header-config + server-side brute-force-lockout (§10); async worker-model geshard per inbox (§7); lease-based Heater-integratie (claim/release/usage/report) (§4).

### Nice to Have (innovatie)
- Adaptive-warming-controller, anomaly-detection, placement-voorspelling (§16); read-only impersonation + `session_version`-enforcement; hash-chained append-only audit; UI readiness-breakdown + eerlijke ETA (§15); provider-push (Gmail Pub/Sub, MS Graph) i.p.v. IMAP-poll (§7).

---

## 18. Eindscore

| Dimensie | Score | Kern-onderbouwing |
|---|---:|---|
| Architectuur | 4/10 | Platte structuur, God-object `main.py`, geen lagen/DDD, schema-drift; wél modulaire engines + outbox-patroon aanwezig |
| Security | 5/10 | Sterk fundament (JWT/RLS/secrets), maar zero-click XSS + SSRF + fallback-HMAC |
| GDPR | 4/10 | Echte export/erase, maar geen retentie, wees-PII bij delete, reply-unsubscribe genegeerd, US-transfers ongeregeld |
| Performance | 3/10 | N+1 per e-mail, `count(exact)` in hot loops, login-per-bericht, O(mailboxen) IMAP-poll |
| Deliverability | 4/10 | Goede DNS/placement-tooling, maar nep-threads, gesloten Gmail-loop, fictieve reputatie, geen preflight |
| UX | 6/10 | Volwassen (sparklines, week-tijdlijn, ETA), maar geen readiness-breakdown, XSS-oppervlak |
| Monitoring | 3/10 | Business-alerting oké; geen business-metrics/tracing/ops-dashboard/infra-alerts; PII in logs |
| Schaalbaarheid | 3/10 | 3 hot-tabellen zonder index, geen partitionering, single-process poll-everything |
| Onderhoudbaarheid | 4/10 | Flat + God-object + 3 schedulers + schema-drift; wél tests + conventies |
| Testbaarheid | 5/10 | 80+ unit + live RLS-integratietests + pure functies; maar strakke DB-coupling |
| Kosten | 2/10 | Verse LLM per e-mail, O(N²) budget-check, warmup stopt nooit, write-amplificatie |
| Enterprise Readiness | 3/10 | Meerdere Criticals blokkeren productie-multi-tenant op schaal |

### **Totaal: 3,9 / 10**

**Waarom:** Warmr is een indrukwekkend *breed* MVP met echte kwaliteit in de details (auth, RLS-kern, webhook-plumbing, GDPR-endpoints, placement-tests). Maar het is gebouwd als een **single-tenant, single-process, poll-alles**-systeem met **veiligheids- en kostenlekken** en een **kern-belofte (Heater-overdracht) die niet functioneert**. De afstand tot "beste enterprise warmup-platform op de markt" wordt niet bepaald door ontbrekende features — die zijn er in overvloed — maar door de **fundamenten**: isolatie die aan de randen lekt, een warmup die filters niet misleidt, kosten die kwadratisch schalen, en een lifecycle die niet automatisch draait. Fix de zeven Criticals in §17 en de reële score springt richting 6–7; pak daarna High/Medium en 8+ is haalbaar.

---

*Onderbouwing per punt: alle `file:line`-verwijzingen zijn geverifieerd tegen de codebase op 2026-07-09. Deze audit vervangt de "grotendeels groene" `WARMR_AUDIT.md` (apr 2026) waar die conclusies door nieuwe bevindingen worden tegengesproken (o.a. RLS-dekking, cascade-FK's, promotie-trigger, XSS).*
