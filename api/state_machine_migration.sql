-- ============================================================
-- Warmr — State Machine Migration (Fase 4b, architecture item 1 of 3)
-- NOT APPLIED YET — verification-first, see below.
--
-- Backs utils/state_machine.py + utils/state_machines_registry.py's
-- CAMPAIGN_LEAD_STATUS / INBOX_STATUS graphs, currently wired everywhere
-- they're written in LOG-ONLY mode (check_log_only() logs a warning on a
-- mismatch, never blocks the write — see campaign_scheduler.py,
-- funnel_engine.py, bounce_handler.py, utils/suppression.py,
-- auto_promote.py, diagnostics_engine.py, api/main.py).
--
-- Both graphs were reconstructed by directly reading every current write
-- site for these two columns (not from prior research) on 2026-07-15. Two
-- open questions surfaced that this migration cannot resolve without real
-- production data:
--   - campaign_leads.status = 'pending' is never written by any code path
--     found (both insert sites rely on the schema DEFAULT 'active'; every
--     other appearance is inside a defensive .in_("status", [...]) filter).
--   - inboxes.status = 'retired' is never written by any code path found
--     (only ever read via .neq("status", "retired") filters).
-- Both are plausible legacy/manual-intervention values, not proven-dead.
-- The CHECK constraints below are VALUE-only (allowed strings), not
-- transition-only — they do not encode the from->to graph, so this
-- uncertainty doesn't block them. A transition-enforcing trigger is a
-- separate, later step and stays fully out of scope here.
--
-- ------------------------------------------------------------
-- STEP 0 — run this FIRST, before anything below. Paste the output back.
-- ------------------------------------------------------------
--   SELECT status, COUNT(*) FROM campaign_leads GROUP BY status ORDER BY count DESC;
--   SELECT status, COUNT(*) FROM inboxes GROUP BY status ORDER BY count DESC;
--
--   Expected for campaign_leads: only active / pending / sending / completed
--   / paused / bounced / unsubscribed.
--   Expected for inboxes: only warmup / ready / paused / retired.
--
--   If ANY other value shows up: STOP. Do not apply the constraints below
--   until you've traced where that value came from — it means either the
--   graph reconstruction above missed a write site, or there's stale data
--   from before some earlier migration/rename.
-- ------------------------------------------------------------

-- STEP 1 — additive, NOT VALID: only checked on new/updated rows, existing
-- rows are NOT scanned (fast, no lock contention). Safe to run once STEP 0
-- confirms no unexpected values exist.

ALTER TABLE campaign_leads
  ADD CONSTRAINT campaign_leads_status_check
  CHECK (status IN ('active','pending','sending','completed','paused','bounced','unsubscribed'))
  NOT VALID;

ALTER TABLE inboxes
  ADD CONSTRAINT inboxes_status_check
  CHECK (status IN ('warmup','ready','paused','retired'))
  NOT VALID;

-- ------------------------------------------------------------
-- STEP 2 — OPTIONAL, LATER, SEPARATE from this migration's rollout: once
-- STEP 1 has been live for a while with zero constraint-violation errors in
-- the logs, validate the constraint against existing rows too (this DOES
-- scan the full table — expect a brief lock, run outside peak hours):
--
--   ALTER TABLE campaign_leads VALIDATE CONSTRAINT campaign_leads_status_check;
--   ALTER TABLE inboxes VALIDATE CONSTRAINT inboxes_status_check;
--
-- Do not run STEP 2 in the same sitting as STEP 1 — the point of NOT VALID
-- is to decouple "new writes are checked" from "full table scan," so a
-- problem in one doesn't block the other.
-- ------------------------------------------------------------

-- ------------------------------------------------------------
-- Verification (after STEP 1):
--   SELECT conname, convalidated FROM pg_constraint
--   WHERE conname IN ('campaign_leads_status_check', 'inboxes_status_check');
--   -- expect 2 rows, convalidated = false until STEP 2 runs
-- ------------------------------------------------------------
