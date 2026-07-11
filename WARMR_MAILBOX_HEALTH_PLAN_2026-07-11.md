# Warmr — Actieplan Mailbox Health Detection

**Datum:** 2026-07-11
**Bron:** kritische beoordeling van het ready/reputatie-model (zie diagnose hieronder)
**Doel:** van een zelfreferentiële score met dode einden naar een extern-gemeten, zelf-herstellend mailbox-gezondheidsmodel — en per direct de verzendcapaciteit terug (0 ready inboxen vandaag).

---

## 1. Diagnose (samenvatting — alles file:line-geverifieerd)

| # | Doodzonde | Bewijs |
|---|---|---|
| D1 | **Score meet zichzelf**: alle positieve deltas komen uit het eigen warmup-netwerk; `sent` geeft +0.2 (kilometerteller); placement_tester en dns_monitor voeden score noch status; beide productie-inboxen gesatureerd op 100 | `imap_processor.py:65-73` · `warmup_engine.py:302-321` |
| D2 | **Spam-plaatsing wordt beloond**: `spam_rescued +1.0` > `received +0.5`, bijgeschreven op de verzender — het sterkste negatieve signaal telt als grootste plus | `imap_processor.py:67-68` |
| D3 | **Lifecycle-fuik**: promotie alleen via handmatig endpoint (0 automatische callers); `auto_pause_count_24h` heeft géén reset-writer (naam liegt — cumulatief voor eeuwig) terwijl promotie `== 0` eist; auto-resume werkt aantoonbaar niet (reset_at 27 mei verstreken, nog steeds paused) | `api/main.py:581` · `auto_promote.py:12,62` · `daily_reset.py:66-69` · `diagnostics_engine.py:560-570,634` |
| D4 | **Geen geheugen/vangrails op laag volume**: bounce-breaker eist ≥30 sends/7d (vuurt nooit tijdens warmup); geen decay; geen status-audit-trail (pauze-oorzaak onherleidbaar); score-updates slikken fouten | `bounce_handler.py:326` · `imap_processor.py:255-257` |

**Live-staat:** 2 inboxen (info@/contact@meet-aerys.nl), rep=100, `status=paused`, `auto_pause_count_24h=3`, `auto_pause_reset_at=2026-05-27` (6 weken verstreken). Notes tonen géén auto-pause-reden → oorzaak onbekend, onherleidbaar zonder log.

---

## 2. Doelmodel

**Gezondheid = extern gemeten, drie assen, nooit één getal:**

| As | Bron | Gewicht in besluit |
|---|---|---|
| **Technisch** | dns_monitor: SPF/DKIM/DMARC — harde gate, geen adviestekst | blokkerend (pass/fail) |
| **Plaatsing** | placement_tester: seed-tests (inbox vs spam %) | leidend voor promotie/degradatie |
| **Gedrag** | échte campagne-uitkomsten: bounces/complaints uit Heatr's suppressielijst + webhook-events (data bestaat sinds 2026-07-11); warmup-verkeer max klein gewicht | leidend voor pauze |

**State machine met uitgangen (elke transitie automatisch + gelogd):**

```
warmup ──(criteria + placement-test pass)──► probation ──(7d schoon, half volume)──► ready
ready ──(placement <60% óf complaint)──► degraded (volume halveren, extra tests)
degraded ──(2 tests goed)──► ready | ──(verder verval)──► paused
paused ──(reset_at verstreken)──► warmup (AUTOMATISCH — de bestaande dode code echt laten werken)
elke transitie → append-only inbox_status_log (from, to, reden, bron, timestamp)
```

**Teller-regel:** nooit meer cumulatieve tellers met een tijdsnaam. `auto_pause_count_24h` vervangen door tellen van `inbox_status_log`-rijen in een echt 24h-venster.

---

## 3. Fasering

| Fase | Onderwerp | Effect |
|---|---|---|
| W0 | Forensiek + deblokkade | Verzendcapaciteit vandaag terug |
| W1 | Loops aansluiten | Fuik dicht: promotie + resume draaien automatisch |
| W2 | Score-integriteit | D2 gefixt, decay, geen saturatie |
| W3 | Ground truth | Placement + DNS + echte campagnedata in het besluit |
| W4 | State machine + status-log | Probation/degraded, hysterese, audit-trail |
| W5 | Contract met Heatr | reputation/capacity als events i.p.v. stale cache |

---

