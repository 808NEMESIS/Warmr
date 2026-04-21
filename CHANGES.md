# CHANGES — Secrets-at-rest + service-role isolation hardening

Date: 2026-04-18
Scope: close two security gaps identified in `WARMR_AUDIT.md`
(secrets stored in plaintext in Supabase; service-role queries never
tested for `client_id` isolation).

---

## Threat model correction (Task 1)

`SECRETS_AUDIT.md` replaces the original audit's Priority 1 claim.
The original audit assumed SMTP/IMAP passwords lived in Supabase — they do
not. The real plaintext secrets at rest are:

| Table | Column | Origin |
|---|---|---|
| `webhooks` | `secret` | server-generated `token_hex(32)` at create time |
| `crm_integrations` | `api_key` | provider API token supplied by the client |

`api_keys.key_hash` is already a SHA-256 one-way hash — not touched.
All SMTP/IMAP credentials are env-sourced (`.env`) on the backend and
never written to Supabase.

---

## Task 2 — Wire `utils/secrets_vault` into write/read paths

`utils/secrets_vault.encrypt()` returns `enc:<fernet-ciphertext>` and
`decrypt()` is a no-op on values without the `enc:` prefix (backward compat
for rows that predate the migration). `WARMR_MASTER_KEY` must be set in
production; absent it, `encrypt()` falls back to plaintext with a log
warning and `decrypt()` of encrypted blobs raises.

Files modified:

- `api/main.py`
  - `create_crm_integration` — encrypt `api_key` before insert; mask before returning.
  - `update_crm_integration` — encrypt `api_key` in patch if present.
  - `list_crm_integrations` — decrypt before masking for UI display.
  - `test_crm_integration` — decrypt before passing to provider sync.
- `api/public_api.py`
  - `create_webhook` — encrypt server-generated secret before insert; plaintext returned once to caller, never readable again.
- `webhook_dispatcher.py`
  - `deliver` — decrypt webhook secret before HMAC-signing the payload.
- `crm_dispatcher.py`
  - `dispatch_event` — decrypt `api_key` on every loaded integration before
    routing to `sync_to_hubspot` / `sync_to_pipedrive` / `sync_to_webhook`.

No schema changes. Rows stored before this patch continue to work — the
read paths decrypt-or-passthrough, so the migration in Task 3 can be run
at any time without downtime.

---

## Task 3 — Migration script

`scripts/migrate_encrypt_secrets.py` — one-time migration that scans:

- `webhooks.secret`
- `crm_integrations.api_key`

For each row whose value does not start with `enc:`, encrypts it with
`utils.secrets_vault.encrypt` and writes it back. Aborts if
`WARMR_MASTER_KEY` is unset (refuses to silently store plaintext under
the wrong assumption of encryption). Idempotent — rerunning is safe.

Usage:

```bash
source .venv/bin/activate
python scripts/migrate_encrypt_secrets.py --dry-run   # preview counts
python scripts/migrate_encrypt_secrets.py             # apply
```

---

## Task 4 — Encryption unit tests

`tests/test_secrets_encryption.py` — 11 tests:

1. `enc:` prefix present on encrypted output
2. Round-trip of several shapes (ASCII, long token, spaces, emoji, 1-char)
3. Idempotency — `encrypt(encrypt(x)) == encrypt(x)`
4. Backward compat — `decrypt("plaintext") == "plaintext"`
5. Empty-string handling
6. Wrong `WARMR_MASTER_KEY` raises `RuntimeError`
7. CRM payload shape — `api_key` gets `enc:` prefix
8. Webhook row shape — `secret` gets `enc:` prefix and plaintext is gone
9. `webhook_dispatcher` HMAC equals HMAC of original plaintext
10. `crm_dispatcher` decrypt flow produces the original token
11. Migration `_needs_encryption` detects all edge cases

Registered in `tests/run_all.py`.

---

## Task 5 — Backend service-role isolation tests

`tests/test_backend_service_role_isolation.py` — live Supabase integration
test. Seeds two fake clients (A and B) with inboxes / campaigns /
leads / warmup_logs / sending_schedule / email_events, then asserts that
backend functions taking a `client_id` never return rows for the other
client. Cleanup always runs in `finally`.

Covered functions:

