# Warmr — Enterprise Audit v2 (adversarieel, Q3 2026)

**Datum:** 2026-07-10
**Opdracht:** onafhankelijke tweede audit die audit v1 (`WARMR_ENTERPRISE_AUDIT_2026-07.md`) actief probeert te **weerleggen**. Alleen runtime-code als bewijs; geen docs/comments/schemabestanden. Elke conclusie `file:line` + `[CONFIRMED]` / `[REVIEW]`.
**Methode:** zeven onafhankelijke adversariële analyses (deliverability, scheduler/queue, multi-tenancy, security/OWASP, GDPR, performance/DB, architectuur+Heater+observability+AI), elk met de opdracht v1 onderuit te halen en de *reeds toegepaste* fixes op regressies te controleren.

> **Belangrijk t.o.v. v1:** de working tree is sinds v1 gewijzigd. De **Critical-5 (scheduler)** en **Critical-6 (kosten)** patches zijn **toegepast**, maar hun migratie `api/critical5_critical6_migration.sql` is **niet gedraaid** (untracked, geen migratie-runner). De **Critical-1 (RLS)** migratie is als bestand geschreven maar **niet gedraaid**. Dit ene feit bepaalt een groot deel van v2.

---

## 1. Management summary

### Was audit v1 fundamenteel correct?

**Ja op hoofdlijnen correct — maar onvolledig, en gevaarlijk in de remedies.**

Onder adversariële hertoetsing overleeft vrijwel elke *feitelijke bevinding* van v1 (grotendeels `[CONFIRMED]`). Maar drie dingen kloppen niet met het optimistische beeld dat de fix-fase suggereerde:

1. **De toegepaste remedies regresseren de productie.** De C5/C6-code gaat ervan uit dat de migratie gedraaid is. Dat is niet zo. Gevolg *vandaag*: `job_lock` faalt **open** (de RPC bestaat niet → geen enkele cross-process-lock), `increment_daily_sent` valt terug op non-atomaire read-modify-write (**dagcap te omzeilen**), en `daily_reset` schrijft naar een niet-bestaande kolom `last_reset_date` → **gooit elke run en reset niets** (de oude onvoorwaardelijke reset wérkte tenminste). De atomaire send-claim (het enige dat wél werkt zonder migratie) introduceert een **nieuwe** regressie: een lead kan voor eeuwig in `status='sending'` stranden — er is **geen reaper** — met stille onderlevering en een duplicaat bij handmatig herstel.
2. **De kosten-fix schaadde deliverability.** C6 verving per-mail LLM-content door een **template-bank van 3 skeletten per taal**. Voor kosten correct; voor detectie een **regressie** — een 3-bucket SimHash-fingerprint gedeeld over alle tenants is een sterker, statischer signaal dan N unieke LLM-bodies.
3. **v1 miste echte cross-tenant bugs én overschatte/onderschatte severities.** Nieuw gevonden: twee cross-tenant bugs in het bounce-pad die isolatie **omzeilen ongeacht RLS**; een art.17 per-lead-purge die v1 als "werkend" crediteerde maar **stil drie tabellen overslaat**; en verkeerd gewogen severities (fallback-HMAC te hoog, SSRF te laag, kosten €200k/mnd 2-4× te hoog).

Kortom: v1's kaart van het terrein klopt; v1's ingrepen zijn deels onvolledig uitgerold (migratie ontbreekt) en op twee punten contraproductief (templates, stranded sends). Vertrouw v1's *diagnose*, herzie v1's *remedie-uitrol*.

### De vijf grootste enterprise-risico's

1. **[Security] Zero-click stored XSS — nog steeds live, breder dan v1 (4 innerHTML-sinks).** Elke vreemde die een warmup/campagne-inbox mailt, voert JS uit in de geauthenticeerde (mogelijk admin-) sessie van de operator; CSP `unsafe-inline` neutraliseert de mitigatie; token in localStorage → volledige account- en cross-tenant-overname. `[CONFIRMED]`
2. **[Tenancy] 8 RLS-loze tabellen bereikbaar via de PUBLIEKE anon-key.** De hardening-migratie is niet gedraaid → iedereen met de (in elke browser meegeleverde) anon-key kan `curl …/rest/v1/unsubscribe_tokens?select=*` en alle tenants' bearer-tokens + `lead_email` + `webhook_events`-PII lezen/schrijven. `[CONFIRMED nothing applies it / REVIEW prod-state]`
3. **[GDPR] Je kunt de beloofde verwijdering niet nakomen.** `compliance_overview` belooft 30-daagse verwijdering; `admin_delete_client` laat `leads` + alle `reply_inbox`-bodies staan en kan door een filter op een niet-bestaande kolom zelfs `inboxes`/`clients` intact laten; de per-lead-purge slaat `email_events`/`campaign_leads`/`bounce_log` stil over maar retourneert `{"purged":true}`. EU-verkoopblokker. `[CONFIRMED]`
4. **[Scheduler] De toegepaste scheduler-fix regresseerde productie** (migratie un-run): geen lock, cap te omzeilen, `daily_reset` reset niets, plus de stranded-`sending`-lead zonder reaper. `[CONFIRMED]`
5. **[Deliverability] De warmup misleidt een 2026-filter waarschijnlijk niet — en de kosten-fix maakte het erger.** Geen threading-headers, gesloten all-Gmail-loop, interne-fictie-reputatie zonder Postmaster/SNDS, placement gemeten-maar-genegeerd, weekend/feestdag-verzending, plus de nieuwe 3-skelet-template-fingerprint. `[CONFIRMED]`

