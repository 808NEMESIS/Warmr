"""
reply_classifier.py — Classify incoming replies via Claude Haiku.

Categories:
  interested     — wants more info or a meeting
  not_interested — explicit lack of interest
  out_of_office  — auto-reply or absence
  referral       — refers to another person
  unsubscribe    — wants no further contact
  question       — has a question
  other          — anything else

Called by imap_processor.py when a real (non-warmup) reply is detected.
"""

import logging
import os
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

CATEGORIES = (
    "interested",
    "not_interested",
    "out_of_office",
    "referral",
    "unsubscribe",
    "question",
    "other",
)


def classify_reply(
    reply_body: str,
    reply_subject: str = "",
    original_subject: str = "",
    supabase_client=None,
    client_id: str | None = None,
) -> str:
    """
    Classify a reply email using Claude Haiku.

    Returns one of the CATEGORIES strings.
    Falls back to 'other' if classification fails.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — defaulting to 'other'.")
        return "other"

    # Quick heuristics for obvious cases (skip API call)
    body_lower = reply_body.lower().strip()

    # Out of office patterns
    ooo_signals = [
        "out of office", "afwezig", "niet aanwezig", "op vakantie",
        "automatic reply", "automatisch antwoord", "auto-reply",
        "i am currently out", "ik ben momenteel niet",
    ]
    if any(s in body_lower for s in ooo_signals):
        return "out_of_office"

    # Unsubscribe patterns
    unsub_signals = [
        "uitschrijven", "unsubscribe", "verwijder mij", "remove me",
        "stop met mailen", "niet meer contacteren", "geen interesse meer",
    ]
    if any(s in body_lower for s in unsub_signals):
        return "unsubscribe"

    # Very short negative
    if len(body_lower) < 30 and any(w in body_lower for w in ["nee", "no", "niet geïnteresseerd", "no thanks"]):
        return "not_interested"

    # API call for ambiguous cases
    prompt = f"""Classify this email reply into exactly ONE category.

Categories:
- interested: wants more info, a meeting, or shows positive engagement
- not_interested: explicitly says no, not relevant, wrong person
- out_of_office: auto-reply, vacation, absence notice
- referral: suggests contacting someone else
- unsubscribe: wants to stop receiving emails
- question: asks a question (neutral, not clearly positive or negative)
- other: doesn't fit any category

Subject: {reply_subject}
Original subject: {original_subject}

Reply body:
\"\"\"
{reply_body[:500]}
\"\"\"

Return ONLY the category name, nothing else."""

    try:
        if supabase_client:
            from utils.cost_tracker import tracked_claude_call
            client = anthropic.Anthropic(api_key=api_key)
            message = tracked_claude_call(
                client, supabase_client,
                model="claude-haiku-4-5-20251001",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=20,
                context="reply_classification",
                client_id=client_id,
            )
        else:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=20,
                messages=[{"role": "user", "content": prompt}],
            )

        result = message.content[0].text.strip().lower()

        # Validate result
        if result in CATEGORIES:
            logger.info("Reply classified as '%s': %s", result, reply_subject[:50])
            return result

        # Fuzzy match
        for cat in CATEGORIES:
            if cat in result:
                return cat

        logger.warning("Unexpected classification '%s' — defaulting to 'other'.", result)
        return "other"

    except Exception as exc:
        logger.error("Reply classification failed: %s", exc)
        return "other"


# ---------------------------------------------------------------------------
# Richer classifier — returns category + meeting_intent + urgency in one call.
# Used by imap_processor when it stores a reply. Falls back to the legacy
# classify_reply() on any failure so the pipeline is never blocked.
# ---------------------------------------------------------------------------

URGENCIES = ("low", "medium", "high")


def classify_reply_with_signals(
    reply_body: str,
    reply_subject: str = "",
    original_subject: str = "",
    supabase_client=None,
    client_id: str | None = None,
) -> dict:
    """
    Single-call enrichment of an incoming reply.

    Returns:
        {
          "category":       one of CATEGORIES,
          "meeting_intent": bool,   # explicit booking / calendar signal
          "urgency":        "low" | "medium" | "high",
          "rationale":      short human-readable string,
        }

    Costs one Claude Haiku call. Falls back to `classify_reply()` if the
    model call fails — in that case meeting_intent=False, urgency="low".
    """
    fallback = {
        "category":       "other",
        "meeting_intent": False,
        "urgency":        "low",
        "rationale":      "",
    }

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        fallback["category"] = classify_reply(reply_body, reply_subject, original_subject,
                                              supabase_client, client_id)
        return fallback

    prompt = f"""Analyze this B2B email reply on three dimensions and return strict JSON.

Subject: {reply_subject}
Original subject: {original_subject}

Reply body:
\"\"\"
{reply_body[:1200]}
\"\"\"

Return ONLY this JSON shape (no markdown, no prose):
{{
  "category": one of ["interested","not_interested","out_of_office","referral","unsubscribe","question","other"],
  "meeting_intent": true|false,
  "urgency": "low"|"medium"|"high",
  "rationale": "one short sentence"
}}

Rules:
- meeting_intent=true when the reply explicitly asks for a call/meeting/demo, shares availability, or requests a booking link.
- urgency="high" only for explicit time pressure ("this week", "asap", "today"), "medium" if the sender expects a reply soon, else "low".
- category "interested" and meeting_intent=true can coexist."""

    try:
        import json as _json
        if supabase_client:
            from utils.cost_tracker import tracked_claude_call
            client = anthropic.Anthropic(api_key=api_key)
            message = tracked_claude_call(
                client, supabase_client,
                model="claude-haiku-4-5-20251001",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=250,
                context="reply_classification_signals",
                client_id=client_id,
            )
        else:
            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{"role": "user", "content": prompt}],
            )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])
        parsed = _json.loads(raw)

        category = str(parsed.get("category", "other")).lower()
        if category not in CATEGORIES:
            category = "other"
        urgency = str(parsed.get("urgency", "low")).lower()
        if urgency not in URGENCIES:
            urgency = "low"

        return {
            "category":       category,
            "meeting_intent": bool(parsed.get("meeting_intent", False)),
            "urgency":        urgency,
            "rationale":      str(parsed.get("rationale", ""))[:300],
        }

    except Exception as exc:
        logger.warning("Signal-rich classification failed (%s) — falling back to legacy path.", exc)
        fallback["category"] = classify_reply(reply_body, reply_subject, original_subject,
                                              supabase_client, client_id)
        return fallback
