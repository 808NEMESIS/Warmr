-- ============================================================
-- Warmr — RLS Hardening Migration
-- Fixes WARMR_ENTERPRISE_AUDIT_2026-07.md §6.1 (Critical 1):
--   8 tenant tables had RLS DISABLED. Supabase grants anon/authenticated
--   default privileges on every public table, so ANY logged-in tenant could
--   read/write every other tenant's rows with the anon key.
--
-- Tables secured (all confirmed backend/service-role only — no frontend
-- anon-key reads; grep of frontend/*.{js,html} returns zero references):
--   webhook_logs             full_schema.sql:322   (client_id TEXT, nullable)
--   webhook_events           full_schema.sql:343   (client_id TEXT NOT NULL, holds lead PII)
--   warmup_network_accounts  full_schema.sql:392   (client_id TEXT nullable; NULL = shared pool)
--   network_health_log       full_schema.sql:410   (client_id TEXT, nullable)
--   placement_test_results   full_schema.sql:486   (NO client_id → via placement_tests.client_id)
--   dns_check_log            full_schema.sql:521   (NO client_id → via domains.client_id)
--   blacklist_recoveries     full_schema.sql:537   (NO client_id → via domains.client_id)
--   unsubscribe_tokens       full_schema.sql:906   (client_id TEXT NOT NULL)
--
-- STRATEGY
--   The load-bearing fix is: ENABLE RLS + REVOKE ALL FROM anon, authenticated.
--   service_role (used by every Python backend and the FastAPI app — see
--   api/main.py:94, api/public_api.py:52) has BYPASSRLS and is NOT affected
--   by these REVOKEs, so all backend paths keep working unchanged.
--
--   The public unsubscribe flow does NOT need an anon SELECT: the page is
--   rendered server-side by FastAPI using the service-role client
--   (api/main.py:5962-6003 → _supabase.table("unsubscribe_tokens")...), so
--   REVOKE + RLS is safe here — no SECURITY DEFINER RPC required.
--   (The comment "needs public read for unsubscribe page" in
--   suppression_migration.sql:41-42 is inaccurate; the browser never queries
--   this table with the anon key.)
--
--   Tenant-scoped SELECT policies below are DEFENCE-IN-DEPTH. They are
--   DORMANT while anon/authenticated have no table grant, and only become
--   active if you later re-GRANT SELECT to authenticated for direct dashboard
--   reads. They mirror the existing policy styles exactly:
--     - direct client_id  → inboxes (full_schema.sql:642-649)
--     - parent subquery   → warmup_logs (full_schema.sql:665-673)
--     - FOR SELECT read    → diagnostics_log (full_schema.sql:782-786)
--
-- Idempotent: safe to run multiple times.
-- Run in the Supabase SQL editor (or via CLI) with the service role.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. webhook_logs  — direct client_id (nullable); backend-only
--    Read: api/public_api.py:1108, webhook_dispatcher.py. Write: webhook_dispatcher.py:252
-- ------------------------------------------------------------
ALTER TABLE webhook_logs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON webhook_logs FROM anon, authenticated;
GRANT  ALL ON webhook_logs TO service_role;
DROP POLICY IF EXISTS "webhook_logs_isolation" ON webhook_logs;
CREATE POLICY "webhook_logs_isolation"
  ON webhook_logs FOR SELECT
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- 2. webhook_events  — direct client_id (NOT NULL); outbox queue, holds lead PII
--    Read/Write: webhook_dispatcher.py, imap_processor.py:749, api/public_api.py:316
--    Pure backend queue → service-role only, NO tenant policy.
-- ------------------------------------------------------------
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON webhook_events FROM anon, authenticated;
GRANT  ALL ON webhook_events TO service_role;
DROP POLICY IF EXISTS "webhook_events_isolation" ON webhook_events;
-- (no permissive policy: RLS-enabled + no policy = deny-all for non-service roles)

-- ------------------------------------------------------------
-- 3. warmup_network_accounts  — client_id nullable (NULL = shared pool); backend-only
--    Read/Write: diagnostics_engine.py:437, api/main.py:2204
--    Shared-pool rows have NULL client_id and belong to no tenant →
--    service-role only, NO tenant policy (a client_id policy would never
--    match NULL rows anyway).
-- ------------------------------------------------------------
ALTER TABLE warmup_network_accounts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON warmup_network_accounts FROM anon, authenticated;
GRANT  ALL ON warmup_network_accounts TO service_role;
DROP POLICY IF EXISTS "warmup_network_accounts_isolation" ON warmup_network_accounts;
-- (no permissive policy: service-role only)

-- ------------------------------------------------------------
-- 4. network_health_log  — direct client_id (nullable); backend-only
--    Read: api/main.py:1306. Write: diagnostics_engine.py:483
-- ------------------------------------------------------------
ALTER TABLE network_health_log ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON network_health_log FROM anon, authenticated;
GRANT  ALL ON network_health_log TO service_role;
DROP POLICY IF EXISTS "network_health_log_isolation" ON network_health_log;
CREATE POLICY "network_health_log_isolation"
  ON network_health_log FOR SELECT
  USING (client_id = auth.uid()::text);

-- ------------------------------------------------------------
-- 5. placement_test_results  — NO client_id → isolate via placement_tests.client_id
--    Read: api/main.py:3696. Write: placement_tester.py:309
--    Parent-subquery mirrors warmup_logs (full_schema.sql:665-673).
-- ------------------------------------------------------------
ALTER TABLE placement_test_results ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON placement_test_results FROM anon, authenticated;
GRANT  ALL ON placement_test_results TO service_role;
DROP POLICY IF EXISTS "placement_test_results_isolation" ON placement_test_results;
CREATE POLICY "placement_test_results_isolation"
  ON placement_test_results FOR SELECT
  USING (
    test_id IN (
      SELECT id FROM placement_tests WHERE client_id = auth.uid()::text
    )
  );

-- ------------------------------------------------------------
-- 6. dns_check_log  — NO client_id → isolate via domains.client_id
--    Read: api/main.py:3829. Write: dns_monitor.py:98
-- ------------------------------------------------------------
ALTER TABLE dns_check_log ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON dns_check_log FROM anon, authenticated;
GRANT  ALL ON dns_check_log TO service_role;
DROP POLICY IF EXISTS "dns_check_log_isolation" ON dns_check_log;
CREATE POLICY "dns_check_log_isolation"
  ON dns_check_log FOR SELECT
  USING (
    domain_id IN (
      SELECT id FROM domains WHERE client_id = auth.uid()::text
    )
  );

-- ------------------------------------------------------------
-- 7. blacklist_recoveries  — NO client_id → isolate via domains.client_id
--    Read: api/main.py:3811 (via /domains/{id}/health). Write: dns_monitor.py:423
-- ------------------------------------------------------------
ALTER TABLE blacklist_recoveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON blacklist_recoveries FROM anon, authenticated;
GRANT  ALL ON blacklist_recoveries TO service_role;
DROP POLICY IF EXISTS "blacklist_recoveries_isolation" ON blacklist_recoveries;
CREATE POLICY "blacklist_recoveries_isolation"
  ON blacklist_recoveries FOR SELECT
  USING (
    domain_id IN (
      SELECT id FROM domains WHERE client_id = auth.uid()::text
    )
  );

-- ------------------------------------------------------------
-- 8. unsubscribe_tokens  — direct client_id (NOT NULL); backend-only
--    Read/Write: api/main.py (server-side, service role):
--      insert  :5945, select :5965/:5992, update :6003; campaign_scheduler.py:126
--    Public /unsubscribe page is served by FastAPI with the service-role
--    client, NOT the browser anon key → REVOKE is safe, no public read needed.
--    Token is a bearer secret; must NEVER be listable/guessable by tenants →
--    service-role only, NO tenant policy.
-- ------------------------------------------------------------
ALTER TABLE unsubscribe_tokens ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON unsubscribe_tokens FROM anon, authenticated;
GRANT  ALL ON unsubscribe_tokens TO service_role;
DROP POLICY IF EXISTS "unsubscribe_tokens_isolation" ON unsubscribe_tokens;
-- (no permissive policy: service-role only)

COMMIT;

-- ------------------------------------------------------------
-- Verification (run manually after applying):
--   SELECT relname, relrowsecurity
--   FROM pg_class
--   WHERE relname IN ('webhook_logs','webhook_events','warmup_network_accounts',
--                     'network_health_log','placement_test_results','dns_check_log',
--                     'blacklist_recoveries','unsubscribe_tokens');
--   -- expect relrowsecurity = true for all 8
--
--   SELECT table_name, grantee, privilege_type
--   FROM information_schema.role_table_grants
--   WHERE table_name IN ('webhook_logs','webhook_events','warmup_network_accounts',
--                        'network_health_log','placement_test_results','dns_check_log',
--                        'blacklist_recoveries','unsubscribe_tokens')
--     AND grantee IN ('anon','authenticated')
--   ORDER BY table_name, grantee;
--   -- expect ZERO rows
-- ------------------------------------------------------------