---

## 2. Validatie van audit v1 (per claim)

`[CONFIRMED]` = herbewezen uit code · `[REFUTED]` = onjuist/onterecht gescoord · `[REVIEW]` = niet volledig verifieerbaar (bv. prod-DB-status).

| v1-claim | v2-oordeel | Bewijs |
|---|---|---|
| 8 tenant-tabellen zonder RLS → lek | **[CONFIRMED]** (+ 9e: `job_locks`) | `full_schema.sql:322,343,392,410,486,521,537,906`; nieuw `critical5_critical6_migration.sql:78` |
| "geen policy heeft WITH CHECK" → schrijf-escape | **[REFUTED als vuln]** | `FOR ALL USING` hergebruikt USING als WITH CHECK → writes wél begrensd; niet scoren |
| RLS beschermt elke ingelogde tenant | **[REFUTED / gereframed]** | App gebruikt anon-key **alleen voor auth** (`app.js:5,19`), backend = service_role (`main.py:94`), JWT wordt nooit naar PostgREST doorgegeven → RLS is **moot voor het app-pad**; lek geldt alleen de 8 tabellen via directe anon-PostgREST |
| Cascade-FK's falen stil (TEXT↔UUID) | **[CONFIRMED]** | `tenancy_hardening_migration.sql:11-117`, `EXCEPTION WHEN OTHERS THEN NULL`; `client_id TEXT` vs `clients.id UUID` |
| Zero-click stored XSS (Critical) | **[CONFIRMED, breder]** | 4 sinks: `app.js:139`, `unified-inbox.html:376,420`, `dashboard.html:682`; bron `/notifications/poll` `main.py:2777,2793`; CSP `unsafe-inline` `main.py:128` |
| JWT algorithm-confusion mogelijk | **[REFUTED — v1 zei al "niet mogelijk"; herbevestigd]** | `auth.py:61-92`, HS256 gebruikt secret niet de JWKS-key; `alg:none` → 401 |
| SSRF alleen CRM-pad (Medium) | **[CONFIRMED + erger → High]** | `crm_dispatcher.py:137` + `_verify_webhook_url` `main.py:5063` ongeguard + `skip_verification` bypass; `url_safety.py:46` localhost-exceptie |
| Fallback-HMAC-secret (High/conditioneel) | **[REFUTED voor deze deploy → Low]** | `.env` zet `WARMR_API_TOKEN` (64 tekens) → default niet actief; latent, niet live |
| Reputatie is interne fictie; placement niet teruggekoppeld | **[CONFIRMED]** | geen postmaster/snds/talos-API; `placement_test_results` alleen dashboard; `auto_promote` gate = interne score |
| Geen threading-headers op warmup | **[CONFIRMED]** | `warmup_engine.py:539-547`, `imap_processor.py:435-443` — geen Message-ID/In-Reply-To/References |
| `SEND_DAYS` dode config; weekends verzonden | **[CONFIRMED]** | 0 Python-reads; `warmup_engine.py:508-516` geen weekday-gate |
| Promotie-trigger heeft geen aanroeper | **[CONFIRMED, sterker]** | enige caller `main.py:594` (JWT-only) → Heatr (API-key) kan niet; geen cron/n8n/frontend |
| `inbox.warmup_complete` nooit ge-emit | **[CONFIRMED]** (+ `campaign.completed`, `lead.bounced`, `lead.unsubscribed` ook dood → 4/8) | geen emit-site; `emit_webhook_event` heeft 0 callers |
| Heater is lead-feeder; Warmr blijft sender | **[CONFIRMED]** | `campaign_scheduler.py:710`; `public_api` inbox-endpoints read-only; geen lease/claim/handover in code |
| Geen retentie; 30-dagen-belofte is onwaar | **[CONFIRMED, sterker]** | `main.py:4871` string; enige delete-by-age = `admin_audit_log_prune` (wist audit, niet PII) |
| `admin_delete_client` orphant PII | **[CONFIRMED, erger]** | filtert `warmup_logs`/`bounce_log` op niet-bestaande `client_id` → FK-abort kan `inboxes`/`clients` intact laten |
| Per-lead purge werkt over 9 tabellen (v1 positief) | **[REFUTED]** | `_purge_lead_by_id` `main.py:4925` zet `.eq(client_id)` op `email_events`/`campaign_leads`/`bounce_log` (geen client_id) → deletes gooien, geslikt, rijen blijven; retourneert tóch `{"purged":true}`; mist `crm_sync_log` |
| Reply-unsubscribe genegeerd | **[CONFIRMED]** | `imap_processor.py:766` zet alleen `leads.status`; `is_suppressed` email-only |
| Kosten ~€200k/mnd bij 100k | **[REFUTED — 2-4× te hoog]** | C6 sampled content 10%; replies bleven live LLM → ~€80k/mnd, gedomineerd door warmup-replies; + per-client €2/dag cap |
| 3 hot-tabellen zonder index; geen partitionering; TIMESTAMP zonder tz | **[CONFIRMED]** (+ gemengde naive/aware writers = correctheids-landmijn) | `full_schema.sql:91,110,130`; `warmup_engine.py:137` `utcnow()` vs `campaign_scheduler.py:443` aware |
| Triple-scheduler; `daily_reset` uurlijks; `main(client_id=)` TypeError | **[CONFIRMED / deels FIXED]** | TypeError gefixt (`main.py:1516`) → double-send nu **live**, alleen door claim gedekt; daily-reset nu `StartCalendarInterval` |
| Architectuur: God-object, geen lagen, 6-writer status | **[CONFIRMED]** | `main.py` 6482 r / 130 endpoints / 209 DB-calls; 6+ `inboxes.status`-writers last-write-wins |
| Observability 3/10 | **[CONFIRMED]** | geen Sentry/OTel; `/metrics` HTTP-only; correlatie alleen in API-tier; batch-engines = black box |

