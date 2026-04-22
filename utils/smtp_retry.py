"""
utils/smtp_retry.py — wrap SMTP sends with retry on transient failures.

SMTP codes split into categories:
  - 4xx: transient (rate limit, greylisting, temp auth issue) → retry is safe
  - 5xx: permanent (bad address, blocked) → retry is wasted work + reputation harm

Usage:
    from utils.smtp_retry import send_with_retry

    send_with_retry(
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        sender_email=...,
        sender_password=...,
        recipient_email=...,
        msg_as_string=msg.as_string(),
    )

Raises:
    smtplib.SMTPException on permanent failure OR after exhausting retries.

Defaults are conservative — 3 attempts over ~30 seconds. Override via env:
    WARMR_SMTP_MAX_ATTEMPTS (default 3)
    WARMR_SMTP_BACKOFF_BASE  (default 5 seconds)
"""

from __future__ import annotations

import logging
import os
import smtplib
import time

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = int(os.getenv("WARMR_SMTP_MAX_ATTEMPTS", "3"))
BACKOFF_BASE = float(os.getenv("WARMR_SMTP_BACKOFF_BASE", "5"))


# SMTP response codes that are safe to retry.
# RFC 5321 §4.2.1: 4yz responses are transient.
TRANSIENT_CODES = {421, 450, 451, 452, 454}


def _is_transient(exc: Exception) -> bool:
    """True if the SMTP error is a transient 4xx that's worth retrying."""
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code in TRANSIENT_CODES
    # Socket-level errors (connection reset, timeout) are also transient
    if isinstance(exc, (TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected)):
        return True
    return False


def send_with_retry(
    smtp_host: str,
    smtp_port: int,
    sender_email: str,
    sender_password: str,
    recipient_email: str,
    msg_as_string: str,
    max_attempts: int | None = None,
) -> int:
    """
    Send one message via SMTP SSL with retry on transient errors.

    Returns the number of attempts it took (1 on first-try success).
    Raises the last exception if all attempts fail or on a permanent 5xx.
    """
    attempts_allowed = max_attempts if max_attempts is not None else MAX_ATTEMPTS
    last_exc: Exception | None = None

    for attempt in range(1, attempts_allowed + 1):
        try:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, [recipient_email], msg_as_string)
            if attempt > 1:
                logger.info(
                    "SMTP send succeeded on attempt %d for %s → %s",
                    attempt, sender_email, recipient_email,
                )
            return attempt
        except smtplib.SMTPException as exc:
            last_exc = exc
            if not _is_transient(exc):
                # Permanent failure — do not retry
                logger.warning(
                    "SMTP permanent failure %s → %s on attempt %d: %s",
                    sender_email, recipient_email, attempt, exc,
                )
                raise
            if attempt < attempts_allowed:
                backoff = BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "SMTP transient failure %s → %s on attempt %d/%d (%s) — retrying in %.1fs",
                    sender_email, recipient_email, attempt, attempts_allowed, exc, backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "SMTP transient failure %s → %s after %d attempts: %s",
                    sender_email, recipient_email, attempts_allowed, exc,
                )
        except (TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if attempt < attempts_allowed:
                backoff = BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "SMTP network error %s → %s on attempt %d/%d (%s) — retrying in %.1fs",
                    sender_email, recipient_email, attempt, attempts_allowed, exc, backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "SMTP network error %s → %s after %d attempts: %s",
                    sender_email, recipient_email, attempts_allowed, exc,
                )

    assert last_exc is not None
    raise last_exc
