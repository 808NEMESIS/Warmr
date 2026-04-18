"""
content_scorer.py

Two-layer email spam / deliverability scorer for Warmr.

Layer 1 — Rule-based (instant, no API call):
    Checks subject + body against 150+ Dutch and English spam trigger words,
    structural signals (ALL CAPS ratio, exclamation marks, link density,
    HTML ratio, missing reply-to, subject length extremes, body word count,
    tracking pixels, unsubscribe link presence).
    Returns a score 0–100 and a dict of triggered flags.

Layer 2 — Claude Sonnet deep analysis (on request):
    Sends subject + body to Claude Sonnet claude-sonnet-4-6.
    Returns: spam_score (0–100), specific flags, rewrite suggestions,
    tone assessment.

Overall score = 0.4 × rule_score + 0.6 × claude_score (when both available).

Usage:
    from content_scorer import score_content, deep_score_content, save_content_score

    # Quick rule-based check
    result = score_content(subject, body, html_body)

    # Deep AI analysis
    result = deep_score_content(subject, body, html_body)

    # Persist to DB
    save_content_score(sb, client_id, result, campaign_id, step_id)
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv
from supabase import Client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ANTHROPIC_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
SONNET_MODEL:  str = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Spam word lists  (Dutch + English, 150+ total)
# ---------------------------------------------------------------------------

SPAM_WORDS_NL: frozenset[str] = frozenset({
    # Urgentie / actie
    "actie nu", "beperkte tijd", "nu kopen", "koop nu", "bestel nu",
    "aanbieding", "gratis", "goedkoop", "korting", "sale", "uitverkoop",
    "tijdelijk aanbod", "bespaar nu", "niet missen", "vandaag nog",
    "direct beschikbaar", "snel", "haast je", "laatste kans",
    # Geld / financieel
    "verdien geld", "extra inkomen", "passief inkomen", "snel geld",
    "geld verdienen", "investering", "rendement", "winst gegarandeerd",
    "rijkdom", "financiële vrijheid", "schuldenvrij", "lening",
    "krediet", "casino", "jackpot", "prijs gewonnen", "loterij",
    # Spam signalen
    "klik hier", "klik nu", "bezoek onze website", "lees meer",
    "geen risico", "risicovrij", "100% garantie", "gegarandeerd",
    "bewezen methode", "geheim", "verborgen", "exclusief aanbod",
    "uitnodiging", "profiteer nu", "maximale resultaten",
    "afmelden", "uitschrijven", "geen spam",
    # Medisch / supplement
    "afvallen", "gewichtsverlies", "pillen", "supplement",
    "wondermiddel", "snelle oplossing",
    # Misleidend
    "urgent", "dringend", "waarschuwing", "account geblokkeerd",
    "bevestig uw gegevens", "inloggen vereist", "uw account",
})

SPAM_WORDS_EN: frozenset[str] = frozenset({
    # Urgency / action
    "act now", "limited time", "buy now", "order now", "click here",
    "click now", "call now", "don't miss", "last chance",
    "today only", "urgent", "hurry", "limited offer",
    # Money / financial
    "earn money", "extra income", "passive income", "fast cash",
    "make money", "investment opportunity", "guaranteed returns",
    "financial freedom", "debt free", "get paid", "winner",
    "you won", "prize", "lottery", "casino", "jackpot",
    "million dollars", "billion",
    # Spam signals
    "free", "free!", "free offer", "no cost", "at no cost",
    "100% free", "absolutely free", "risk free", "risk-free",
    "no risk", "guarantee", "guaranteed", "no obligation",
    "satisfaction guaranteed", "money back", "no questions asked",
    "proven", "secret", "hidden", "exclusive deal",
    "special promotion", "special offer", "amazing deal",
    "once in a lifetime", "never before", "unbelievable",
    "incredible offer", "unsubscribe", "remove from list",
    # Medical / supplement
    "lose weight", "weight loss", "diet", "pills", "supplements",
    "miracle", "cure", "remedy", "anti-aging",
    # Misleading / phishing
    "account suspended", "verify your account", "confirm your details",
    "update your information", "your account has been", "dear friend",
    "dear customer", "dear user", "dear beneficiary",
    "congratulations you", "you have been selected",
    # MLM / scam
    "work from home", "be your own boss", "unlimited earning",
    "join our team", "ground floor opportunity", "network marketing",
    "multi-level", "referral bonus", "pyramid", "downline",
    # Excessive punctuation patterns
    "!!!",
})

ALL_SPAM_WORDS: frozenset[str] = SPAM_WORDS_NL | SPAM_WORDS_EN


# ---------------------------------------------------------------------------
# Rule-based scoring
# ---------------------------------------------------------------------------

def _count_links(text: str) -> int:
    """Count http/https URLs in text."""
    return len(re.findall(r"https?://\S+", text))


def _count_tracking_pixels(html: str) -> int:
    """Count likely tracking pixels (1×1 img tags) in HTML."""
    if not html:
        return 0
    # Pattern: <img ... width="1" ... height="1" ...>
    return len(re.findall(
        r'<img[^>]+(?:width=["\']?1["\']?[^>]+height=["\']?1["\']?|height=["\']?1["\']?[^>]+width=["\']?1["\']?)[^>]*>',
        html, re.IGNORECASE,
    ))


def _html_ratio(body: str, html_body: str) -> float:
    """Return ratio of HTML content to plain text length."""
    plain_len = len(body or "")
    html_len  = len(html_body or "")
    if not plain_len and not html_len:
        return 0.0
    if not plain_len:
        return 1.0
    return html_len / (plain_len + html_len)


def run_rule_based_check(
    subject: str,
    body: str,
    html_body: Optional[str] = None,
) -> dict:
    """
    Run all rule-based spam checks on subject + body.

    Returns:
        {
            "score": float (0–100, higher = spammier),
            "flags": {flag_name: value},
            "triggered_words": [str],
        }
    """
    flags: dict = {}
    penalty: float = 0.0

    subject_lower = (subject or "").lower()
    body_lower    = (body or "").lower()
    combined      = subject_lower + " " + body_lower

    # ── Spam word detection ──────────────────────────────────────────────
    triggered_words: list[str] = []
    for word in ALL_SPAM_WORDS:
        if word.lower() in combined:
            triggered_words.append(word)
    word_penalty = min(len(triggered_words) * 5, 40)
    penalty += word_penalty
    if triggered_words:
        flags["spam_words"] = triggered_words

    # ── ALL CAPS ratio (subject) ─────────────────────────────────────────
    subj_letters = [c for c in (subject or "") if c.isalpha()]
    if len(subj_letters) >= 5:
        caps_ratio = sum(1 for c in subj_letters if c.isupper()) / len(subj_letters)
        if caps_ratio > 0.5:
            penalty += 15
            flags["excessive_caps_subject"] = round(caps_ratio, 2)

    # ── Exclamation marks ────────────────────────────────────────────────
    excl_count = combined.count("!")
    if excl_count >= 3:
        penalty += min(excl_count * 2, 15)
        flags["exclamation_marks"] = excl_count

    # ── Link count ───────────────────────────────────────────────────────
    link_count = _count_links(body or "")
    if html_body:
        link_count = max(link_count, _count_links(html_body))
    if link_count > 3:
        penalty += min((link_count - 3) * 5, 20)
        flags["link_count"] = link_count

    # ── HTML ratio ───────────────────────────────────────────────────────
    if html_body:
        ratio = _html_ratio(body, html_body)
        if ratio > 0.8:
            penalty += 10
            flags["html_heavy"] = round(ratio, 2)

    # ── Subject length ───────────────────────────────────────────────────
    subj_len = len(subject or "")
    if subj_len == 0:
        penalty += 20
        flags["missing_subject"] = True
    elif subj_len > 80:
        penalty += 10
        flags["subject_too_long"] = subj_len
    elif subj_len < 5:
        penalty += 10
        flags["subject_too_short"] = subj_len

    # ── Body word count ──────────────────────────────────────────────────
    word_count = len((body or "").split())
    if word_count < 20:
        penalty += 10
        flags["body_too_short"] = word_count
    elif word_count > 600:
        penalty += 8
        flags["body_too_long"] = word_count

    # ── Tracking pixels ──────────────────────────────────────────────────
    if html_body:
        pixels = _count_tracking_pixels(html_body)
        if pixels > 0:
            penalty += 10
            flags["tracking_pixels"] = pixels

    # ── Unsubscribe link ─────────────────────────────────────────────────
    if re.search(r"unsubscri|afmelden|uitschrijven", combined, re.IGNORECASE):
        flags["has_unsubscribe"] = True
        # Not always bad — required for bulk email, but odd for cold 1:1
        # Just flag it, no penalty

    # ── ALL CAPS words in body ───────────────────────────────────────────
    caps_words = re.findall(r'\b[A-Z]{3,}\b', body or "")
    caps_words = [w for w in caps_words if w not in {"CEO", "CFO", "CTO", "CMO", "HR", "KPI", "ROI", "API", "B2B", "B2C", "SaaS", "CRM", "ERP", "URL", "FAQ"}]
    if len(caps_words) > 3:
        penalty += 8
        flags["caps_words_in_body"] = caps_words[:10]

    score = min(round(penalty, 1), 100.0)

    return {
        "score":           score,
        "flags":           flags,
        "triggered_words": triggered_words,
    }


# ---------------------------------------------------------------------------
# Claude Sonnet deep analysis
# ---------------------------------------------------------------------------

_DEEP_ANALYSIS_PROMPT = """You are an expert cold email deliverability analyst.
Analyse the following B2B cold email for spam risk and deliverability issues.

