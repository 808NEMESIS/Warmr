"""
utils/secrets_vault.py — encryption at rest for SMTP passwords and other secrets.

Uses Fernet (AES-128-CBC + HMAC-SHA256) symmetric encryption with a master key
derived from WARMR_MASTER_KEY env var. Keeps passwords out of plaintext in `.env`.

Usage:
    from utils.secrets_vault import encrypt, decrypt

    # When saving a password
    stored = encrypt("my-app-password")  # → "enc:..." string

    # When loading
    plain = decrypt(stored)  # returns plain text, or original if not encrypted

Format: encrypted values are prefixed with "enc:" so we can tell at a glance.
Unencrypted plaintext passes through untouched — makes migration incremental.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

_PREFIX = "enc:"


def _get_master_key() -> bytes | None:
    """Derive a 32-byte Fernet key from WARMR_MASTER_KEY env var."""
    raw = os.getenv("WARMR_MASTER_KEY", "")
    if not raw:
        return None
    # Always derive — don't require users to generate a Fernet key
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet():
    """Lazy import of cryptography.fernet so it's optional."""
    try:
        from cryptography.fernet import Fernet, InvalidToken  # type: ignore
        return Fernet, InvalidToken
    except ImportError:
        return None, None


def encrypt(plaintext: str) -> str:
    """
    Encrypt a string; returns `enc:<ciphertext>` or the plaintext if no master key.

    If cryptography is not installed, returns plaintext unchanged with a warning.
    This is fail-safe — we don't block saves when encryption is unavailable.
    """
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext  # Already encrypted

    key = _get_master_key()
    if not key:
        logger.warning("WARMR_MASTER_KEY not set — storing password in plaintext.")
        return plaintext

    Fernet, _ = _fernet()
    if Fernet is None:
        logger.warning("cryptography package not installed — storing password in plaintext.")
        return plaintext

    token = Fernet(key).encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """
    Decrypt `enc:<ciphertext>` back to plaintext. Returns as-is if not encrypted.
    """
    if not value or not value.startswith(_PREFIX):
        return value

    key = _get_master_key()
    if not key:
        raise RuntimeError("Encrypted value present but WARMR_MASTER_KEY is not set.")

    Fernet, InvalidToken = _fernet()
    if Fernet is None:
        raise RuntimeError("Encrypted value present but cryptography package is not installed.")

    ciphertext = value[len(_PREFIX):]
    try:
        return Fernet(key).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(f"Failed to decrypt — master key rotated or data corrupted: {exc}")