**Nieuwe bevindingen die v1 volledig miste** (details in de secties): (a) cross-tenant **write** in het bounce-pad; (b) cross-tenant **read** van `bounce_log`; (c) warmup verzendt exact 1 mail/invocatie → reëel plafond ~36/dag (week-5-doel 60 onbereikbaar); (d) `smtp_retry.py` bestaat maar de echte send-paden gebruiken het niet → 421 wordt als `bounced` gelogd; (e) tweede, parallelle webhook-pad in `funnel_engine.py:96`; (f) `tracked_claude_call` wordt door het duurste (Opus) pad omzeild → ongemeten spend; (g) stranded-`sending`-lead zonder reaper.

---

## 3. New critical findings

1. **[CONFIRMED · Critical · NEW] Cross-tenant WRITE in bounce-handler.** `bounce_handler.py:274` resolt de lead op **kaal e-mailadres** (`.eq("email", …)`, geen `client_id`) en zet vervolgens `leads.status` + `campaign_leads.status='bounced'`. Een bounce die binnenkomt op inbox van klant A kan de lead van **klant B** als bounced markeren en B's actieve campagne stoppen wanneer beide hetzelfde adres targeten (`info@`, `sales@` — alledaags in B2B). Omzeilt isolatie **ongeacht RLS**. `inbox["client_id"]` is beschikbaar op de call-site (`:432`) maar wordt niet doorgegeven. **Fix:** `client_id` doorgeven + `.eq("client_id", …)`.
2. **[CONFIRMED · High · NEW] Cross-tenant READ van bounce-historie.** `campaign_scheduler.py:262` `is_email_hard_bounced` negeert het meegegeven `client_id` en query't `bounce_log` op `lead_email` globaal → de bounce/complaint-historie van één tenant onderdrukt de sends van een ander. Conservatief (blokkeert, lekt geen inhoud) maar één tenant stuurt het gedrag van een ander.
3. **[CONFIRMED · Critical · regressie door fix] `daily_reset` reset niets in productie.** De C5-date-guard schrijft `last_reset_date` (`daily_reset.py:66`), een kolom die alleen in de un-run migratie bestaat → PostgREST weigert → geslikt → dagteller wordt nooit genulld. Deels gemaskeerd door `auto_reset_stale_counters`, maar het is een netto-regressie t.o.v. de werkende oude reset.
4. **[CONFIRMED · Critical · regressie door fix] Stranded `sending`-leads zonder reaper.** De atomaire claim (`campaign_scheduler.py:946`) zet `status='sending'`; crasht/lease-verloopt het proces vóór completion, dan blijft de lead voor eeuwig `sending` (geen reaper; grep bevestigt alleen een read-only count in `daily_briefing.py:137`) → stille onderlevering, en handmatig herstel her-verzendt (idempotency-index un-run).
5. **[CONFIRMED · Critical · verergerd] Per-lead art.17-purge slaat 3 tabellen stil over** en retourneert tóch success (zie §GDPR).
6. **[CONFIRMED · High · verergerd] SSRF-verificatie is zelf een bypassbare self-SSRF** (`main.py:5063` ongeguard + `skip_verification` `:5574` + PATCH-URL zonder verificatie `:5617`).

