"""
api/dns_check.py

Live DNS record checking for SPF, DKIM, and DMARC via dnspython.
All lookups use a 5-second timeout so the API never hangs on slow resolvers.

DKIM selector defaults to "google" (Google Workspace default).
Pass a different selector for Microsoft 365 ("selector1", "selector2") or custom setups.
"""

from dataclasses import dataclass, field
from typing import Optional

import dns.exception
import dns.resolver


@dataclass
class DNSCheckResult:
    """Structured result for a full DNS health check on one domain."""

    domain: str
    spf_configured: bool = False
    spf_record: Optional[str] = None
    dkim_configured: bool = False
    dkim_selector: str = "google"
    dkim_record: Optional[str] = None
    dmarc_configured: bool = False
    dmarc_phase: str = "none"          # none | quarantine | enforce
    dmarc_record: Optional[str] = None
    mx_configured: bool = False
    mx_records: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON responses."""
        return {
            "domain": self.domain,
            "spf": {
                "configured": self.spf_configured,
                "record": self.spf_record,
            },
            "dkim": {
                "configured": self.dkim_configured,
                "selector": self.dkim_selector,
                "record": self.dkim_record,
            },
            "dmarc": {
                "configured": self.dmarc_configured,
                "phase": self.dmarc_phase,
                "record": self.dmarc_record,
            },
            "mx": {
                "configured": self.mx_configured,
                "records": self.mx_records,
            },
            "overall_healthy": (
                self.spf_configured
                and self.dkim_configured
                and self.dmarc_configured
                and self.mx_configured
            ),
            "errors": self.errors,
        }


def _join_txt(rdata) -> str:
    """Join multi-string TXT record parts into one string."""
    if hasattr(rdata, "strings"):
        return "".join(s.decode("utf-8", errors="replace") for s in rdata.strings)
    return str(rdata)


def check_spf(domain: str) -> tuple[bool, Optional[str]]:
    """
    Query TXT records for domain and return the first SPF record found.

    Returns (configured: bool, record_text: str | None).
    """
    try:
        answers = dns.resolver.resolve(domain, "TXT", lifetime=5)
        for rdata in answers:
            txt = _join_txt(rdata)
            if txt.startswith("v=spf1"):
                return True, txt
    except (dns.exception.DNSException, Exception):
        pass
    return False, None


def check_dkim(domain: str, selector: str = "google") -> tuple[bool, Optional[str]]:
    """
    Query TXT record at {selector}._domainkey.{domain}.

    Google Workspace default selector is 'google'.
    Microsoft 365 uses 'selector1' and 'selector2'.
    Returns (configured: bool, record_text: str | None).
    """
    try:
        dkim_name = f"{selector}._domainkey.{domain}"
        answers = dns.resolver.resolve(dkim_name, "TXT", lifetime=5)
        for rdata in answers:
            txt = _join_txt(rdata)
            if "v=DKIM1" in txt:
                return True, txt
    except (dns.exception.DNSException, Exception):
        pass
    return False, None


def _parse_dmarc_phase(record: str) -> str:
    """Extract DMARC policy phase from a DMARC record string."""
    r = record.lower()
    if "p=reject" in r:
        return "enforce"
    if "p=quarantine" in r:
        return "quarantine"
    return "none"


def check_dmarc(domain: str) -> tuple[bool, str, Optional[str]]:
    """
    Query TXT record at _dmarc.{domain}.

    Returns (configured: bool, phase: str, record_text: str | None).
    Phase is 'none' | 'quarantine' | 'enforce'.
    """
    try:
        dmarc_name = f"_dmarc.{domain}"
        answers = dns.resolver.resolve(dmarc_name, "TXT", lifetime=5)
        for rdata in answers:
            txt = _join_txt(rdata)
            if txt.startswith("v=DMARC1"):
                phase = _parse_dmarc_phase(txt)
                return True, phase, txt
    except (dns.exception.DNSException, Exception):
        pass
    return False, "none", None


def check_mx(domain: str) -> tuple[bool, list[str]]:
    """
    Query MX records for domain.

    Returns (configured: bool, hostnames: list[str]) sorted by preference.
    """
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        # Sort by preference (lowest = highest priority)
        sorted_records = sorted(answers, key=lambda r: r.preference)
        hostnames = [str(r.exchange).rstrip(".") for r in sorted_records]
        return bool(hostnames), hostnames
    except (dns.exception.DNSException, Exception):
        pass
    return False, []


def run_full_dns_check(domain: str, dkim_selector: str = "google") -> DNSCheckResult:
    """
    Run SPF, DKIM, DMARC, and MX checks for a domain in sequence.

    Returns a populated DNSCheckResult dataclass.
    DNS errors per record type are captured in result.errors rather than raised.
    """
    result = DNSCheckResult(domain=domain, dkim_selector=dkim_selector)

    try:
        result.spf_configured, result.spf_record = check_spf(domain)
    except Exception as exc:
        result.errors.append(f"SPF lookup error: {exc}")

    try:
        result.dkim_configured, result.dkim_record = check_dkim(domain, selector=dkim_selector)
    except Exception as exc:
        result.errors.append(f"DKIM lookup error: {exc}")

    try:
        result.dmarc_configured, result.dmarc_phase, result.dmarc_record = check_dmarc(domain)
    except Exception as exc:
        result.errors.append(f"DMARC lookup error: {exc}")

    try:
        result.mx_configured, result.mx_records = check_mx(domain)
    except Exception as exc:
        result.errors.append(f"MX lookup error: {exc}")

    return result
