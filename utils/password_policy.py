"""
utils/password_policy.py — password strength validation.

Rules:
  - Minimum 12 characters
  - At least one lowercase, one uppercase, one digit
  - Not in a top-list of common passwords
  - Not equal to email local-part or domain

Used by signup + password-change flows via POST /auth/check-password.

Supabase itself also enforces its own minimum (6 chars), so this is additive.
"""

from __future__ import annotations

import re
from typing import Iterable

MIN_LENGTH = 12
MAX_LENGTH = 128

# Top common passwords — sourced from SecLists, curated. Exact-match only
# (case-insensitive). Real production should ship a bigger list (100k+).
_COMMON = {
    "password", "password1", "password123", "123456", "123456789",
    "qwerty", "qwerty123", "abc123", "letmein", "welcome",
    "admin", "admin123", "administrator", "root", "changeme",
    "iloveyou", "monkey", "dragon", "football", "baseball",
    "passw0rd", "p@ssw0rd", "p@ssword", "welcome1", "welcome123",
    "zaq12wsx", "qwertyuiop", "111111", "000000", "12345678",
    "warmr", "warmr123", "aerys", "aerys123",
}


def _has_upper(s: str) -> bool: return any(c.isupper() for c in s)
def _has_lower(s: str) -> bool: return any(c.islower() for c in s)
def _has_digit(s: str) -> bool: return any(c.isdigit() for c in s)


def check_password(password: str, email: str | None = None) -> tuple[bool, list[str]]:
    """
    Validate a password. Returns (is_valid, errors).

    Errors are short human-readable strings suitable for direct UI display.
    """
    errors: list[str] = []

    if not password:
        return False, ["Wachtwoord is verplicht."]

    if len(password) < MIN_LENGTH:
        errors.append(f"Minimaal {MIN_LENGTH} tekens.")
    if len(password) > MAX_LENGTH:
        errors.append(f"Maximaal {MAX_LENGTH} tekens.")
    if not _has_lower(password):
        errors.append("Minimaal 1 kleine letter.")
    if not _has_upper(password):
        errors.append("Minimaal 1 hoofdletter.")
    if not _has_digit(password):
        errors.append("Minimaal 1 cijfer.")

    lowered = password.lower()
    if lowered in _COMMON:
        errors.append("Te zwak — kies iets unieks.")

    if email:
        local = email.split("@", 1)[0].lower()
        domain = (email.split("@", 1)[1] if "@" in email else "").lower().split(".", 1)[0]
        if local and local in lowered:
            errors.append("Wachtwoord mag je e-mailnaam niet bevatten.")
        if domain and len(domain) >= 4 and domain in lowered:
            errors.append("Wachtwoord mag je bedrijfsnaam niet bevatten.")

    return (len(errors) == 0), errors


def strength_score(password: str) -> int:
    """
    Return a 0–4 strength score (for UI meter).
    0 = empty/very weak, 4 = excellent.
    """
    if not password:
        return 0
    score = 0
    if len(password) >= MIN_LENGTH:
        score += 1
    if len(password) >= 16:
        score += 1
    variety = sum(1 for check in (_has_lower, _has_upper, _has_digit) if check(password))
    variety += 1 if re.search(r"[^a-zA-Z0-9]", password) else 0
    score += min(2, variety - 1)
    if password.lower() in _COMMON:
        score = max(0, score - 2)
    return min(4, score)
