"""
ab_test_engine.py

A/B test variant selection and statistical significance analysis for campaign sequences.

How A/B testing works in Warmr:
  - A sequence step with variants has two rows in sequence_steps:
      (campaign_id, step_number=2, ab_variant='A', ab_weight=60)
      (campaign_id, step_number=2, ab_variant='B', ab_weight=40)
  - For each lead, select_variant() deterministically picks A or B based on
    a hash of the lead_id (same lead always gets the same variant).
  - email_events records which variant was sent (ab_variant field).
  - find_winning_variant() queries email_events, runs a two-proportion z-test
    on reply rates, and reports the winner once 100+ sends exist per variant.

Statistical method: two-proportion z-test, two-sided, α = 0.05 (95% confidence).
Minimum sample requirement: MIN_SENDS_PER_VARIANT = 100 (from CLAUDE.md).
"""

import hashlib
import math
import logging
from typing import Optional

from supabase import Client

logger = logging.getLogger(__name__)

MIN_SENDS_PER_VARIANT: int = 100
SIGNIFICANCE_LEVEL: float = 0.05  # α for two-sided test


# ---------------------------------------------------------------------------
# Variant selection
# ---------------------------------------------------------------------------

def select_variant(
    step_rows: list[dict],
    lead_id: str,
) -> dict:
    """
    Choose which sequence step row to use for a given lead.

    If the step has no A/B variants (all rows have ab_variant=None), return
    the single row unchanged.

    If A and B variants exist, select deterministically based on a hash of
    lead_id so that:
      - The same lead always receives the same variant.
      - Distribution across leads matches the ab_weight percentages.

    Args:
        step_rows: All sequence_steps rows for this (campaign_id, step_number).
                   May contain one non-variant row, or two rows with ab_variant='A'/'B'.
        lead_id:   UUID string of the lead being processed.

    Returns:
        The chosen sequence_steps dict row.
    """
    if not step_rows:
        raise ValueError("step_rows must not be empty.")

    ab_rows = [r for r in step_rows if r.get("ab_variant") in ("A", "B")]

    # No A/B test configured for this step
    if not ab_rows:
        return step_rows[0]

    step_a = next((r for r in ab_rows if r.get("ab_variant") == "A"), None)
    step_b = next((r for r in ab_rows if r.get("ab_variant") == "B"), None)

    # Degenerate: only one variant row found — return it
    if not step_a:
        return step_b  # type: ignore[return-value]
    if not step_b:
        return step_a

    # Hash lead_id to a 0–99 bucket (uniform distribution)
    digest = hashlib.sha256(lead_id.encode("utf-8")).hexdigest()
    bucket = int(digest, 16) % 100

    weight_a: int = int(step_a.get("ab_weight") or 50)
    return step_a if bucket < weight_a else step_b


# ---------------------------------------------------------------------------
# Statistical significance
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """
    Cumulative distribution function of the standard normal distribution.

    Implemented via math.erf to avoid a scipy dependency.
    Accurate to ~7 decimal places for |x| < 6.
    """
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def calculate_significance(
    n_a: int,
    replies_a: int,
    n_b: int,
    replies_b: int,
) -> dict:
    """
    Run a two-proportion z-test comparing reply rates for variants A and B.

    Args:
        n_a:       Total emails sent for variant A.
        replies_a: Emails that received a reply for variant A.
        n_b:       Total emails sent for variant B.
        replies_b: Emails that received a reply for variant B.

    Returns:
        Dict with keys:
          significant (bool)   — True if p_value < SIGNIFICANCE_LEVEL
          winner (str|None)    — 'A', 'B', or None (tie / insufficient data)
          z_score (float)
          p_value (float)
          confidence_pct (float) — (1 - p_value) * 100
          rate_a (float)       — reply rate for A
          rate_b (float)       — reply rate for B
          sends_a, sends_b (int)
          reason (str)         — human-readable explanation
    """
    base = {
        "sends_a": n_a,
        "sends_b": n_b,
        "replies_a": replies_a,
        "replies_b": replies_b,
        "rate_a": round(replies_a / n_a, 4) if n_a > 0 else 0.0,
        "rate_b": round(replies_b / n_b, 4) if n_b > 0 else 0.0,
        "z_score": 0.0,
        "p_value": 1.0,
        "confidence_pct": 0.0,
        "significant": False,
        "winner": None,
    }

    if n_a < MIN_SENDS_PER_VARIANT or n_b < MIN_SENDS_PER_VARIANT:
        base["reason"] = (
            f"Insufficient data: need {MIN_SENDS_PER_VARIANT} sends per variant "
            f"(A has {n_a}, B has {n_b})."
        )
        return base

    p_a = replies_a / n_a
    p_b = replies_b / n_b
    p_pooled = (replies_a + replies_b) / (n_a + n_b)

    base["rate_a"] = round(p_a, 4)
    base["rate_b"] = round(p_b, 4)

    if p_pooled == 0.0 or p_pooled == 1.0:
        base["reason"] = "Degenerate rates: no replies recorded or 100% reply rate."
        return base

    se = math.sqrt(p_pooled * (1.0 - p_pooled) * (1.0 / n_a + 1.0 / n_b))

    if se == 0.0:
        base["reason"] = "Zero standard error — rates are identical."
        return base

    z = (p_a - p_b) / se
    # Two-sided p-value
    p_value = 2.0 * (1.0 - _norm_cdf(abs(z)))

    base["z_score"] = round(z, 4)
    base["p_value"] = round(p_value, 4)
    base["confidence_pct"] = round((1.0 - p_value) * 100.0, 1)
    base["significant"] = p_value < SIGNIFICANCE_LEVEL

    if base["significant"]:
        base["winner"] = "A" if p_a > p_b else "B"
        base["reason"] = (
            f"Variant {base['winner']} wins with {base['confidence_pct']}% confidence "
            f"(reply rate A={p_a:.1%}, B={p_b:.1%})."
        )
    else:
        base["reason"] = (
            f"No significant difference yet (p={p_value:.3f}, need p<{SIGNIFICANCE_LEVEL}). "
            f"Reply rate A={p_a:.1%}, B={p_b:.1%}."
        )

    return base