- `weekly_report.gather_client_metrics` — metrics are per-client
- `campaign_scheduler.load_due_campaign_leads` — does not cross tenants
  (note: this one is keyed by `campaign_id`, not `client_id` — the test
  confirms that each campaign row's `client_id` is correct)
- `funnel_engine.snapshot_funnel` — counts match per-client row counts
- `engagement_scorer` decay — independent per lead

**Non-fatal gap report** (prints at test-end): the test also enumerates
the backend functions that do NOT take `client_id` and therefore fetch
rows from every tenant in the same call:

- `warmup_engine.load_active_inboxes`
- `imap_processor.load_active_inboxes`
- `bounce_handler.load_active_inboxes`
- `daily_reset` — resets `daily_sent` for every inbox (intentional)

Isolation for these scripts depends on the operational model: one Warmr
instance per tenant with `.env` holding that tenant's credentials, not on
`client_id` filtering in code. The test is **non-fatal** about these —
they are documented, not broken. If Warmr ever moves to a single-instance
multi-tenant worker model, each function above needs a `client_id`
parameter and an `.eq("client_id", …)` filter.

This test is NOT registered in `tests/run_all.py` (it requires live
Supabase creds and writes data). Run manually:

```bash
source .venv/bin/activate
python tests/test_backend_service_role_isolation.py
```

---

## Task 6 — Static analyzer

`scripts/check_service_role_queries.py` — AST-based scanner that:

1. Parses `full_schema.sql` → set of tables with a `client_id` column
   (28 tables).
2. Walks every `.execute()` call in every `.py` file.
3. Resolves the method chain: `.table("x")` anchor + any
   `.select` / `.update` / `.delete` / `.insert` / `.upsert` ops.
4. Checks for `.eq("client_id", …)` / `.in_("client_id", …)` /
   `.match({"client_id": …})`, OR — for `insert` / `upsert` — a literal
   dict payload containing a `client_id` key.
5. Reports findings; exits 1 if any.

Allowlist marker: `# service-audit: allow` on (or immediately above) a
query line marks it as reviewed.

Run:

```bash
python scripts/check_service_role_queries.py         # full report
python scripts/check_service_role_queries.py --json  # machine-readable
```

**Baseline (2026-04-18):** 155 findings, 1 `.rpc(...)` call. Almost all
findings are the "check-first-then-act-by-pk" pattern:

```python
check = sb.table("x").select("client_id").eq("id", pk).limit(1).execute()
if check.data[0]["client_id"] != auth_client_id:
    raise HTTPException(403)
sb.table("x").update(patch).eq("id", pk).execute()  # ← flagged but safe
```

This is safe at runtime (the ownership gate runs first) but not statically
verifiable. The script is therefore **not** wired into CI by default.
Use it as:

- an on-demand audit tool when touching service-role code paths;
- a review aid — grep the output for tables/ops you did not expect.

Limitations (documented in the script docstring):

- Only literal `.table("...")` and `.eq("client_id", ...)` strings are seen.
- Chains split across statements (`q = …; q = q.eq(…); q.execute()`) are not tracked.
- `.rpc(…)` calls are listed separately (opaque to static analysis).

---

## Infra + deps

- `requirements.txt` — added `cryptography>=42.0` (was implicitly available
  via `python-jose[cryptography]`, now explicit).
- `scripts/__init__.py` — new empty file so `tests/…` can import
  `scripts.migrate_encrypt_secrets`.

---

## How to verify

```bash
# 1. Unit tests — all encryption + existing suites
source .venv/bin/activate
python tests/run_all.py
# → TOTAL: 91/91 passed

# 2. Live service-role isolation (needs SUPABASE_URL + SUPABASE_KEY)
python tests/test_backend_service_role_isolation.py

# 3. Static analyzer baseline
python scripts/check_service_role_queries.py

# 4. Migration dry-run (needs WARMR_MASTER_KEY + Supabase creds)
python scripts/migrate_encrypt_secrets.py --dry-run
```

---

## What still depends on ops discipline

Two things cannot be closed in code alone:

1. **`WARMR_MASTER_KEY` secrecy** — if lost, every encrypted `webhooks.secret`
   and `crm_integrations.api_key` becomes unrecoverable and clients must
   re-register. Back it up in a password manager out of band.

2. **One-instance-per-tenant for workers** — `warmup_engine`, `imap_processor`,
   `bounce_handler`, `daily_reset` read inboxes for every tenant in the
   same Supabase project. Running one Warmr deployment for multiple
   tenants from the same `.env` will mix sends. Documented but not
   enforced in code.