---

## 4. Deliverability — score 3/10 (v1: 4/10, **oneens, omlaag**)

Volledige pipeline getraced. Alle v1-defecten `[CONFIRMED]` en ongewijzigd in runtime: geen warmup-threading (`warmup_engine.py:539-547`; reply `imap_processor.py:435-443`), gesloten all-Gmail-mesh (`imap_processor.py:130,821` — live `.env` = 5 warmup-Gmails + 2 client-inboxen), reputatie zonder externe feeds, spam-rescue geeft `+1.0` (metric beweegt de verkeerde kant tijdens een echte spam-landing), placement gemeten-maar-genegeerd, stap-functie-volume + constante reply-ratio 0.35 + uniforme 1-30s cron-clustering, weekend/feestdag-sends, `SEND_DAYS` dood.

**Reden voor −1 t.o.v. v1:** de C6-template-bank (`warmup_templates.py:55-101`) maakt ~90% (100% voor ready-inboxen) van de warmup-bodies uit **3 skeletten per taal** — een sterker, statisch, cross-tenant fingerprint dan per-mail-LLM. De kosten-fix bewoog de warmup-engine de verkeerde kant op qua detecteerbaarheid.

**Nieuw:** warmup verzendt exact 1 mail per invocatie (`warmup_engine.py:645-725`) × ≤36 launchd-runs/dag → reëel plafond **~36/inbox/dag**; het week-5-doel van 60 is door cadans onbereikbaar (gedocumenteerd schema = fictie). `smtp_retry.py` bestaat maar `warmup_engine.send_via_smtp:545` en `campaign_scheduler.send_campaign_email:710` gebruiken het **niet** → een 421-throttle wordt op het campagne-pad als `bounced` gelogd (`:974`) → kan een gezonde campagne auto-pauzeren.

**Mailbox-burn tijdens Heater-gebruik:** er is **geen lease-concept**; alle drie de remmen (bounce>3%, rep<35, ≥3 SMTP-errors) blijven uit tijdens een echte spam-folder-burn (geen bounce, geen FBL, en self-rescue *verhoogt* de interne score). Niets stopt warmup of campagne op een stil verbrandende mailbox.

**Vs. Instantly/Smartlead/Lemlist/MailReach/Mailivery/Folderly/Warmbox (mechanisch):** commerciële warmup ontleent dekking aan schaal + provider-diversiteit + echte threaded engagement, en sluit de lus op gemeten placement. Warmr heeft een kleine, single-provider, gesloten mesh die orphan-(ongethreade)-berichten uit 3 skeletten uitwisselt op een vaste cron incl. weekends, met een zelf-referentiële reputatie en een gemeten-maar-genegeerd placement-signaal. Elke as die peer-network-warmup plausibel maakt, is de as waar Warmr het zwakst is.

**Verdict:** zou een 2026 Gmail/M365-filter niet betrouwbaar misleiden. Voor een 2-inbox low-volume deployment waarschijnlijk reputatie-neutraal op de Gmail-as; netto-negatief zodra het schaalt (fingerprint wordt slechter met meer tenants) of zodra niet-Gmail-placement telt.

---

## 5. Scheduler — score 4/10 tree, ~3/10 prod-vandaag (v1: 2/10, **deels oneens**)

Kernfeit: de C5-migratie is untracked/un-run; er is geen migratie-runner. Fix-voor-fix:
- **Atomaire claim — WERKT** `[CONFIRMED]`. postgrest 1.0.2 `update()` geeft standaard `returning=representation` → `claim.data` gevuld; de conditionele `UPDATE … WHERE status='active'` is Postgres-atomisch en migratie-onafhankelijk. Het enige echte double-send-slot vandaag. (Weerlegt de "niets verzendt"-zorg.)
- **`job_lock` — INERT** `[CONFIRMED]`. RPC ontbreekt → faalt open → geen lock in prod. TTL 900s begrenst stale (weerlegt "stale forever") maar is **korter dan een echte campagne-run** (30-180s × N leads) → lease verloopt mid-run → concurrente run steelt hem.
- **`daily_reset` — regressie** (zie §3).
- **`increment_daily_sent` — inert** → RMW → met open lock → lost updates → cap omzeild.
- **Idempotency-index — un-run** → geen backstop.

**Scenario's:** post-delivery SMTP-timeout → als `bounced` gevangen, claim terug naar `active` → her-verzonden = duplicaat, geen Message-ID-reconcile. Webhook-retry → geen lock/claim; n8n elke 60s + cycles >60s → dubbele levering (nonce vers per poging, dedupt niet). DB offline → `job_lock` faalt open (onveilig), atomaire claim faalt dicht (veilig).

**Oneens met v1's 2/10:** voor prod-vandaag ~3/10 verdedigbaar; v1 over-crediteerde lock/increment/index en **miste de nieuwe daily_reset-regressie**. Voor een correct gemigreerde tree ~6,5/10.

