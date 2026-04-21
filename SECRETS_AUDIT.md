# Secrets Vault Usage Audit — 2026-04-20

**Scope:** Locate every path where a secret is written to or read from Supabase,
map against current `utils/secrets_vault.py` usage, and identify which paths
need wiring. NO CODE CHANGES yet — this is the scope-confirmation document.

---

## TL;DR — the threat model from WARMR_AUDIT.md Priority 1 was partly wrong

The audit assumed SMTP app-passwords live in Supabase. They do **not**. SMTP
passwords are read from `.env` at runtime and never touch the database. The
`inboxes` schema has no password column (`full_schema.sql:42-65`), and
`api/main.py:448-463` confirms no password key is included in the Supabase
insert payload for `POST /inboxes`. The comment in `warmup_engine.py:552`
spells it out: `# password fetched from env, not DB`.

**Real Supabase-resident secrets that currently sit plaintext:**

| Table | Column | Type | Writer | Reader |
|---|---|---|---|---|
| `webhooks` | `secret` | Plaintext string | `api/public_api.py:640` (POST /webhooks) | `webhook_dispatcher.py:123` (_sign_payload) |
| `crm_integrations` | `api_key` | Plaintext string | `api/main.py:4943` (POST /crm/integrations) | `crm_dispatcher.py:53, 96` (sync_to_hubspot/pipedrive) |
| `api_keys` | `key_hash` | SHA256 hash | `api/public_api.py:767` | `api/public_api.py:110` |

`api_keys.key_hash` is already one-way hashed (`hashlib.sha256` at
`api/public_api.py:77-79`) — correct for API key storage, no encryption
needed. The other two are real gaps.

**Current `secrets_vault.py` usage:** **zero**. Grep for `from utils.secrets_vault`
or `secrets_vault.` across the whole repo returns only the file's own
docstring (`utils/secrets_vault.py:2, 8`). The module is orphan code —
defined but never imported.

**API name note:** `secrets_vault.py` exports `encrypt()` and `decrypt()`
(not `encrypt_password` / `decrypt_password` as the task described). The
functions accept any string; the password-specific naming in the task is
descriptive, not literal.

---

## Passwords in Supabase writes/reads

### `inboxes` table — **no password column**

- Schema: `full_schema.sql:42-65` — no password / app_password / smtp_password column
- Create endpoint: `api/main.py:422-479` — payload dict at `:448-463` has no password field
- No update endpoint touches a password field (`patch_inbox` at `:516-526` restricts to `{status, warmup_active, notes, daily_warmup_target, daily_campaign_target}`)

### SMTP/IMAP passwords — read exclusively from environment

| File:Line | Env var pattern | Purpose |
|---|---|---|
| `warmup_engine.py:85, 620` | `WARMUP_NETWORK_{i}_PASSWORD`, `INBOX_{i}_PASSWORD` | Warmup sending |
| `warmup_engine.py:685` | `INBOX_{i}_PASSWORD` | Dry-run preview |
| `imap_processor.py:129, 149` | `WARMUP_NETWORK_{i}_PASSWORD`, `INBOX_{i}_PASSWORD` | Spam rescue + reply generation |
| `campaign_scheduler.py:88` | `INBOX_{i}_PASSWORD` | Campaign send |
| `bounce_handler.py:107` | `INBOX_{i}_PASSWORD` | DSN / ARF scan |
| `diagnostics_engine.py:392, 434` | `WARMUP_NETWORK_{i}_PASSWORD`, `acc["password"]` | IMAP health check |
| `placement_tester.py:133` | `{prefix}_PASSWORD` | Seed account placement test |
| `test_connections.py:77` | `INBOX_{i}_PASSWORD` | Smoke test |
| `tests/test_smtp_connection.py:25`, `tests/test_imap_connection.py:24` | `INBOX_{i}_PASSWORD` | Smoke test |

**Implication:** The existing audit recommendation "encrypt SMTP passwords in
Supabase" is moot — there are no such passwords in Supabase. What DOES exist:
passwords in `.env` plaintext on disk. That is a different threat (server
compromise, not DB leak) and `secrets_vault` as currently designed doesn't
solve it — you'd need filesystem-level encryption or a vault provider.

---

## Real gaps — where `secrets_vault` SHOULD be wired

### Gap 1: `webhooks.secret` — plaintext in Supabase

- **Writer:** `api/public_api.py:640`
  ```python
  secret = secrets.token_hex(32)  # 64-char hex signing secret
  row = {"client_id": ..., "url": ..., "events": ..., "secret": secret, ...}
  resp = sb.table("webhooks").insert(row).execute()
  ```
  Secret is server-generated, returned once to the API caller (`:649`), and
  stored plaintext.

- **Reader:** `webhook_dispatcher.py` (via `_sign_payload` at `:86` and usage
  at `:123`):
  ```python
  def _sign_payload(secret: str, body_bytes: bytes) -> str:
      return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
  ```
  The secret is read from the `webhooks.secret` column, decoded to bytes, used
  for HMAC. Encryption needs to decrypt before `_sign_payload`.

- **List endpoint** (`api/public_api.py:653-664`) explicitly excludes `secret`
  from its SELECT — good, it never returns to clients post-creation.