## 4. Fase W0 — Forensiek + deblokkade (VANDAAG)

**PR W0-1 — inbox_status_log eerst** (anders is de volgende pauze wéér onherleidbaar):
```sql
CREATE TABLE IF NOT EXISTS inbox_status_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    inbox_id    UUID NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    reason      TEXT NOT NULL,      -- bounce_rate | critical_reputation | smtp_errors | manual | auto_resume | promotion
    source      TEXT NOT NULL,      -- bounce_handler | diagnostics | auto_promote | operator | ...
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_inbox_status_log_inbox ON inbox_status_log (inbox_id, created_at DESC);
```
Elke plek die vandaag `inboxes.status` schrijft (bounce_handler:337, diagnostics:256/633, auto_promote:154, API-endpoints) schrijft óók een log-rij. Grep-inventaris eerst: **alle** status-writers vinden — de huidige pauze kwam via een pad dat geen notes zet.

**PR W0-2 — deblokkade (handmatig, gedocumenteerd):**
1. Forensiek: warmup_logs + bounce_log + notifications van beide inboxen rond de pauze-datum uitlezen → oorzaak vaststellen en in de nieuwe log registreren als `reason='forensic_backfill'`.
2. DNS-check + placement-test EERST draaien (niet blind deblokkeren — misschien is paused terecht).
3. Bij groen: `status='warmup'`, `auto_pause_count_24h=0`, `auto_pause_reset_at=NULL` + log-rij `manual/deblokkade`.
4. **Niet direct naar ready** — laat de (nieuwe, W1) promotie-loop dat verdienen.

**Acceptatie W0:** beide inboxen uit de fuik, oorzaak gedocumenteerd, elke toekomstige statuswissel gelogd.

---

## 5. Fase W1 — Loops aansluiten

**PR W1-1 — promotie + resume in de diagnostics-tick** (draait al als service):
- elke tick: (a) `paused` + `auto_pause_reset_at <= now` → `warmup` (bestaande code activeren + fixen: uitzoeken waarom hij 6 weken niet vuurde — vermoedelijk caller/client_id-pad); (b) alle `warmup`-inboxen door `check_and_promote_inbox`.
- **Root-cause-plicht:** niet alleen opnieuw aanroepen — eerst bewijzen waarom de resume nooit liep (logging + test), anders komt de fuik terug.

**PR W1-2 — teller vervangen door venster:**
- `auto_pause_count_24h` deprecaten; promotiecriterium wordt: `0 pauzes in inbox_status_log in de laatste 7 dagen` (venster op timestamps, geen teller).
- daily_reset hoeft dan niets te resetten (de bug-klasse verdwijnt).

**Acceptatie W1:** een inbox die criteria haalt wordt binnen één tick ready zonder mensenhand; een verlopen pauze herstelt zichzelf; beide bewezen met tests én één live-cyclus.

---

## 6. Fase W2 — Score-integriteit

**PR W2-1 — deltas corrigeren:**
- `spam_rescued`: **−3.0** voor de verzender (plaatsing in spam is het signaal; de rescue is netwerk-mitigatie, geen verdienste) + aparte teller `spam_placements_7d`.
- `sent`: +0.2 → **0** (versturen is geen verdienste). `received`/`opened` blijven klein positief (warmup-functie), maar zie W3-gewicht.

**PR W2-2 — decay + anti-saturatie:**
- dagelijkse decay richting 50 (bv. 2%/dag zonder events): score wordt "recente gezondheid", niet "historische optelsom".
- soft-cap: boven 85 halveert elke positieve delta → 100 is haalbaar maar betekenisvol.

**Acceptatie W2:** score daalt aantoonbaar bij spam-plaatsing; een inactieve inbox zakt richting 50; scores van gezonde vs ongezonde inboxen zijn onderscheidbaar.

---

## 7. Fase W3 — Ground truth in het besluit

**PR W3-1 — placement_tester aansluiten:**
- wekelijkse (warmup: 2×/week) seed-test per inbox; resultaat → `placement_score` (inbox-% over laatste 3 tests) op de inbox-rij.
- promotiecriterium erbij: `placement_score ≥ 80`. Degradatiecriterium: `< 60`.

**PR W3-2 — dns_monitor blokkerend:**
- SPF/DKIM/DMARC-fail → `degraded` + notificatie (nu: alleen adviestekst). Herstel → automatisch terug.