---

## 6. Queues — [CONFIRMED grotendeels ongewijzigd]

- **Send-queue (`campaign_leads`):** claim werkt (§5), maar **geen reaper** voor `sending` (§3-4). Retry op SMTP-fail revert naar `active` (goed) maar zonder backoff/attempt-cap → tight loop tegen een throttlende provider, en 421→`bounced` vervuilt de bounce-rate.
- **Webhook-dispatcher:** geen claim/lock; twee runners (standalone `while True` + n8n /60s) → dubbele levering; circuit breaker aanwezig (goed).
- **Enrichment-queue:** claimt atomisch (goed) + unieke index tegen dubbele rijen; maar failures → direct terug naar `pending` zonder `next_retry_at`/backoff → poison-message hamert; geen DLQ.
- **Tweede, verborgen "queue":** `funnel_engine.py:96` POST rechtstreeks naar `crm_integrations.webhook_url` (event `lead.stage_changed`, niet eens in `VALID_EVENTS`) — omzeilt de hele `webhook_events`→dispatcher-pijplijn (verborgen coupling).

---

## 7. Architecture — score 4/10 (v1: 4/10, **eens**)

`[CONFIRMED]` God-object `main.py` (6482 r, 130 endpoints, 209 DB-calls), platte structuur, 24 modules met eigen `create_client` (`public_api._sb()` zelfs per-request), geen domein/repo-laag. **Geen centrale state-machine:** ≥6 `inboxes.status`-writers last-write-wins (`auto_promote:154` **zonder** `status='warmup'`-predicaat, `bounce_handler:324`, `diagnostics_engine:255/566/632`, `warmup_engine:823`, `main.py:447/507/541`). `reputation_score`-writers: imap/bounce/warmup. **Race (C3-fix afwezig):** `auto_promote` leest→evalueert→schrijft `ready` onvoorwaardelijk (`:154`) → een pause die binnen het read-write-venster landt wordt overschreven; blast-radius deels gemaskeerd doordat pause ook `warmup_active=False`/`auto_pause_count` zet die de criteria toevallig checken. Geen enkele status-transitie is een SQL compare-and-swap.

---

## 8. Database — DB-scalability 3/10 (v1: 3/10, **eens**)

`[CONFIRMED]` nul indexen op `warmup_logs`/`sending_schedule`/`bounce_log` (hoogste volume); `inboxes.client_id` ongeïndexeerd, `leads.client_id` niet bruikbaar (2e kolom in `leads(email,client_id)`); `campaign_leads(status,next_send_at)` ongeïndexeerd terwijl `load_due_campaign_leads` daar exact op filtert; geen partitionering; **`TIMESTAMP` zonder tz met gemengde naive/aware writers** (`warmup_engine.py:137 utcnow()` vs `campaign_scheduler.py:443` aware) = correctheids-landmijn; UUIDv4-PK's op insert-heavy logs; **nul CHECK-constraints**. **Schema-drift `[CONFIRMED]`:** `leads.engagement_score`/`engagement_updated_at` en `clients.session_version` bestaan in **geen** SQL-bestand maar worden door runtime gelezen/geschreven (`engagement_scorer.py:55-64`, `auth.py:128`, `main.py:5114`) → bewijs dat schemabestanden ≠ productie. De un-run migratie voegt `job_locks`, RPC's, `last_reset_date`, `warmup_mode`, `email_events_sent_once_idx` toe — alle afwezig in prod.

---

## 9. Security — score 4/10 (v1: 5/10, **oneens, omlaag**)

Zie de gecorrigeerde OWASP-tabel. Kern: **zero-click stored XSS nog live** (A03, 4 sinks), **SSRF verergerd naar High** (CRM-create/verify/patch ongeguard + `skip_verification` + localhost-exceptie), **open redirect** `/c/{token}` (`main.py:6183` buiten `if verified`). **Weerleggingen van v1:** JWT algorithm-confusion niet mogelijk (herbevestigd); fallback-HMAC **Low** i.p.v. High (env zet de token); clickjacking géén gap (XFO DENY + `frame-ancestors 'none'`); pandas-RCE onjuist (geen path-traversal, geen code-exec). **De nieuwe C5/C6-code is schoon** — RPC's via gebonden params, geen SQLi/command-injection. Impersonation full-write + niet-intrekbaar (`session_version` bijgewerkt maar nooit vergeleken). Rate-limit op **ongeverifieerde** JWT-claims. CSV-formula-injectie ongewijzigd.