- **Risk if Supabase leaks:** Attacker can forge webhook deliveries that pass
  HMAC verification on the client's receiving endpoint → reply/bounce/unsub
  events can be spoofed against Heatr-like consumers.

### Gap 2: `crm_integrations.api_key` — plaintext in Supabase

- **Writer:** `api/main.py:4943` (POST /crm/integrations):
  ```python
  payload = {
      "client_id": client_id,
      "provider": provider,
      "api_key": body.get("api_key"),   # ← plaintext straight from client
      ...
  }
  resp = _supabase.table("crm_integrations").insert(payload).execute()
  ```

- **Update:** `api/main.py:4975` — `patch` dict may include `api_key` plaintext
  (`allowed` set at `:4969` contains `api_key`).

- **Reader:** `crm_dispatcher.py`:
  - `sync_to_hubspot` (`:53`): `api_key = integration.get("api_key", "")`
  - `sync_to_pipedrive` (`:96`): same pattern
  - Used in `Authorization: Bearer {api_key}` header (`:71, 82`) and Pipedrive
    query param (`:113`).

- **List endpoint masking:** `api/main.py:4910-4914` masks api_key before
  returning the list (`6 chars + "..." + 4 chars`). Good — but the full
  plaintext is still in the DB.

- **Risk if Supabase leaks:** Attacker gains read access to tenant HubSpot /
  Pipedrive tenants. Lateral movement from Warmr compromise → customer CRM.

---

## Paths explicitly NOT in scope (already handled or no plaintext)

| Path | Reason |
|---|---|
| `api_keys.key_hash` | Already SHA256 one-way (`api/public_api.py:77-79`). Correct for bearer tokens. |
| `SUPABASE_KEY`, `SUPABASE_JWT_SECRET`, `WARMR_API_TOKEN`, `ANTHROPIC_API_KEY`, `RESEND_API_KEY` | Env-only, not DB-resident. Different threat (server compromise). |
| `client_settings.*` | No credential columns. |
| `crm_integrations.webhook_url` | URL, not secret. |

---

## Proposed scope for Task 2 (wire encryption) — needs your confirmation

Given the corrected threat model, the actual fixes are:

**1. `crm_integrations.api_key` (high value — customer CRM tokens)**
- Write: `api/main.py:4943` — wrap `body.get("api_key")` in `encrypt()`
- Write: `api/main.py:4975` patch handler — if `api_key` in patch, encrypt before update
- Read: `crm_dispatcher.py:53, 96` — wrap `integration.get("api_key")` in `decrypt()`
- Masking in list endpoint (`api/main.py:4910-4914`): decrypt first, then mask
- Test endpoint (`api/main.py:5000`): decrypt before passing to provider call

**2. `webhooks.secret` (medium value — HMAC forgery surface)**
- Write: `api/public_api.py:640` — encrypt before insert
- Read: `webhook_dispatcher.py` (wherever `webhook["secret"]` gets read before `_sign_payload`)
- Backward compat: existing plaintext secrets fall through because `decrypt()`
  returns input unchanged if no `enc:` prefix (`utils/secrets_vault.py:81-82`).

**3. Migration script**
- Scan `webhooks.secret` + `crm_integrations.api_key` for rows without the
  `enc:` prefix → encrypt and write back.
- `--dry-run` flag prints counts, no writes.
- Idempotent via the `enc:` prefix check.

**4. Tests**
- `tests/test_password_encryption.py` — rename suggested to
  `tests/test_secrets_encryption.py` since it's not about passwords. Tests:
  - `POST /crm/integrations` → row in DB has `enc:` prefix
  - `sync_to_hubspot` sees plaintext after decrypt
  - `POST /webhooks` → row in DB has `enc:` prefix
  - `webhook_dispatcher` signs correctly (decrypted secret matches what was
    returned to client at creation)
  - Decrypt with wrong `WARMR_MASTER_KEY` raises
  - Roundtrip: encrypt → Supabase insert → select → decrypt → equals original

**5. `.env.example` note** — `WARMR_MASTER_KEY` is already listed on line 45.
If not set, `encrypt()` falls back to plaintext with a WARNING log. No behavior
change for existing installs.

**6. Blast-radius if `WARMR_MASTER_KEY` is lost** — encrypted secrets become
unrecoverable. Client must regenerate CRM api_keys + webhooks. Document this
in the migration script's help text.

---

## Questions for you before I proceed

1. **Scope confirmation:** Given SMTP passwords are env-only, should Task 2
   fix `crm_integrations.api_key` + `webhooks.secret` as listed above? Or do
   you still want the SMTP-password angle (which would need filesystem-level
   encryption, out of current `secrets_vault.py` design)?

2. **Naming:** Rename `tests/test_password_encryption.py` →
   `tests/test_secrets_encryption.py`? It's about webhook secrets + CRM API
   keys, not passwords.

3. **Migration script name:** Rename
   `scripts/migrate_encrypt_passwords.py` →
   `scripts/migrate_encrypt_secrets.py`? (Same reasoning.)

4. **Backend isolation test (Task 5):** Stays as-scoped. The task description
   is sound — I'll set that up after Task 2/3/4 are confirmed or in parallel.

---

**Status:** Audit complete. No code changed. Waiting for your approval on the
scope above before starting Task 2.
