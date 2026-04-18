"""
enrichment_engine.py

Enriches a single lead through three sequential steps:

    Step 1 — Email verification
        • Syntax validation (format check)
        • Disposable domain detection (hardcoded list)
        • MX record check via dnspython (domain actually receives mail)
        → updates leads.verified (true/false)

    Step 2 — Company enrichment (all providers are optional)
        • Hunter.io   (HUNTER_API_KEY)  — email validation + domain search
        • Clearbit    (CLEARBIT_API_KEY) — company size, industry, tech stack, funding
        • Apify       (APIFY_TOKEN)     — LinkedIn company data via actor
        → data merged into leads.custom_fields (existing keys are preserved)

    Step 3 — Personalised opener via Claude Haiku
        • Generates a unique, single-sentence opening line based on enriched data
        → stored as leads.custom_fields["personalized_opener"]
        → usable as {{opener}} in sequence templates

Call from the queue worker:
    from enrichment_engine import enrich_lead
    result = enrich_lead(lead_id, client_id)

Or import directly for one-off use in scripts.

Environment variables:
    SUPABASE_URL, SUPABASE_KEY         — required
    ANTHROPIC_API_KEY                  — required for Step 3 opener generation
    HUNTER_API_KEY                     — optional, enables Hunter.io enrichment
    CLEARBIT_API_KEY                   — optional, enables Clearbit enrichment
    APIFY_TOKEN                        — optional, enables LinkedIn via Apify
    APIFY_LINKEDIN_ACTOR               — optional, Apify actor ID (default: apify/linkedin-company-scraper)
    WARMUP_LANGUAGE                    — 'nl' | 'en' | 'fr' (default: nl), used in opener prompt
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import dns.exception
import dns.resolver
import httpx
from anthropic import Anthropic
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

_HUNTER_KEY:         Optional[str] = os.getenv("HUNTER_API_KEY")
_CLEARBIT_KEY:       Optional[str] = os.getenv("CLEARBIT_API_KEY")
_APIFY_TOKEN:        Optional[str] = os.getenv("APIFY_TOKEN")
_APIFY_LINKEDIN_ACT: str           = os.getenv("APIFY_LINKEDIN_ACTOR", "apify/linkedin-company-scraper")

_ANTHROPIC_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
_LANGUAGE:      str = os.getenv("WARMUP_LANGUAGE", "nl")

_HTTP_TIMEOUT = 15  # seconds per external API call


# ---------------------------------------------------------------------------
# Disposable email domains
# ---------------------------------------------------------------------------

DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        # Major disposable providers
        "mailinator.com", "guerrillamail.com", "guerrillamailblock.com",
        "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de",
        "guerrillamail.net", "guerrillamail.org",
        "tempmail.com", "temp-mail.org", "temp-mail.io",
        "throwaway.email", "throwam.com",
        "sharklasers.com", "guerrillamailblock.com", "grr.la", "guerrillamail.info",
        "spam4.me", "yopmail.com", "yopmail.fr", "cool.fr.nf", "jetable.fr.nf",
        "nospam.ze.tc", "nomail.xl.cx", "mega.zik.dj", "speed.1s.fr",
        "courriel.fr.nf", "moncourrier.fr.nf", "monemail.fr.nf", "monmail.fr.nf",
        "dispostable.com", "mailnull.com", "mailnull.net",
        "trashmail.com", "trashmail.at", "trashmail.me", "trashmail.net",
        "trashmail.io", "trashmail.org",
        "maildrop.cc", "spamgourmet.com",
        "discard.email", "discardmail.com", "discardmail.de",
        "fakeinbox.com", "tempinbox.com",
        "spamherelots.com", "spamhere.net",
        "mailexpire.com", "spambox.us", "filzmail.com",
        "binkmail.com", "bobmail.info", "chammy.info", "devnullmail.com",
        "fakedemail.com", "imgof.com", "jnxjn.com", "jourrapide.com",
        "klassmaster.com", "klassmaster.net",
        "lol.ovpn.to", "mt2009.com", "mt2014.com", "nospamfor.us",
        "nowmymail.com", "ownmail.net", "pecinan.com", "pecinan.net",
        "proxymail.eu.org", "rklips.com", "spamfree24.org", "spamfree24.de",
        "spamfree24.eu", "spamfree24.info", "spamfree24.net",
        "temporaryinbox.com", "tempr.email", "trbvm.com",
        "truckmail.com", "uggsrock.com", "veryrealemail.com",
        "xagloo.com", "yamu.be",
        # Dutch / BENELUX throwaway providers
        "wegwerpmail.nl", "spamgourmet.org", "mailna.me",
        # Catch-all patterns handled programmatically below
    }
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class EnrichmentResult:
    """Accumulates all findings for one lead enrichment run."""
    lead_id:    str
    email:      str
    domain:     str

    # Step 1 — email verification
    verified:            Optional[bool] = None
    verification_reason: str            = ""

    # Step 2 — company enrichment
    hunter_data:  dict = field(default_factory=dict)
    clearbit_data: dict = field(default_factory=dict)
    apify_data:   dict = field(default_factory=dict)

    # Step 3 — opener
    personalized_opener: Optional[str] = None

    # Combined custom_fields patch
    custom_fields: dict = field(default_factory=dict)

    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sb() -> Client:
    """Return a fresh Supabase client (service role — bypasses RLS)."""
    return create_client(_SUPABASE_URL, _SUPABASE_KEY)


def _now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# STEP 1 — Email verification
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)

_CATCHALL_DISPOSABLE_PATTERNS = (
    "mailinator", "yopmail", "guerrillamail", "trashmail", "tempmail",
    "throwaway", "spamgourmet", "discard.email", "fakeinbox", "maildrop",
)


def _is_disposable(domain: str) -> bool:
    """Return True if domain is a known disposable email provider."""
    d = domain.lower()
    if d in DISPOSABLE_DOMAINS:
        return True
    for pattern in _CATCHALL_DISPOSABLE_PATTERNS:
        if pattern in d:
            return True
    return False


def _has_mx(domain: str) -> bool:
    """Return True if the domain has at least one MX record (can receive mail)."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        return len(list(answers)) > 0
    except (dns.exception.DNSException, Exception):
        return False