| OWASP | Bevinding | v1 sev | v2 sev |
|---|---|---|---|
| A03 XSS | zero-click stored, 4 sinks, CSP `unsafe-inline` | Critical | **Critical** (live) |
| A10 SSRF | CRM + bypassbare self-SSRF-verify + localhost | Med/High | **High** |
| A01 | impersonation full-write, niet-intrekbaar; `session_version` dood | High | **Med-High** |
| A01 | open redirect `/c/{token}?url=` | Medium | **Medium** |
| A03 | CSV/formula-injectie | Low/Med | **Low-Med** |
| A04 | rate-limit op unverified `sub`; geen XFF | Low | **Low-Med** |
| A02 | fallback-HMAC-secret | Critical | **Low** (env overschrijft) |
| A05 | `/docs` open; chunked-upload size-bypass; `unsafe-inline` | — | **Low** |
| A07 | JWT: geen confusion; jose 3.3.0 CVE's | Info | **Low** (dep) |
| A01 | clickjacking | — | **Geen** (weerlegd) |

Live-exploiteerbare posture zit onder het midden door de XSS → **4/10**. Stijgt naar 6-7 zodra de 4 sinks geëscaped zijn en `unsafe-inline` weg is.

---

## 10. GDPR — score 3/10 (v1: 4/10, **oneens, omlaag**)

`[CONFIRMED]` retentie is fictie (`main.py:4871`; enige delete-by-age wist de audit-log, niet PII). `admin_delete_client` orphant de primaire PII én kan door de niet-bestaande-`client_id`-filter een FK-abort veroorzaken die `inboxes`/`clients` intact laat. **[REFUTED v1-positief]** de per-lead-purge werkt niet: `.eq("client_id")` op `email_events`/`campaign_leads`/`bounce_log` (geen kolom) → deletes gooien, geslikt, rijen blijven; retourneert `{"purged":true}`; mist `crm_sync_log`. Reply-unsubscribe alleen als neveneffect van `stop_on_reply` en alleen binnen dezelfde campagne — **geen suppression-grafsteen** → her-import her-mailt. Plaintext reply-bodies + `phone`/`linkedin`/`ip_address`; **correctie v1:** SMTP-wachtwoorden staan in `.env` (niet DB-encrypted); wat conditioneel encrypted is, zijn CRM-keys + webhook-secrets. PII in ongeroteerde launchd-logs. Sub-processors (Anthropic/Resend/Clearbit/Apify=US, Hunter=EU) zonder regio-pin/validatie of DPA-register. Nul consent/legal-basis-records. Export/delete **niet** ge-audit. **Grootste EU-verkoopblokker: je kunt de contractueel beloofde verwijdering niet nakomen.**

---

## 11. Performance — 3/10 (v1: 3/10, **eens**); Kosten — 3/10 prod / 4/10 post-migratie (v1: 2/10, **omhoog**)

`process_lead` doet nu **~16-18** seriële round-trips (v1: 12-15; de claim voegde er één toe), `load_client_settings` nog steeds **2×** (`:856`,`:915`), `count_inbox_sends_last_hour` per lead → **ongeïndexeerde seq-scan van `warmup_logs` per lead** (ergste hot-loop). Budget-check: degradeert naar full-scan (niet gefixt in prod; RPC un-run), post-migratie SQL-`SUM` (O(K)/call, geen O(1)-counter). IMAP: exact 3 logins/inbox/run + O(N·M) SEARCHes → ~43M logins/dag bij 100k, past niet in het venster. SMTP: login per mail. Geen pooling; `job_lock` maakt een **tweede** client per job. Onbegrensde in-memory loads (`load_active_inboxes` `SELECT *` zonder `.limit()`).

**Kosten omhoog:** C6 landde 3 van 4 quick-wins — template-bank + 10%-sampling op content (~90% cut), maintenance-mode voor ready-inboxen (werkt pre-migratie via de `status=='ready'`-tak), budget→template-failover. **Weerlegt v1's €200k/mnd** → ~€80k/mnd, gedomineerd door **niet-getemplateerde warmup-replies** (`imap_processor.py:406`, ongesampled) + per-client €2/dag-cap. Vandaar Kosten 2→3 (prod) / 4 (post-migratie). Blijft van 5 af omdat replies 100% live LLM zijn en pollers poll-everything.

---

## 12. Observability — 3/10 (v1: 3/10, **eens**)

Geen Sentry, geen OTel/tracing, JSON-logging default uit, `/metrics` **HTTP-only** (geen business-metrics), correlatie-ID's alleen in de API-tier (batch-engines delen geen ID), PII in logs. Positief: `/health` doet echte Supabase/Claude/SMTP-checks (maar geen `/ready`). De omzet-kritische batch-tier (warmup/campaign/imap) is een **black box** — nul metrics.

---

## 13. AI — 5/10 (v1: geen aparte score)