# ---------------------------------------------------------------------------
# Supabase-backed analysis
# ---------------------------------------------------------------------------

def get_ab_results(
    supabase: Client,
    campaign_id: str,
    step_number: int,
) -> dict:
    """
    Query email_events and compile per-variant send and reply counts.

    Returns a dict:
      {
        'A': {'sends': int, 'replies': int},
        'B': {'sends': int, 'replies': int},
        'step_number': int,
        'campaign_id': str,
      }
    """
    # Fetch the sequence_step IDs for this step_number so we can filter events
    steps_resp = (
        supabase.table("sequence_steps")
        .select("id, ab_variant")
        .eq("campaign_id", campaign_id)
        .eq("step_number", step_number)
        .in_("ab_variant", ["A", "B"])
        .execute()
    )
    step_rows = steps_resp.data or []
    step_id_to_variant = {r["id"]: r.get("ab_variant") for r in step_rows}

    if not step_id_to_variant:
        return {
            "A": {"sends": 0, "replies": 0},
            "B": {"sends": 0, "replies": 0},
            "step_number": step_number,
            "campaign_id": campaign_id,
            "error": "No A/B variant steps found for this step_number.",
        }

    # Fetch relevant events
    events_resp = (
        supabase.table("email_events")
        .select("event_type, ab_variant, sequence_step_id")
        .eq("campaign_id", campaign_id)
        .in_("sequence_step_id", list(step_id_to_variant.keys()))
        .in_("event_type", ["sent", "replied"])
        .execute()
    )
    events = events_resp.data or []

    counts: dict[str, dict[str, int]] = {
        "A": {"sends": 0, "replies": 0},
        "B": {"sends": 0, "replies": 0},
    }

    for event in events:
        variant = event.get("ab_variant") or step_id_to_variant.get(event.get("sequence_step_id") or "")
        if variant not in ("A", "B"):
            continue
        if event["event_type"] == "sent":
            counts[variant]["sends"] += 1
        elif event["event_type"] == "replied":
            counts[variant]["replies"] += 1

    return {
        **counts,
        "step_number": step_number,
        "campaign_id": campaign_id,
    }


def find_winning_variant(
    supabase: Client,
    campaign_id: str,
    step_number: int,
) -> dict:
    """
    Full A/B analysis pipeline: fetch data → run significance test → return result.

    Returns the calculate_significance result dict enriched with raw counts and
    the campaign/step context.
    """
    results = get_ab_results(supabase, campaign_id, step_number)

    if "error" in results:
        return results

    significance = calculate_significance(
        n_a=results["A"]["sends"],
        replies_a=results["A"]["replies"],
        n_b=results["B"]["sends"],
        replies_b=results["B"]["replies"],
    )

    return {
        **significance,
        "campaign_id": campaign_id,
        "step_number": step_number,
        "raw": results,
    }