def verify_email(email: str, result: EnrichmentResult) -> None:
    """
    Step 1: validate email and update result.verified.

    Checks:
        1. Regex syntax
        2. Disposable domain
        3. MX record present on domain
    """
    email = email.strip().lower()

    if not _EMAIL_RE.match(email):
        result.verified = False
        result.verification_reason = "invalid_syntax"
        return

    domain = email.split("@")[1]

    if _is_disposable(domain):
        result.verified = False
        result.verification_reason = "disposable_domain"
        return

    if not _has_mx(domain):
        result.verified = False
        result.verification_reason = "no_mx_record"
        return

    result.verified = True
    result.verification_reason = "ok"


# ---------------------------------------------------------------------------
# STEP 2 — Company enrichment
# ---------------------------------------------------------------------------

def _hunter_enrich(email: str, domain: str, result: EnrichmentResult) -> None:
    """
    Hunter.io enrichment:
        1. Verify the email address exists
        2. Domain search — fetch other emails at the company for context
    """
    if not _HUNTER_KEY:
        return

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            # Email verifier
            ev_resp = client.get(
                "https://api.hunter.io/v2/email-verifier",
                params={"email": email, "api_key": _HUNTER_KEY},
            )
            if ev_resp.status_code == 200:
                ev_data = ev_resp.json().get("data", {})
                result.hunter_data["email_status"]     = ev_data.get("status")
                result.hunter_data["email_score"]      = ev_data.get("score")
                result.hunter_data["email_disposable"] = ev_data.get("disposable", False)
                result.hunter_data["email_webmail"]    = ev_data.get("webmail", False)
                result.hunter_data["email_mx_found"]   = ev_data.get("mx_records", False)
                # If Hunter says invalid/disposable, override Step 1 result
                if ev_data.get("status") in ("invalid", "disposable", "unknown"):
                    result.verified = False
                    result.verification_reason = f"hunter_{ev_data.get('status')}"

            # Domain search — company name, industry, company size
            ds_resp = client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": _HUNTER_KEY, "limit": 1},
            )
            if ds_resp.status_code == 200:
                ds_data = ds_resp.json().get("data", {})
                result.hunter_data["company_name"]     = ds_data.get("organization")
                result.hunter_data["company_industry"] = ds_data.get("industry")
                result.hunter_data["company_size"]     = ds_data.get("company_size")
                result.hunter_data["company_country"]  = ds_data.get("country")
                result.hunter_data["company_twitter"]  = ds_data.get("twitter")
                result.hunter_data["company_linkedin"]  = ds_data.get("linkedin")
                result.hunter_data["company_description"] = ds_data.get("description")

    except Exception as exc:
        logger.warning("Hunter.io enrichment failed for %s: %s", email, exc)
        result.errors.append(f"hunter_error: {exc}")