Verspreide directe SDK-calls; `tracked_claude_call` is een dunne wrapper die **wordt omzeild** door `enrichment_engine`, `sequence_analyzer` en **heel `api/main.py`** — inclusief het duurste **Opus-4-7 deep-research-pad** (`main.py:851`) → **ongemeten spend**. Geen gateway, geen caching, geen retry/backoff, geen model-fallback-keten. Wél goede kosten-instincten in het warmup-pad (sampling + template-failover + per-call kostenlog). Modellen: Haiku 4.5 (warmup/classify/enrich), Sonnet 4.6 (score/suggest/briefing), Opus 4.7 (research). Fan-out na C6: week-5-inbox ~6 content-calls/dag (was ~60); ready-inboxen 0; **replies ongesampled = dominante warmup-LLM-kost.**

---

## 14. Heater-integratie — ownership 5/10

**Definitief:** Warmr is en blijft de **sender/eigenaar**; er is **geen lease/claim/handover-concept in code**. Heatr (API-key) kan alleen leads pushen (`public_api.py:331,580`) en capaciteit **lezen** (`:691,752`, read-only). De promotie-endpoint is **JWT-only** → Heatr is er structureel van uitgesloten. **4 van 8 gedeclareerde webhook-events zijn dood** (`inbox.warmup_complete`, `campaign.completed`, `lead.bounced`, `lead.unsubscribed` — nooit ge-emit); `emit_webhook_event` heeft 0 callers. Scenario's: ready-inbox blijft (bewust) maintenance-warmup sturen; "mailbox-burn tijdens Heater-lease" is een fictieve premisse (geen lease); `campaign.completed` wordt nooit ge-emit → Heatr weet nooit dat een campagne klaar is. **Verborgen coupling (nieuw):** `funnel_engine.py:96` = tweede webhook-pad buiten de dispatcher.

---

## 15. Future architecture (op basis van deze bevindingen)

- **Eén `transition_inbox(id, from_states, to, reason)` als enige status-writer** (SQL compare-and-swap) → dood de 6-writer last-write-wins en de promote/pause-race in één klap.
- **Outbox + één dispatcher + idempotency-key**, en verwijder het parallelle `funnel_engine`-webhook-pad → één event-ruggengraat; maak de 4 dode events echt (of schrap ze).
- **Repository-laag + één gedeelde client/pool** → verwijder 24× `create_client` en de per-request client; maak service-role-queries verplicht `client_id`-gefilterd (dicht de bounce-bugs by construction).
- **Migratie-discipline:** een echte runner + `schema_migrations` + CI die migratie én code samen uitrolt (de kern-les van v2: code zonder migratie = regressie).
- **Async worker-model geshard per inbox + IMAP IDLE/provider-push** i.p.v. poll-everything → verwijder de O(N·M) login-bom vóór N > enkele duizenden.
- **Reputatie-ingest (Postmaster/SNDS) + placement-feedback in de readiness-gate** → maak de score echt i.p.v. fictie.
- **Warmup: echte threading-headers, provider-diversiteit, per-inbox jitter/holiday-gate, en warmup-replies templaten/sampelen.**

---

## 16. Scores (v2 vs v1)

| Dimensie | v1 | v2 | Waarom het verschil |
|---|---:|---:|---|
| Architectuur | 4 | **4** | Ongewijzigd; God-object + geen state-machine bevestigd |
| Security | 5 | **4** | XSS nog live (4 sinks), SSRF verergerd; balans onder het midden |
| GDPR | 4 | **3** | Per-lead-purge blijkt kapot (v1-positief weerlegd); cascade + delete erger |
| Performance | 3 | **3** | C5/C6 was correctheid, niet perf; één hot-pad-round-trip erbij |
| Deliverability | 4 | **3** | C6-template-bank = sterker fingerprint (remedie schaadde) |
| UX | 6 | **5** | Niet diep her-geaudit; −1 want de operator-UI draagt de live XSS en de "Klaar over X dagen" leunt op promotie die nooit autonoom draait |
| Monitoring | 3 | **3** | Bevestigd; batch-tier black box |
| Scalability | 3 | **3** | DB-defecten ongewijzigd; migratie un-run |
| Maintainability | 4 | **4** | Ongewijzigd; + schema-drift bevestigd |
| Testability | 5 | **4** | 205/205 groen, maar de tests valideren mock-gedrag tegen een **un-run** migratie → vals vertrouwen (geen integratietest tegen echt schema); daily_reset-regressie ontsnapte |
| Kosten | 2 | **3** (4 post-migratie) | C6 landde 3/4 quick-wins; €200k/mnd was 2-4× te hoog |
| Enterprise Readiness | 3 | **3** | Meerdere Criticals live + fixes onvolledig/regressief uitgerold |
| — Heater-integratie | — | **5** | Ownership schoon, contract halfdood |
| — AI-infra | — | **5** | Goede kosten-instincten, wrapper omzeild, geen gateway/cache/retry |

**Totaal v2: ~3,6/10** (v1: 3,9/10). Het lagere cijfer weerspiegelt niet dat het systeem verslechterde op elke as, maar dat (a) v1-positieven bij hertoetsing kapot bleken (per-lead-purge), (b) een remedie deliverability schaadde, en (c) de toegepaste-maar-niet-gemigreerde fixes productie deels regresseren. Kosten stegen; deliverability/security/GDPR daalden.