Subject: {subject}

Body:
{body}

Provide your analysis in EXACTLY this JSON format (return valid JSON only, no markdown):

{{
  "spam_score": <integer 0-100, where 0=perfect deliverability, 100=certain spam>,
  "tone": "<professional|neutral|pushy|spammy>",
  "flags": [
    "<specific issue 1>",
    "<specific issue 2>"
  ],
  "suggestions": [
    "<specific rewrite suggestion 1>",
    "<specific rewrite suggestion 2>",
    "<specific rewrite suggestion 3>"
  ],
  "rewritten_subject": "<improved subject line, max 60 chars>",
  "summary": "<1 sentence summary of the main deliverability risk>"
}}

Be specific and actionable. Flags should identify concrete problems. Suggestions should be implementable immediately."""


def run_claude_analysis(
    subject: str,
    body: str,
    supabase_client=None,
    client_id: str | None = None,
) -> Optional[dict]:
    """
    Run Claude Sonnet deep spam analysis on an email.

    Returns dict with: spam_score, tone, flags, suggestions, rewritten_subject, summary.
    Returns None if the API call fails.

    If supabase_client is provided, uses tracked_claude_call for cost tracking.
    """
    if not ANTHROPIC_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping deep analysis.")
        return None

    prompt = _DEEP_ANALYSIS_PROMPT.format(
        subject = subject or "(no subject)",
        body    = (body or "")[:3000],  # cap at 3000 chars to control tokens
    )

    try:
        import json
        client  = Anthropic(api_key=ANTHROPIC_KEY)
        messages = [{"role": "user", "content": prompt}]

        if supabase_client:
            from utils.cost_tracker import tracked_claude_call
            message = tracked_claude_call(
                client, supabase_client,
                model=SONNET_MODEL,
                messages=messages,
                max_tokens=600,
                context="content_scoring",
                client_id=client_id,
            )
        else:
            message = client.messages.create(
                model      = SONNET_MODEL,
                max_tokens = 600,
                messages   = messages,
            )
        raw = message.content[0].text.strip()

        # Strip markdown code blocks if present
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        parsed = json.loads(raw)
        return {
            "spam_score":        int(parsed.get("spam_score", 50)),
            "tone":              parsed.get("tone", "neutral"),
            "flags":             parsed.get("flags", []),
            "suggestions":       parsed.get("suggestions", []),
            "rewritten_subject": parsed.get("rewritten_subject", ""),
            "summary":           parsed.get("summary", ""),
        }

    except Exception as exc:
        logger.error("Claude Sonnet deep analysis failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Combined scoring
# ---------------------------------------------------------------------------

def score_content(
    subject: str,
    body: str,
    html_body: Optional[str] = None,
) -> dict:
    """
    Run rule-based spam check only (instant, no API call).

    Returns dict with rule_based score, flags, and triggered_words.
    """
    rule = run_rule_based_check(subject, body, html_body)
    return {
        "rule_based_score": rule["score"],
        "rule_based_flags": rule["flags"],
        "triggered_words":  rule["triggered_words"],
        "overall_score":    rule["score"],
        "claude_score":     None,
        "claude_flags":     None,
        "claude_suggestions": None,
    }


def deep_score_content(
    subject: str,
    body: str,
    html_body: Optional[str] = None,
) -> dict:
    """
    Run both rule-based check AND Claude Sonnet deep analysis.

    Overall score = 40% rule + 60% Claude when both available.
    Falls back to rule-only if Claude fails.

    Returns merged dict with all fields from both layers.
    """
    rule   = run_rule_based_check(subject, body, html_body)
    claude = run_claude_analysis(subject, body)

    rule_score = rule["score"]
    if claude:
        claude_score  = float(claude["spam_score"])
        overall_score = round(0.4 * rule_score + 0.6 * claude_score, 1)
    else:
        claude_score  = None
        overall_score = rule_score

    return {
        "rule_based_score":   rule_score,
        "rule_based_flags":   rule["flags"],
        "triggered_words":    rule["triggered_words"],
        "claude_score":       claude_score,
        "claude_flags":       claude.get("flags") if claude else None,
        "claude_suggestions": claude.get("suggestions") if claude else None,
        "claude_tone":        claude.get("tone") if claude else None,
        "rewritten_subject":  claude.get("rewritten_subject") if claude else None,
        "claude_summary":     claude.get("summary") if claude else None,
        "overall_score":      overall_score,
    }


# ---------------------------------------------------------------------------
# Persist to database
# ---------------------------------------------------------------------------

def save_content_score(
    sb: Client,
    client_id: str,
    result: dict,
    subject: str,
    campaign_id: Optional[str] = None,
    sequence_step_id: Optional[str] = None,
) -> Optional[str]:
    """
    Persist a scoring result to the content_scores table.

    Returns the new record ID, or None on failure.
    """
    try:
        row = sb.table("content_scores").insert({
            "client_id":          client_id,
            "campaign_id":        campaign_id,
            "sequence_step_id":   sequence_step_id,
            "subject":            subject,
            "rule_based_score":   result.get("rule_based_score"),
            "rule_based_flags":   result.get("rule_based_flags"),
            "claude_score":       result.get("claude_score"),
            "claude_flags":       result.get("claude_flags"),
            "claude_suggestions": result.get("claude_suggestions"),
            "overall_score":      result.get("overall_score"),
            "created_at":         datetime.now(timezone.utc).isoformat(),
        }).execute()
        return row.data[0]["id"] if row.data else None
    except Exception as exc:
        logger.error("Failed to save content score: %s", exc)
        return None
