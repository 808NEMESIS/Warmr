"""
dns_monitor.py

Continuous DNS drift monitor and blacklist checker for Warmr.

Two independent check cycles:
    1. DNS drift (every 15 minutes):
       Compares current SPF / DKIM / DMARC records against the expected
       values stored in the domains table. Logs any changes to dns_check_log
       and creates a notification if a record changes or disappears.

    2. Blacklist detection (every 6 hours):
       Resolves the domain's MX or A record to an IP, then queries each
       DNSBL zone via reverse-IP DNS lookup — no paid API required.
       On a new hit, creates a blacklist_recoveries row with a step-by-step
       recovery guide specific to the blacklist.

Run standalone:
    python dns_monitor.py

Or call run_dns_checks(sb) / run_blacklist_checks(sb) from n8n HTTP endpoint.
"""

import logging
import os
import socket
from datetime import datetime, timezone
from typing import Optional

import dns.exception
import dns.resolver
from dotenv import load_dotenv
from supabase import Client, create_client

from api.dns_check import check_spf, check_dkim, check_dmarc, check_mx

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

# DNSBL zones to check (free, no API key required)
DNSBL_ZONES: list[tuple[str, str, int, str]] = [
    # (zone, display_name, estimated_resolution_days, delisting_url_template)
    ("zen.spamhaus.org",         "Spamhaus ZEN",       7,  "https://www.spamhaus.org/lookup/"),
    ("bl.spamcop.net",           "SpamCop",            3,  "https://www.spamcop.net/bl.shtml?{ip}"),
    ("dnsbl.sorbs.net",          "SORBS",              14, "https://www.sorbs.net/lookup.shtml"),
    ("b.barracudacentral.org",   "Barracuda",          5,  "https://www.barracudacentral.org/lookups"),
    ("dnsbl-1.uceprotect.net",   "UCEProtect L1",      10, "https://www.uceprotect.net/en/rblcheck.php"),
]