---

## 17. Priority roadmap

**Onderscheid:** `HOTFIX` = klein, veilig, nu · `PATCH` = veilige codewijziging · `MIGRATIE` = architecturaal/DB, venster + verificatie.

### P0 — nu (live exploitatie / prod-regressie)
- **P0-A `MIGRATIE` (atomair!):** draai `critical5_critical6_migration.sql` **en** houd de reeds-toegepaste C5/C6-code — of rol de C5-code terug. **Deze twee MOETEN samen landen.** Nu draait code zonder migratie → `daily_reset` gooit, `job_lock` faalt open, cap omzeilbaar. Verifieer met de embedded query dat de objecten bestaan.
- **P0-B `PATCH` XSS:** escape de 4 innerHTML-sinks + `safe-dom.js`; verwijder `script-src 'unsafe-inline'`. (Frontend-only, geen migratie.)
- **P0-C `MIGRATIE` RLS:** draai `rls_hardening_migration.sql` (+ `job_locks`) en **verifieer**; tot dan zijn de 8+1 tabellen publiek via de anon-key.
- **P0-D `PATCH` bounce cross-tenant:** `client_id` doorgeven in `bounce_handler.py:274` + scopen in `campaign_scheduler.py:262`. (Nieuwe v2-bug.)
- **P0-E `PATCH` reaper:** een job die `campaign_leads.status='sending'` ouder dan X min terugzet naar `active` (met Message-ID-reconcile) — anders straft de atomaire claim onderlevering af met duplicaten.

### P1 — deze sprint (compliance / veiligheid)
- `PATCH` SSRF: `assert_url_safe` op CRM-create/verify/patch; verwijder `skip_verification` + localhost-exceptie.
- `PATCH` GDPR: `_purge_lead_by_id` niet meer op niet-bestaande `client_id` filteren (delete `email_events`/`campaign_leads`/`bounce_log` via `lead_id`; voeg `crm_sync_log` toe); `admin_delete_client` alle tabellen; reply-unsubscribe → suppression-grafsteen; **`compliance_overview` niet 30 dagen claimen tot de retentie-job draait.**
- `PATCH` open redirect binnen `if verified` + URL-allowlist; impersonation intrekbaar (`session_version` vergelijken) of read-only.
- `MIGRATIE` indexen: `warmup_logs(inbox_id,action,timestamp)` + `campaign_leads(status,next_send_at)` — verwijderen de twee dominante per-lead/per-inbox seq-scans.

### P2 — schaalbaarheid
- `MIGRATIE` (atomair): C3 promotie — conditionele `WHERE status='warmup'` + `inbox.warmup_complete`-emit + `webhook_events.idempotency_key` + **reply_rate-writer** (anders blokkeert het criterium alle promoties). Deze vier samen.
- `MIGRATIE` (venster, PITR eerst, atomair met RLS-policy-herschrijving): C4 `client_id` TEXT→UUID + child-FK-CASCADE. Herschrijft de P0-C-policies (`::text` weg) — die twee horen bij elkaar.
- `PATCH` warmup-realisme: threading-headers, provider-diversiteit, holiday/weekday-gate, warmup-replies templaten/sampelen; placement-feedback in de gate.
- `PATCH`/`MIGRATIE` centrale `transition_inbox` state-machine; verwijder het `funnel_engine`-parallelle webhook-pad.

### P3 — enterprise-hardening
- Async worker-model + IMAP IDLE/push; repository-laag + gedeelde pool; business-metrics-exporter + ops-dashboard; Sentry + OTel-tracing over de batch-engines; LLM-gateway (metering ook op het Opus-pad) + caching + retry; migratie-runner + CI-integratietests tegen een echt schema.

### Wat atomair samen MOET landen (om regressie te voorkomen)
1. **C5/C6-code ⇄ `critical5_critical6_migration.sql`** — de huidige split IS de regressie.
2. **C1 RLS-policies ⇄ C4 UUID-conversie** — de UUID-migratie herschrijft de `::text`-policies; los uitrollen breekt RLS of de conversie.
3. **C3 reply_rate-criterium ⇄ reply_rate-writer** — criterium zonder writer blokkeert alle promoties.
4. **Atomaire send-claim ⇄ reaper + idempotency-index** — claim zonder reaper = stille onderlevering; herstel zonder index = duplicaten.

---

*Alle `file:line`-verwijzingen geverifieerd tegen de working tree op 2026-07-10. Deze v2 bevestigt de diagnose van v1 grotendeels, weerlegt enkele severities en één v1-positief, en toont aan dat de reeds-toegepaste remedies onvolledig (migratie un-run) en op punten regressief/contraproductief zijn uitgerold.*