**PR W3-3 — echte campagnedata (de brug met Heatr):**
- bounce/complaint-rates uit `heatr_suppressions` (hard_bounce per inbox/domein, bestaat sinds fase 2 Heatr) en Heatr's webhook-eventledger meenemen; drempels met minimum-sample per bron, en de 30-sends-eis vervangen door gecombineerd venster (warmup + campagne).

**Beslispunt B1 (vóór W3):** gewichtsverdeling placement/gedrag/warmup — voorstel 50/35/15.

---

## 8. Fase W4 — State machine + hysterese

- `probation` (na promotie: 7 dagen op 50% volume, dan pas vol) en `degraded` (halve capaciteit + extra tests) toevoegen; alle transities uit §2, elk via één `transition()`-functie die valideert + logt (zelfde patroon als Heatr's enrollment-closure).
- Hysterese: promotie-drempels ≠ degradatie-drempels (bv. promoot bij placement ≥80, degradeer pas <60) — geen flapperen.
- `get_ready_inboxes` (wat Heatr ziet) blijft `status='ready'`; `probation` telt mee met capaciteits-cap.

---

## 9. Fase W5 — Contract met Heatr

- Events `inbox.reputation_changed`, `inbox.status_changed`, `inbox.capacity` naar Heatr's webhook (die heeft sinds PR 10 een eventledger met dedup) — vervangt Heatr's stale `system_state`-cache in SendingGuard.
- Warmr's capacity-event maakt eindelijk Heatr's domein-cap mogelijk (bewust uitgeschakeld in Heatr fase 3 PR 11 — "wacht op Warmr capacity-events").

---

## 10. PR-volgorde

| PR | Onderwerp | Afhankelijkheid |
|---|---|---|
| W0-1 | inbox_status_log + alle writers loggen | geen |
| W0-2 | Forensiek + deblokkade 2 inboxen | W0-1 |
| W1-1 | Promotie + resume in diagnostics-tick (met root-cause resume-bug) | W0-1 |
| W1-2 | Teller → 7d-venster op status-log | W0-1, W1-1 |
| W2-1 | Delta-fix (spam_rescued negatief, sent 0) | geen |
| W2-2 | Decay + soft-cap | W2-1 |
| W3-1 | Placement in criteria | W1-1 |
| W3-2 | DNS blokkerend | W0-1 |
| W3-3 | Campagnedata uit Heatr-tabellen | Heatr-deploy (staat live) |
| W4 | Probation/degraded + transition() | W0-1 t/m W3 |
| W5 | Events naar Heatr | W4 |

---

## 11. Tests, metrics, DoD

**Tests per PR:** unit (criteria/deltas/vensters), transitie-tests (elke pijl uit §2 + verboden sprongen), regressietest op de fuik (inbox met oude pauze → komt er automatisch uit), en de resume-bug krijgt een test die de 6-weken-situatie naspeelt.

**Metrics/alerts:** `inbox_status_transitions_total` per reason · `ready_inbox_count` (alert bij 0 — dít had 6 weken geleden moeten afgaan) · `placement_score` per inbox · `stale_paused_count` (paused met verstreken reset_at > 1h = bug-alarm) · pauze zonder log-rij = alarm.

**Definition of done per fase:** migratie gedraaid · tests groen · de oorspronkelijke doodzonde niet meer reproduceerbaar · runbook bijgewerkt · minimaal één volledige live-cyclus geobserveerd (W1: échte auto-promotie; W3: échte placement-test in het besluit).

---

## 12. Eerste werkpakket — "Capaciteit terug" (W0 + W1)

Klein, geen score-wijzigingen, geen gedragsverandering voor verzonden campagnes:
1. inbox_status_log + writers (W0-1)
2. forensiek + gedocumenteerde deblokkade van beide inboxen (W0-2)
3. resume + promotie automatisch, met root-cause van de dode resume (W1-1)
4. teller → venster (W1-2)
5. alert op `ready_inbox_count == 0` en `stale_paused_count > 0`

**Succescriteria:** beide inboxen doorlopen warmup → (criteria) → ready zonder handmatige stap; de fuik is met een test onmogelijk gemaakt; elke statuswissel is herleidbaar.

---

*De belangrijkste discipline, geleend van de Heatr-recovery: eerst de fuik en het geheugen (W0/W1), dan pas de intelligentie (W2/W3). Een slimmere score bovenop een lifecycle zonder uitgangen lost niets op.*