def _clearbit_enrich(domain: str, result: EnrichmentResult) -> None:
    """
    Clearbit company enrichment:
        Company size, industry, tech stack, funding, description.
    """
    if not _CLEARBIT_KEY:
        return

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.get(
                "https://company.clearbit.com/v2/companies/find",
                params={"domain": domain},
                headers={"Authorization": f"Bearer {_CLEARBIT_KEY}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                metrics  = data.get("metrics", {})
                category = data.get("category", {})

                result.clearbit_data["company_name"]        = data.get("name")
                result.clearbit_data["company_description"] = data.get("description")
                result.clearbit_data["company_industry"]    = category.get("industry")
                result.clearbit_data["company_sector"]      = category.get("sector")
                result.clearbit_data["company_size"]        = metrics.get("employeesRange")
                result.clearbit_data["employee_count"]      = metrics.get("employees")
                result.clearbit_data["annual_revenue"]      = metrics.get("annualRevenue")
                result.clearbit_data["raised_amount"]       = data.get("crunchbase", {}).get("handle")
                result.clearbit_data["tech_stack"]          = [t.get("tag") for t in data.get("tech", [])]
                result.clearbit_data["founded_year"]        = data.get("foundedYear")
                result.clearbit_data["company_type"]        = data.get("type")  # public|private|education
                result.clearbit_data["company_location"]    = (
                    f"{data.get('geo', {}).get('city')}, {data.get('geo', {}).get('country')}"
                    if data.get("geo", {}).get("city") else None
                )
            elif resp.status_code == 404:
                logger.info("Clearbit: no data for domain %s", domain)
            else:
                logger.warning("Clearbit returned %d for domain %s", resp.status_code, domain)

    except Exception as exc:
        logger.warning("Clearbit enrichment failed for %s: %s", domain, exc)
        result.errors.append(f"clearbit_error: {exc}")


def _apify_linkedin_enrich(domain: str, linkedin_url: Optional[str], result: EnrichmentResult) -> None:
    """
    LinkedIn company enrichment via Apify actor.
    Uses linkedin_url if available, otherwise searches by domain.

    Fetches: description, headcount, specialties, recent posts.
    """
    if not _APIFY_TOKEN:
        return

    if not linkedin_url:
        # Can't search by domain without a URL — skip
        logger.debug("Apify LinkedIn: no linkedin_url for domain %s, skipping", domain)
        return

    try:
        actor_id = _APIFY_LINKEDIN_ACT.replace("/", "~")
        run_url  = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"

        with httpx.Client(timeout=60) as client:  # LinkedIn scrapes take longer
            resp = client.post(
                run_url,
                params={"token": _APIFY_TOKEN},
                json={
                    "startUrls": [{"url": linkedin_url}],
                    "proxyConfiguration": {"useApifyProxy": True},
                },
            )

            if resp.status_code == 200:
                items = resp.json()
                if items:
                    item = items[0]
                    result.apify_data["linkedin_description"]  = item.get("description")
                    result.apify_data["linkedin_headcount"]    = item.get("staffCount")
                    result.apify_data["linkedin_specialties"]  = item.get("specialities", [])
                    result.apify_data["linkedin_founded_year"] = item.get("foundedOn", {}).get("year")
                    result.apify_data["linkedin_followers"]    = item.get("followerCount")
                    # Recent posts for personalisation context
                    posts = item.get("recentPosts") or item.get("posts") or []
                    if posts:
                        result.apify_data["linkedin_recent_post"] = (
                            posts[0].get("text", "")[:300] if posts else None
                        )
            else:
                logger.warning("Apify returned %d for %s", resp.status_code, linkedin_url)

    except Exception as exc:
        logger.warning("Apify LinkedIn enrichment failed for %s: %s", linkedin_url, exc)
        result.errors.append(f"apify_error: {exc}")


def enrich_company(lead_row: dict, result: EnrichmentResult) -> None:
    """
    Step 2: run all configured provider enrichments sequentially.

    Providers that are not configured (no API key) are silently skipped.
    Partial results from one provider don't block others.
    """
    email        = result.email
    domain       = result.domain
    linkedin_url = lead_row.get("linkedin_url")

    _hunter_enrich(email, domain, result)
    _clearbit_enrich(domain, result)
    _apify_linkedin_enrich(domain, linkedin_url, result)

    # Merge all provider data into custom_fields (later providers win on conflicts)
    merged: dict = {}
    for provider_data in (result.hunter_data, result.clearbit_data, result.apify_data):
        # Only set non-None values
        for k, v in provider_data.items():
            if v is not None:
                merged[k] = v

    result.custom_fields.update(merged)


# ---------------------------------------------------------------------------
# STEP 3 — Personalised opener via Claude Haiku
# ---------------------------------------------------------------------------

def _build_opener_context(lead_row: dict, result: EnrichmentResult) -> str:
    """
    Build a compact context string for the opener prompt from enriched data.
    Combines the most useful signals across all providers.
    """
    cf = result.custom_fields

    parts: list[str] = []

    # Company identity
    company = (
        cf.get("company_name")
        or lead_row.get("company_name")
        or result.domain
    )
    parts.append(f"Bedrijf: {company}")

    if industry := cf.get("company_industry"):
        parts.append(f"Industrie: {industry}")

    if size := (cf.get("company_size") or cf.get("employee_count")):
        parts.append(f"Bedrijfsgrootte: {size}")

    if location := cf.get("company_location"):
        parts.append(f"Locatie: {location}")

    # Description — prefer LinkedIn (usually more specific), fallback to others
    description = (
        cf.get("linkedin_description")
        or cf.get("company_description")
    )
    if description:
        # Trim to ~200 chars for the prompt
        parts.append(f"Beschrijving: {description[:200]}")

    # Tech stack (Clearbit)
    if tech := cf.get("tech_stack"):
        if isinstance(tech, list) and tech:
            parts.append(f"Tech stack: {', '.join(str(t) for t in tech[:6])}")

    # Recent LinkedIn post (strong personalisation signal)
    if post := cf.get("linkedin_recent_post"):
        parts.append(f"Recent LinkedIn bericht: {post[:200]}")

    # Fallback if no enrichment data available
    if len(parts) <= 1:
        parts.append(f"Website domein: {result.domain}")
        if job_title := lead_row.get("job_title"):
            parts.append(f"Functietitel ontvanger: {job_title}")

    return "\n".join(parts)


def generate_opener(lead_row: dict, result: EnrichmentResult) -> None:
    """
    Step 3: generate a personalised opening sentence via Claude Haiku.

    The opener is stored in result.custom_fields["personalized_opener"].
    It is usable as {{opener}} in sequence templates.
    """
    if not _ANTHROPIC_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping opener generation for lead %s", result.lead_id)
        return

    language_instruction = {
        "nl": "Schrijf in vloeiend, professioneel Nederlands.",
        "en": "Write in fluent, professional English.",
        "fr": "Écris en français professionnel et fluide.",
    }.get(_LANGUAGE, "Schrijf in vloeiend, professioneel Nederlands.")

    context = _build_opener_context(lead_row, result)

    prompt = f"""Je schrijft een gepersonaliseerde openingszin voor een B2B cold email.

Bedrijfscontext:
{context}

Taak: Schrijf één enkele openingszin (max. 25 woorden) die:
- Specifiek refereert aan iets van dit bedrijf (industrie, activiteit, tech, recente post, of groei)
- Bewondering of oprechte interesse uitdrukt — geen vleiende overdrijving
- Naadloos kan leiden naar een verkoop-pitch zonder dat de zin zelf verkoopt
- Klinkt als geschreven door een mens, niet als een sjabloon

{language_instruction}

Geef ALLEEN de openingszin terug, geen uitleg, geen aanhalingstekens."""

    try:
        client   = Anthropic(api_key=_ANTHROPIC_KEY)
        message  = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        opener = message.content[0].text.strip().strip('"').strip("'")
        result.custom_fields["personalized_opener"] = opener
        result.personalized_opener = opener
        logger.debug("Generated opener for lead %s: %s", result.lead_id, opener)
    except Exception as exc:
        logger.warning("Claude opener generation failed for lead %s: %s", result.lead_id, exc)
        result.errors.append(f"opener_error: {exc}")


# ---------------------------------------------------------------------------
# Write results back to Supabase
# ---------------------------------------------------------------------------

def _flush_to_db(sb: Client, lead_row: dict, result: EnrichmentResult) -> None:
    """
    Merge enrichment results back into the leads table.

    custom_fields is merged via jsonb_merge so existing keys are preserved.
    Uses a raw SQL update to merge JSONB rather than overwrite it.
    """
    lead_id = result.lead_id

    # Merge existing custom_fields with new data
    existing_cf: dict = lead_row.get("custom_fields") or {}
    merged_cf = {**existing_cf, **result.custom_fields}

    patch = {
        "verified":      result.verified,
        "enriched":      True,
        "custom_fields": merged_cf,
    }

    try:
        sb.table("leads").update(patch).eq("id", lead_id).execute()
        logger.info("Lead %s enriched. verified=%s fields=%s", lead_id, result.verified, list(result.custom_fields.keys()))
    except Exception as exc:
        logger.error("Failed to write enrichment results for lead %s: %s", lead_id, exc)
        raise


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def enrich_lead(lead_id: str, client_id: str) -> EnrichmentResult:
    """
    Run the full enrichment pipeline for one lead.

    Raises ValueError if the lead is not found or doesn't belong to client_id.
    Partial failures (individual provider errors) are captured in result.errors
    and do not abort the pipeline.

    Returns the populated EnrichmentResult.
    """
    sb = _sb()

    # Load lead row
    resp = (
        sb.table("leads")
        .select("*")
        .eq("id", lead_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise ValueError(f"Lead {lead_id} not found for client {client_id}")

    lead_row = rows[0]
    email    = lead_row.get("email", "").strip().lower()
    domain   = lead_row.get("domain") or (email.split("@")[1] if "@" in email else "")

    result = EnrichmentResult(lead_id=lead_id, email=email, domain=domain)

    # Step 1 — Email verification
    logger.info("Enriching lead %s (%s) — step 1: email verification", lead_id, email)
    try:
        verify_email(email, result)
    except Exception as exc:
        logger.error("Step 1 failed for lead %s: %s", lead_id, exc)
        result.errors.append(f"verification_error: {exc}")
        result.verified = None

    # Step 2 — Company enrichment
    logger.info("Enriching lead %s — step 2: company enrichment", lead_id)
    try:
        enrich_company(lead_row, result)
    except Exception as exc:
        logger.error("Step 2 failed for lead %s: %s", lead_id, exc)
        result.errors.append(f"enrichment_error: {exc}")

    # Step 3 — Personalised opener
    logger.info("Enriching lead %s — step 3: opener generation", lead_id)
    try:
        generate_opener(lead_row, result)
    except Exception as exc:
        logger.error("Step 3 failed for lead %s: %s", lead_id, exc)
        result.errors.append(f"opener_error: {exc}")

    # Write back to DB
    _flush_to_db(sb, lead_row, result)

    return result
