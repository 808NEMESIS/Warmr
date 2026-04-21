"""
scripts/migrate_encrypt_secrets.py — one-time migration to encrypt
plaintext secrets at rest in Supabase.

Scans two tables:
  - webhooks.secret
  - crm_integrations.api_key

For each row whose value does NOT start with the `enc:` prefix, encrypts it
with `utils.secrets_vault.encrypt` and writes it back. Idempotent — rerunning
on an already-encrypted row is a no-op.

WARMR_MASTER_KEY MUST be set. If you lose that key after running this
migration, encrypted secrets become unrecoverable — webhook signing and
CRM syncs will stop working until clients regenerate their credentials.

Usage:
    source .venv/bin/activate
    python scripts/migrate_encrypt_secrets.py --dry-run    # preview
    python scripts/migrate_encrypt_secrets.py              # actually encrypt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow `from utils.secrets_vault import encrypt` when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
_ENC_PREFIX = "enc:"


def _needs_encryption(value: str | None) -> bool:
    """True if the value exists, is non-empty, and is not already encrypted."""
    return bool(value) and not value.startswith(_ENC_PREFIX)


def migrate_table(
    sb: Client,
    table: str,
    pk_col: str,
    secret_col: str,
    encrypt_fn,
    dry_run: bool,
) -> dict[str, int]:
    """
    Encrypt `secret_col` for every row in `table` whose value is plaintext.

    Args:
        sb: Supabase service-role client.
        table: Table name.
        pk_col: Primary key column (usually "id").
        secret_col: Column holding the secret string.
        encrypt_fn: Function that takes plaintext and returns ciphertext with prefix.
        dry_run: If True, do not write changes.

    Returns:
        {"scanned": N, "encrypted": N, "skipped": N, "errors": N}
    """
    stats = {"scanned": 0, "encrypted": 0, "skipped": 0, "errors": 0}
    try:
        # Paginate to avoid huge responses on big tables
        page = 0
        page_size = 500
        while True:
            resp = (
                sb.table(table)
                .select(f"{pk_col}, {secret_col}")
                .range(page * page_size, page * page_size + page_size - 1)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                break
            for row in rows:
                stats["scanned"] += 1
                value = row.get(secret_col)
                pk = row.get(pk_col)
                if not _needs_encryption(value):
                    stats["skipped"] += 1
                    continue

                ciphertext = encrypt_fn(value)
                if not ciphertext.startswith(_ENC_PREFIX):
                    # encrypt() falls back to plaintext if WARMR_MASTER_KEY
                    # missing or cryptography package absent. Abort — we
                    # cannot migrate under these conditions.
                    logger.error(
                        "encrypt_fn returned plaintext — WARMR_MASTER_KEY likely unset. "
                        "Aborting migration for %s %s.",
                        table, pk,
                    )
                    stats["errors"] += 1
                    return stats

                if dry_run:
                    logger.info("[DRY-RUN] Would encrypt %s.%s for %s=%s", table, secret_col, pk_col, pk)
                    stats["encrypted"] += 1
                    continue

                try:
                    sb.table(table).update({secret_col: ciphertext}).eq(pk_col, pk).execute()
                    stats["encrypted"] += 1
                    logger.info("Encrypted %s.%s for %s=%s", table, secret_col, pk_col, pk)
                except Exception as exc:
                    stats["errors"] += 1
                    logger.error("Failed to update %s %s: %s", table, pk, exc)

            if len(rows) < page_size:
                break
            page += 1
    except Exception as exc:
        logger.error("Migration failed for table %s: %s", table, exc)
        stats["errors"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypt plaintext secrets at rest in Supabase.")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts without writing.")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Aborting.")
        return 1

    if not os.getenv("WARMR_MASTER_KEY"):
        logger.critical(
            "WARMR_MASTER_KEY is not set. Encryption requires a master key. "
            "Set WARMR_MASTER_KEY in your .env and rerun. "
            "If you lose the key later, encrypted secrets become unrecoverable."
        )
        return 1

    from utils.secrets_vault import encrypt as _vault_encrypt

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    logger.info("=== Secret encryption migration (%s) ===", mode)

    total = {"scanned": 0, "encrypted": 0, "skipped": 0, "errors": 0}
    for table, pk_col, secret_col in [
        ("webhooks",          "id", "secret"),
        ("crm_integrations",  "id", "api_key"),
    ]:
        logger.info("--- %s.%s ---", table, secret_col)
        stats = migrate_table(sb, table, pk_col, secret_col, _vault_encrypt, args.dry_run)
        logger.info(
            "%s.%s: scanned=%d encrypted=%d skipped=%d errors=%d",
            table, secret_col, stats["scanned"], stats["encrypted"], stats["skipped"], stats["errors"],
        )
        for k in total:
            total[k] += stats[k]

    logger.info("=== Total: scanned=%d encrypted=%d skipped=%d errors=%d ===",
                total["scanned"], total["encrypted"], total["skipped"], total["errors"])

    if args.dry_run:
        logger.info("Dry-run complete. Rerun without --dry-run to apply changes.")

    return 0 if total["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
