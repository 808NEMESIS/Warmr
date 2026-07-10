-- ============================================================
-- Warmr — RLS Emergency Revoke (immediate mitigation)
-- Fixes WARMR_ENTERPRISE_AUDIT_2026-07.md §6.1 / WARMR_ENTERPRISE_AUDIT_V2_Q3_2026.md P0-C:
--   8 tenant tables have RLS DISABLED. Supabase grants anon/authenticated
--   default privileges on every public table, so ANY holder of the public
--   anon key (shipped in every browser bundle — see frontend/config.js) can
--   read/write every tenant's rows in these tables directly via PostgREST.
--
-- SCOPE — this file is DELIBERATELY minimal. It contains ONLY the REVOKE
-- statements, extracted from the full api/rls_hardening_migration.sql, so
-- the live leak can be closed with a 30-second copy-paste into the Supabase
-- SQL editor WITHOUT waiting for the larger migration (which also enables
-- RLS and adds defense-in-depth SELECT policies) to be validated on a
-- branch first.
--
-- WHY REVOKE ALONE IS SUFFICIENT: Postgres permission grants (GRANT/REVOKE)
-- are a layer BELOW row level security — they gate whether a role may touch
-- the table AT ALL, independent of whether RLS is enabled. Revoking
-- anon/authenticated's table-level privileges makes the table
-- inaccessible to those roles immediately, even while RLS itself remains
-- disabled. service_role (used by every Python backend and the FastAPI
-- app — api/main.py:94, api/public_api.py:52) has BYPASSRLS and is a
-- SEPARATE role from anon/authenticated, so it is completely unaffected by
-- these REVOKEs — every backend code path keeps working unchanged.
--
-- Run api/rls_hardening_migration.sql afterwards (on a branch first) to add
-- ENABLE ROW LEVEL SECURITY + tenant-scoped policies as defense-in-depth,
-- in case a table is ever re-GRANTed for a future dashboard feature.
--
-- Idempotent: safe to run multiple times. Run in the Supabase SQL editor
-- (or via CLI) with the service role, against project zomdrygdcaenjnrrpcpw.
-- ============================================================

REVOKE ALL ON webhook_logs             FROM anon, authenticated;
REVOKE ALL ON webhook_events           FROM anon, authenticated;
REVOKE ALL ON warmup_network_accounts  FROM anon, authenticated;
REVOKE ALL ON network_health_log       FROM anon, authenticated;
REVOKE ALL ON placement_test_results   FROM anon, authenticated;
REVOKE ALL ON dns_check_log            FROM anon, authenticated;
REVOKE ALL ON blacklist_recoveries     FROM anon, authenticated;
REVOKE ALL ON unsubscribe_tokens       FROM anon, authenticated;

-- ------------------------------------------------------------
-- Verification (run manually right after applying — see also
-- docs/production_schema_state.md §3C for the same query):
--
--   SELECT table_name, grantee, privilege_type
--   FROM information_schema.role_table_grants
--   WHERE table_name IN ('webhook_logs','webhook_events','warmup_network_accounts',
--                        'network_health_log','placement_test_results','dns_check_log',
--                        'blacklist_recoveries','unsubscribe_tokens')
--     AND grantee IN ('anon','authenticated')
--   ORDER BY table_name, grantee;
--   -- expect ZERO rows. Any row returned means the leak is still open for
--   -- that table/role — re-run the REVOKE above.
-- ------------------------------------------------------------
