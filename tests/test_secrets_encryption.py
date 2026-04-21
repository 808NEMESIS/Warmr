"""
tests/test_secrets_encryption.py — verify webhooks.secret and
crm_integrations.api_key are encrypted at rest + roundtrip correctly
through the write/read paths.

Unit-level: mock Supabase client and exercise the encrypt/decrypt functions
against the real utils.secrets_vault. Does not hit live Supabase.

For live integration coverage of the full HTTP flow, see
tests/test_rls_isolation.py for the pattern.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ensure a master key is set for the test run so encrypt actually encrypts.
os.environ.setdefault("WARMR_MASTER_KEY", "unit-test-master-key-do-not-use-in-prod")

from utils.secrets_vault import decrypt, encrypt  # noqa: E402


# ── Roundtrip ─────────────────────────────────────────────────────────────

def test_encrypt_prefix_present():
    out = encrypt("super-secret-token")
    assert out.startswith("enc:"), f"Expected enc: prefix, got {out[:10]}"


def test_encrypt_decrypt_roundtrip():
    for plaintext in [
        "hubspot_abc_123",
        "sk-ant-api03-xxx-very-long-token-with-chars",
        "p@ssw0rd with spaces",
        "🔑 emoji key",
        "a",  # minimal
    ]:
        ciphertext = encrypt(plaintext)
        assert decrypt(ciphertext) == plaintext


def test_encrypt_is_idempotent():
    """Encrypting an already-encrypted value returns it unchanged."""
    once = encrypt("foo")
    twice = encrypt(once)
    assert twice == once


def test_decrypt_plaintext_returns_unchanged():
    """Backward-compat: legacy plaintext rows flow through decrypt() unchanged."""
    assert decrypt("plain-value-no-prefix") == "plain-value-no-prefix"
    assert decrypt("") == ""


def test_encrypt_empty_returns_empty():
    assert encrypt("") == ""


def test_decrypt_with_wrong_master_key_raises():
    """Rotating WARMR_MASTER_KEY must invalidate previously-encrypted blobs."""
    original_key = os.environ.get("WARMR_MASTER_KEY", "")
    try:
        os.environ["WARMR_MASTER_KEY"] = "key-one"
        # Reload the module so the key cache (if any) is fresh — our vault is stateless
        # but we'll re-import to be sure.
        import importlib

        import utils.secrets_vault as vault
        importlib.reload(vault)
        ciphertext = vault.encrypt("my-secret")

        os.environ["WARMR_MASTER_KEY"] = "key-two-different"
        importlib.reload(vault)
        try:
            vault.decrypt(ciphertext)
            raise AssertionError("decrypt() with wrong key should have raised RuntimeError")
        except RuntimeError:
            pass  # expected
    finally:
        os.environ["WARMR_MASTER_KEY"] = original_key
        import importlib
        import utils.secrets_vault as vault
        importlib.reload(vault)


# ── Write-path shape (no live DB) ─────────────────────────────────────────

def test_crm_payload_shape_encrypts_api_key():
    """Mimics the payload build in api/main.py create_crm_integration."""
    api_key_raw = "hubspot-pat-test-123"
    payload = {
        "client_id": "test-client",
        "provider": "hubspot",
        "api_key": encrypt(api_key_raw) if api_key_raw else api_key_raw,
    }
    assert payload["api_key"].startswith("enc:")
    assert decrypt(payload["api_key"]) == api_key_raw


def test_webhook_row_shape_encrypts_secret():
    """Mimics the row build in api/public_api.create_webhook."""
    import secrets
    plain_secret = secrets.token_hex(32)
    row = {
        "client_id": "test-client",
        "url": "https://example.com/hook",
        "events": ["lead.replied"],
        "secret": encrypt(plain_secret),
        "active": True,
    }
    assert row["secret"].startswith("enc:")
    assert decrypt(row["secret"]) == plain_secret
    # Stored ciphertext must not be the plaintext
    assert plain_secret not in row["secret"]


# ── Read-path shape ───────────────────────────────────────────────────────

def test_webhook_dispatcher_decrypts_before_hmac():
    """Simulate the webhook_dispatcher.deliver secret handling."""
    import hashlib
    import hmac
    import secrets as _s

    plain = _s.token_hex(32)
    stored = encrypt(plain)

    # The dispatcher path:
    decrypted = decrypt(stored)
    body = b'{"event":"test"}'
    sig_with_decrypted = hmac.new(decrypted.encode(), body, hashlib.sha256).hexdigest()
    sig_with_plain = hmac.new(plain.encode(), body, hashlib.sha256).hexdigest()

    assert sig_with_decrypted == sig_with_plain, (
        "HMAC of decrypted stored secret must match HMAC of the plaintext "
        "returned to the client at creation time."
    )


def test_crm_dispatcher_decrypts_before_use():
    """Simulate crm_dispatcher loading + using api_key."""
    plain = "hubspot_test_token_abc"
    stored = encrypt(plain)

    # What crm_dispatcher does:
    integration = {"api_key": stored, "provider": "hubspot"}
    integration["api_key"] = decrypt(integration["api_key"])

    assert integration["api_key"] == plain
    # And the `Authorization: Bearer {api_key}` header would now be correct.


# ── Migration script idempotency ──────────────────────────────────────────

def test_migration_needs_encryption_detection():
    """scripts/migrate_encrypt_secrets._needs_encryption logic."""
    from scripts.migrate_encrypt_secrets import _needs_encryption
    assert _needs_encryption("plaintext") is True
    assert _needs_encryption("enc:already-encrypted") is False
    assert _needs_encryption("") is False
    assert _needs_encryption(None) is False


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"  \u2713 {name}")
            except AssertionError as e:
                failed += 1
                print(f"  \u2717 {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  \u2717 {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
