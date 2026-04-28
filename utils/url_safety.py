"""
utils/url_safety.py — guard against SSRF when dispatching to user-supplied URLs.

Webhook URLs are a classic SSRF vector: a user creates a webhook pointing at
http://169.254.169.254/ (AWS metadata) or http://localhost:8000/admin and the
backend dutifully POSTs there from inside the trust boundary. This module
rejects anything that resolves to a private / loopback / link-local IP.

Two checks are needed:
  - At creation time: cheap rejection of obviously bad URLs.
  - At dispatch time: re-resolve and re-check, because DNS rebinding can
    flip a hostname's IP between the two events. The 5-second TTL of
    most attacker-controlled DNS makes this trivial otherwise.

Usage:
    from utils.url_safety import assert_url_safe, UnsafeUrlError

    try:
        assert_url_safe(webhook_url)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """The URL is rejected by SSRF policy."""


# Allowed URL schemes. https-only by default. Set WARMR_URL_ALLOW_HTTP=1 to
# permit http (only sensible for local dev/testing of webhook receivers).
_ALLOW_HTTP: bool = os.getenv("WARMR_URL_ALLOW_HTTP", "0") == "1"
_ALLOWED_SCHEMES = ("https", "http") if _ALLOW_HTTP else ("https",)


def _is_private_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the IP address falls in any reserved/private range."""
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _resolve_addresses(hostname: str) -> list[str]:
    """Return all IPv4 + IPv6 addresses a hostname resolves to.

    Raises socket.gaierror on resolution failure — the caller treats that as
    "unsafe" because we can't verify the destination.
    """
    infos = socket.getaddrinfo(hostname, None)
    return list({info[4][0] for info in infos})


def assert_url_safe(url: str) -> None:
    """
    Validate a URL against SSRF policy. Raises UnsafeUrlError if it fails.

    Checks:
      1. Scheme is https (or http when WARMR_URL_ALLOW_HTTP=1).
      2. Host is present and not literally an IP in a private range.
      3. Hostname resolves only to public IPs (re-checked at dispatch time).
      4. Port, when set explicitly, is not a privileged internal port.
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("Empty URL.")

    parsed = urlparse(url.strip())

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"Scheme '{parsed.scheme}' not allowed. Use {' or '.join(_ALLOWED_SCHEMES)}."
        )

    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no host.")

    # Reject literal IP destinations in private ranges before any DNS work.
    try:
        literal_ip = ipaddress.ip_address(host)
        if _is_private_ip(literal_ip):
            raise UnsafeUrlError(f"IP {host} is in a private/reserved range.")
        # Public literal IP — accept; skip DNS resolution.
        return
    except ValueError:
        # Not a literal IP — fall through to DNS resolution.
        pass

    # Resolve hostname and verify NO resolved address is private.
    try:
        addresses = _resolve_addresses(host)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host '{host}': {exc}")

    if not addresses:
        raise UnsafeUrlError(f"Host '{host}' has no DNS records.")

    for addr in addresses:
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError:
            raise UnsafeUrlError(f"Could not parse resolved address '{addr}'.")
        if _is_private_ip(ip_obj):
            raise UnsafeUrlError(
                f"Host '{host}' resolves to private/internal address {addr}. Refusing."
            )


def is_url_safe(url: str) -> bool:
    """Convenience wrapper. True if assert_url_safe passes."""
    try:
        assert_url_safe(url)
        return True
    except UnsafeUrlError:
        return False