def _sb() -> Client:
    """Create and return a Supabase client."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now_utc() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Domain loading
# ---------------------------------------------------------------------------

def load_active_domains(sb: Client) -> list[dict]:
    """
    Return all non-blacklisted active domains from the domains table.
    """
    resp = (
        sb.table("domains")
        .select("id, domain, spf_expected, dkim_selector, dmarc_phase")
        .execute()
    )
    return resp.data or []


# ---------------------------------------------------------------------------
# DNS drift detection
# ---------------------------------------------------------------------------

def _log_dns_check(
    sb: Client,
    domain_id: str,
    check_type: str,
    result: str,
    expected: Optional[str],
    actual: Optional[str],
) -> None:
    """Insert a row into dns_check_log."""
    try:
        sb.table("dns_check_log").insert({
            "domain_id":      domain_id,
            "check_type":     check_type,
            "result":         result,
            "expected_value": expected,
            "actual_value":   actual,
            "timestamp":      _now_utc(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to log DNS check: %s", exc)


def _create_drift_notification(
    sb: Client,
    client_id: Optional[str],
    domain: str,
    check_type: str,
    expected: Optional[str],
    actual: Optional[str],
) -> None:
    """Create a notification record for a DNS drift event."""
    try:
        sb.table("notifications").insert({
            "client_id": client_id,
            "type":      "dns_drift",
            "title":     f"DNS change detected: {check_type.upper()} on {domain}",
            "body": (
                f"The {check_type.upper()} record for {domain} has changed.\n"
                f"Expected: {expected or 'present'}\n"
                f"Current:  {actual or 'MISSING'}\n\n"
                "Please verify your DNS configuration immediately."
            ),
            "read":       False,
            "created_at": _now_utc(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to create drift notification: %s", exc)


def check_domain_dns_drift(sb: Client, domain_row: dict) -> dict:
    """
    Compare current DNS records for one domain against expected values.

    Updates domains.last_dns_check and domains.dns_check_status.
    Logs every check to dns_check_log.
    Creates a notification on any change.

    Returns a summary dict.
    """
    domain_id = domain_row["id"]
    domain    = domain_row["domain"]
    selector  = domain_row.get("dkim_selector") or "google"
    spf_exp   = domain_row.get("spf_expected")
    client_id = domain_row.get("client_id")

    issues: list[str] = []

    # ── SPF ───────────────────────────────────────────────────────────────
    spf_ok, spf_record = check_spf(domain)
    if not spf_ok:
        _log_dns_check(sb, domain_id, "spf", "missing", spf_exp, None)
        _create_drift_notification(sb, client_id, domain, "spf", spf_exp, None)
        issues.append("SPF record missing")
        logger.warning("Domain %s: SPF record MISSING.", domain)
    elif spf_exp and spf_record != spf_exp:
        _log_dns_check(sb, domain_id, "spf", "changed", spf_exp, spf_record)
        _create_drift_notification(sb, client_id, domain, "spf", spf_exp, spf_record)
        issues.append(f"SPF changed: {spf_record}")
        logger.warning("Domain %s: SPF record CHANGED.", domain)
    else:
        _log_dns_check(sb, domain_id, "spf", "ok", spf_exp, spf_record)

    # ── DKIM ──────────────────────────────────────────────────────────────
    dkim_ok, dkim_record = check_dkim(domain, selector=selector)
    if not dkim_ok:
        _log_dns_check(sb, domain_id, "dkim", "missing", None, None)
        _create_drift_notification(sb, client_id, domain, "dkim", "present", None)
        issues.append("DKIM record missing")
        logger.warning("Domain %s (selector=%s): DKIM record MISSING.", domain, selector)
    else:
        _log_dns_check(sb, domain_id, "dkim", "ok", None, dkim_record)

    # ── DMARC ─────────────────────────────────────────────────────────────
    dmarc_ok, dmarc_phase, dmarc_record = check_dmarc(domain)
    expected_phase = domain_row.get("dmarc_phase") or "none"
    if not dmarc_ok:
        _log_dns_check(sb, domain_id, "dmarc", "missing", expected_phase, None)
        _create_drift_notification(sb, client_id, domain, "dmarc", expected_phase, None)
        issues.append("DMARC record missing")
        logger.warning("Domain %s: DMARC record MISSING.", domain)
    elif dmarc_phase != expected_phase:
        _log_dns_check(sb, domain_id, "dmarc", "changed", expected_phase, dmarc_phase)
        # Phase change: log but don't alert — DMARC phases progress forward naturally
        logger.info("Domain %s: DMARC phase changed %s → %s.", domain, expected_phase, dmarc_phase)
        # Update stored phase
        try:
            sb.table("domains").update({"dmarc_phase": dmarc_phase}).eq("id", domain_id).execute()
        except Exception:
            pass
    else:
        _log_dns_check(sb, domain_id, "dmarc", "ok", expected_phase, dmarc_record)

    # ── Update domain status ──────────────────────────────────────────────
    status = "error" if issues else "ok"
    try:
        sb.table("domains").update({
            "last_dns_check":   _now_utc(),
            "dns_check_status": status,
        }).eq("id", domain_id).execute()
    except Exception as exc:
        logger.warning("Failed to update domain DNS status: %s", exc)

    return {
        "domain":  domain,
        "status":  status,
        "issues":  issues,
    }


def run_dns_checks(sb: Optional[Client] = None) -> dict:
    """
    Run DNS drift checks for all active domains.

    Called every 15 minutes by n8n dns-monitor workflow.
    Returns {total, ok, warning, error}.
    """
    if sb is None:
        sb = _sb()

    domains = load_active_domains(sb)
    if not domains:
        logger.info("No domains to check.")
        return {"total": 0, "ok": 0, "error": 0}

    logger.info("Running DNS drift checks for %d domains...", len(domains))
    counts = {"total": len(domains), "ok": 0, "warning": 0, "error": 0}

    for domain_row in domains:
        try:
            result = check_domain_dns_drift(sb, domain_row)
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        except Exception as exc:
            logger.error("DNS drift check failed for %s: %s", domain_row.get("domain"), exc)
            counts["error"] = counts.get("error", 0) + 1

    logger.info("DNS drift check complete: %s", counts)
    return counts


# ---------------------------------------------------------------------------
# DNSBL blacklist checking
# ---------------------------------------------------------------------------

def _resolve_domain_ip(domain: str) -> Optional[str]:
    """
    Resolve the mail server IP for a domain.

    Tries MX → A record in sequence.
    Returns IP string or None.
    """
    try:
        # Try MX first
        mx_answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        sorted_mx  = sorted(mx_answers, key=lambda r: r.preference)
        if sorted_mx:
            mx_host = str(sorted_mx[0].exchange).rstrip(".")
            try:
                a_answers = dns.resolver.resolve(mx_host, "A", lifetime=5)
                return str(a_answers[0])
            except Exception:
                pass
    except Exception:
        pass

    # Fall back to A record of domain itself
    try:
        a_answers = dns.resolver.resolve(domain, "A", lifetime=5)
        return str(a_answers[0])
    except Exception:
        pass

    return None


def _check_ip_on_dnsbl(ip: str, zone: str) -> bool:
    """
    Check if an IP is listed on a DNSBL zone using reverse-IP DNS lookup.

    Constructs: {reversed_ip}.{zone} and does an A lookup.
    Returns True if listed (got an A record response).
    """
    reversed_ip = ".".join(reversed(ip.split(".")))
    query = f"{reversed_ip}.{zone}"
    try:
        dns.resolver.resolve(query, "A", lifetime=5)
        return True
    except dns.exception.NXDOMAIN:
        return False
    except Exception:
        return False


def _get_recovery_guide(
    domain: str,
    ip: str,
    zone: str,
    display_name: str,
    delisting_url: str,
    resolution_days: int,
) -> list[dict]:
    """
    Build a step-by-step recovery guide for a specific blacklist.

    Returns list of {step, description, completed, completed_at}.
    """
    base_steps = [
        {
            "step":         1,
            "description":  f"Identify why {domain} ({ip}) was listed on {display_name}. Check your email logs for unusual sending patterns, bounces, or spam complaints in the last 7 days.",
            "completed":    False,
            "completed_at": None,
        },
        {
            "step":         2,
            "description":  "Stop all campaign sending from this domain for at least 24 hours. Continue warmup-only sends to maintain reputation.",
            "completed":    False,
            "completed_at": None,
        },
        {
            "step":         3,
            "description":  "Review and clean your lead list: remove hard bounces, unsubscribes, and any addresses that triggered spam complaints.",
            "completed":    False,
            "completed_at": None,
        },
    ]

    # Blacklist-specific delisting step
    if "spamhaus" in zone.lower():
        delist_step = {
            "step":         4,
            "description":  f"Visit the Spamhaus lookup tool at {delisting_url} and look up your IP ({ip}). Follow the removal request process. Spamhaus ZEN listings are often auto-removed once the sending issue is resolved.",
            "completed":    False,
            "completed_at": None,
        }
    elif "spamcop" in zone.lower():
        url = delisting_url.replace("{ip}", ip)
        delist_step = {
            "step":         4,
            "description":  f"Check your SpamCop listing at {url}. SpamCop listings expire automatically after 24–48 hours once spam stops. No manual request needed unless listing persists beyond 3 days.",
            "completed":    False,
            "completed_at": None,
        }
    elif "barracuda" in zone.lower():
        delist_step = {
            "step":         4,
            "description":  f"Submit a removal request at {delisting_url}. Barracuda requires you to verify ownership of the IP address. Typical removal within 48 hours of submission.",
            "completed":    False,
            "completed_at": None,
        }
    else:
        delist_step = {
            "step":         4,
            "description":  f"Submit a delisting request via {delisting_url}. Estimated resolution: {resolution_days} days.",
            "completed":    False,
            "completed_at": None,
        }

    monitor_step = {
        "step":         5,
        "description":  f"After {resolution_days} day(s), re-run the DNS / blacklist check in Warmr to confirm the listing has been removed. Gradually resume campaign sending (start at 20% of normal volume).",
        "completed":    False,
        "completed_at": None,
    }

    return base_steps + [delist_step, monitor_step]


def check_domain_blacklists(sb: Client, domain_row: dict) -> list[dict]:
    """
    Check one domain's sending IP against all DNSBL zones.

    Creates blacklist_recoveries rows for new hits.
    Returns list of detected blacklist names.
    """
    domain    = domain_row["domain"]
    domain_id = domain_row["id"]
    client_id = domain_row.get("client_id")

    ip = _resolve_domain_ip(domain)
    if not ip:
        logger.warning("Cannot resolve IP for domain %s — skipping blacklist check.", domain)
        return []

    new_listings: list[dict] = []

    for zone, display_name, resolution_days, delisting_url_tpl in DNSBL_ZONES:
        listed = _check_ip_on_dnsbl(ip, zone)
        if not listed:
            _log_dns_check(sb, domain_id, "blacklist", "ok", None, zone)
            continue

        # Check if we already have an open recovery for this blacklist
        existing = (
            sb.table("blacklist_recoveries")
            .select("id")
            .eq("domain_id", domain_id)
            .eq("blacklist_name", display_name)
            .eq("resolved", False)
            .limit(1)
            .execute()
        ).data or []

        if existing:
            logger.info("Domain %s already has open recovery for %s.", domain, display_name)
            continue

        logger.warning("Domain %s (%s) is listed on %s!", domain, ip, display_name)
        _log_dns_check(sb, domain_id, "blacklist", "blacklisted", None, display_name)

        delisting_url = delisting_url_tpl.replace("{ip}", ip)
        recovery_steps = _get_recovery_guide(
            domain, ip, zone, display_name, delisting_url, resolution_days
        )

        try:
            sb.table("blacklist_recoveries").insert({
                "domain_id":                  domain_id,
                "blacklist_name":             display_name,
                "delisting_url":              delisting_url,
                "detected_at":               _now_utc(),
                "estimated_resolution_days":  resolution_days,
                "recovery_steps":             recovery_steps,
                "resolved":                   False,
            }).execute()
        except Exception as exc:
            logger.error("Failed to create blacklist recovery for %s: %s", domain, exc)

        # Mark domain as blacklisted
        try:
            sb.table("domains").update({
                "blacklisted": True,
                "last_blacklist_check": _now_utc(),
            }).eq("id", domain_id).execute()
        except Exception:
            pass

        # Create notification
        try:
            sb.table("notifications").insert({
                "client_id": client_id,
                "type":      "blacklist_hit",
                "title":     f"Blacklist alert: {domain} listed on {display_name}",
                "body": (
                    f"{domain} (IP: {ip}) has been detected on {display_name}.\n"
                    f"A step-by-step recovery guide has been created in your domain dashboard.\n"
                    f"Estimated resolution: {resolution_days} day(s)."
                ),
                "read":       False,
                "created_at": _now_utc(),
            }).execute()
        except Exception:
            pass

        new_listings.append({"blacklist": display_name, "ip": ip})

    # Update last check timestamp regardless of result
    try:
        sb.table("domains").update({
            "last_blacklist_check": _now_utc(),
        }).eq("id", domain_id).execute()
    except Exception:
        pass

    if not new_listings:
        logger.info("Domain %s (%s): clean on all DNSBLs.", domain, ip)

    return new_listings


def run_blacklist_checks(sb: Optional[Client] = None) -> dict:
    """
    Run DNSBL blacklist checks for all active domains.

    Called every 6 hours by n8n dns-monitor workflow.
    Returns {total, clean, new_listings}.
    """
    if sb is None:
        sb = _sb()

    domains = load_active_domains(sb)
    if not domains:
        return {"total": 0, "clean": 0, "new_listings": 0}

    logger.info("Running blacklist checks for %d domains...", len(domains))
    total_new = 0

    for domain_row in domains:
        try:
            hits = check_domain_blacklists(sb, domain_row)
            total_new += len(hits)
        except Exception as exc:
            logger.error("Blacklist check failed for %s: %s", domain_row.get("domain"), exc)

    result = {
        "total":        len(domains),
        "clean":        len(domains) - total_new,
        "new_listings": total_new,
    }
    logger.info("Blacklist check complete: %s", result)
    return result


# ---------------------------------------------------------------------------
# Recovery guide helpers
# ---------------------------------------------------------------------------

def update_recovery_step(
    sb: Client,
    recovery_id: str,
    step_index: int,
    completed: bool = True,
) -> bool:
    """
    Mark a recovery step as completed (or not).

    Checks if all steps are done; if so, marks the recovery as resolved.
    Returns True on success.
    """
    try:
        row = (
            sb.table("blacklist_recoveries")
            .select("recovery_steps")
            .eq("id", recovery_id)
            .single()
            .execute()
        ).data
        if not row:
            return False

        steps: list[dict] = row["recovery_steps"] or []
        updated = False
        for step in steps:
            if step.get("step") == step_index:
                step["completed"]    = completed
                step["completed_at"] = _now_utc() if completed else None
                updated = True
                break

        if not updated:
            return False

        all_done = all(s.get("completed") for s in steps)
        payload: dict = {"recovery_steps": steps}
        if all_done:
            payload["resolved"]    = True
            payload["resolved_at"] = _now_utc()

        sb.table("blacklist_recoveries").update(payload).eq("id", recovery_id).execute()

        if all_done:
            # Clear blacklisted flag on the domain
            domain_id = (
                sb.table("blacklist_recoveries")
                .select("domain_id")
                .eq("id", recovery_id)
                .single()
                .execute()
            ).data.get("domain_id")
            if domain_id:
                # Only clear if no other open recoveries
                other_open = (
                    sb.table("blacklist_recoveries")
                    .select("id", count="exact")
                    .eq("domain_id", domain_id)
                    .eq("resolved", False)
                    .execute()
                ).count or 0
                if other_open == 0:
                    sb.table("domains").update({"blacklisted": False}).eq("id", domain_id).execute()

        return True

    except Exception as exc:
        logger.error("Failed to update recovery step: %s", exc)
        return False


def get_blacklist_recovery(sb: Client, domain_id: str) -> Optional[dict]:
    """
    Fetch the most recent open blacklist recovery for a domain.

    Returns the recovery row dict or None.
    """
    try:
        rows = (
            sb.table("blacklist_recoveries")
            .select("*")
            .eq("domain_id", domain_id)
            .eq("resolved", False)
            .order("detected_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        return rows[0] if rows else None
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "dns"
    sb   = _sb()
    if mode == "blacklist":
        run_blacklist_checks(sb)
    else:
        run_dns_checks(sb)
