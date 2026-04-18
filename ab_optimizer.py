"""
ab_optimizer.py

Automatic A/B test winner detection and implementation.

Runs every 6 hours. For each active A/B test:
  - Pulls send counts and open/reply rates per variant from email_events
  - Runs a two-proportion z-test (α=0.05, z threshold = 1.96)
  - Minimum sample: MIN_SENDS_PER_VARIANT = 50 (as specified)
  - If winner found: promotes winning variant, retires loser, creates notification
  - If 500+ total sends with no significance: declares inconclusive, keeps variant A

Statistical test:
    p_pool = (x1 + x2) / (n1 + n2)
    z = (p1 - p2) / sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
    Significant if |z| > 1.96 (two-sided, 95% confidence)

Run standalone:
    python ab_optimizer.py

Or call run_ab_optimization() from an n8n HTTP trigger.
"""

import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

MIN_SENDS_PER_VARIANT: int   = 50
MAX_SENDS_INCONCLUSIVE: int  = 500
Z_THRESHOLD:            float = 1.96   # 95% confidence, two-sided
POLL_INTERVAL_SECONDS:  int   = 21600  # 6 hours


def _sb() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Statistical test
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """CDF of the standard normal distribution (math.erf implementation)."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def two_proportion_z_test(
    n1: int, x1: int,
    n2: int, x2: int,
) -> dict:
    """
    Two-proportion z-test comparing success rates p1 = x1/n1 and p2 = x2/n2.

    Args:
        n1: Total sends for variant A.
        x1: Replies (successes) for variant A.
        n2: Total sends for variant B.
        x2: Replies (successes) for variant B.

    Returns dict:
        significant (bool), winner ('A'|'B'|None), z_score, p_value,
        confidence_pct, rate_a, rate_b, reason (str)
    """
    p1 = x1 / n1 if n1 > 0 else 0.0
    p2 = x2 / n2 if n2 > 0 else 0.0

    result = {
        "n_a": n1, "n_b": n2,
        "replies_a": x1, "replies_b": x2,
        "rate_a": round(p1, 4),
        "rate_b": round(p2, 4),
        "z_score": 0.0,
        "p_value": 1.0,
        "confidence_pct": 0.0,
        "significant": False,
        "winner": None,
        "reason": "",
    }

    if n1 < MIN_SENDS_PER_VARIANT or n2 < MIN_SENDS_PER_VARIANT:
        result["reason"] = (
            f"Insufficient data: need {MIN_SENDS_PER_VARIANT} sends per variant "
            f"(A={n1}, B={n2})."
        )
        return result

    p_pool = (x1 + x2) / (n1 + n2)

    if p_pool in (0.0, 1.0):
        result["reason"] = "Degenerate rates: all successes or all failures."
        return result

    se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2))
    if se == 0.0:
        result["reason"] = "Zero standard error — rates are identical."
        return result

    z = (p1 - p2) / se
    p_value = 2.0 * (1.0 - _norm_cdf(abs(z)))

    result["z_score"]       = round(z, 4)
    result["p_value"]       = round(p_value, 4)
    result["confidence_pct"] = round((1.0 - p_value) * 100.0, 1)
    result["significant"]   = abs(z) > Z_THRESHOLD

    if result["significant"]:
        result["winner"] = "A" if p1 > p2 else "B"
        result["reason"] = (
            f"Variant {result['winner']} wins with {result['confidence_pct']}% confidence "
            f"(reply rate A={p1:.1%}, B={p2:.1%}, z={z:.2f})."
        )
    else:
        result["reason"] = (
            f"No significant difference yet "
            f"(reply rate A={p1:.1%}, B={p2:.1%}, z={z:.2f}, p={p_value:.3f}). "
            f"Need |z| > {Z_THRESHOLD} for significance."
        )

    return result


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_active_ab_tests(sb: Client) -> list[dict]:
    """
    Return all sequence steps that have an active A/B test.

    A test is active when:
      - Two rows exist for the same (campaign_id, step_number)
        with ab_variant = 'A' and ab_variant = 'B'
      - Neither variant has ab_variant = 'retired'
      - The parent campaign status = 'active'
    """
    resp = (
        sb.table("sequence_steps")
        .select("id, campaign_id, step_number, ab_variant, ab_weight, subject, body")
        .in_("ab_variant", ["A", "B"])
        .execute()
    )
    steps = resp.data or []

    # Group by (campaign_id, step_number)
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for step in steps:
        key = (step["campaign_id"], step["step_number"])
        grouped[key].append(step)

    # Keep only complete A+B pairs
    tests: list[dict] = []
    for (campaign_id, step_number), variants in grouped.items():
        a_rows = [v for v in variants if v.get("ab_variant") == "A"]
        b_rows = [v for v in variants if v.get("ab_variant") == "B"]
        if a_rows and b_rows:
            tests.append({
                "campaign_id":  campaign_id,
                "step_number":  step_number,
                "variant_a":    a_rows[0],
                "variant_b":    b_rows[0],
            })

    # Filter by active campaigns
    if not tests:
        return []

    campaign_ids = list({t["campaign_id"] for t in tests})
    active_resp = (
        sb.table("campaigns")
        .select("id")
        .in_("id", campaign_ids)
        .eq("status", "active")
        .execute()
    )
    active_ids = {r["id"] for r in (active_resp.data or [])}

    return [t for t in tests if t["campaign_id"] in active_ids]


def load_variant_metrics(
    sb: Client,
    campaign_id: str,
    step_id_a: str,
    step_id_b: str,
) -> tuple[int, int, int, int]:
    """
    Return (sends_a, replies_a, sends_b, replies_b) from email_events.

    Counts email_events rows where sequence_step_id matches each variant.
    """
    events_resp = (
        sb.table("email_events")
        .select("sequence_step_id, event_type, ab_variant")
        .eq("campaign_id", campaign_id)
        .in_("ab_variant", ["A", "B"])
        .execute()
    )
    events = events_resp.data or []

    sends_a = replies_a = sends_b = replies_b = 0
    for e in events:
        variant    = e.get("ab_variant")
        event_type = e.get("event_type")
        step_id    = e.get("sequence_step_id")

        if step_id == step_id_a or variant == "A":
            if event_type == "sent":
                sends_a += 1
            elif event_type == "replied":
                replies_a += 1
        elif step_id == step_id_b or variant == "B":
            if event_type == "sent":
                sends_b += 1
            elif event_type == "replied":
                replies_b += 1

    return sends_a, replies_a, sends_b, replies_b


# ---------------------------------------------------------------------------
# Action — promote winner / retire loser
# ---------------------------------------------------------------------------

def _promote_winner(
    sb: Client,
    test: dict,
    winner: str,
    z_result: dict,
) -> None:
    """
    Promote the winning variant and retire the losing one.

    - Losing variant: ab_variant = 'retired'
    - Winning variant: ab_variant set to None (becomes the canonical step)
    - All future campaign_leads for this step get the winning variant
    - Creates 'ab_winner' notification
    """
    winning_step  = test["variant_a"] if winner == "A" else test["variant_b"]
    retiring_step = test["variant_b"] if winner == "A" else test["variant_a"]

    campaign_id = test["campaign_id"]
    step_number = test["step_number"]

    # Retire the loser
    try:
        sb.table("sequence_steps").update({
            "ab_variant": "retired",
        }).eq("id", retiring_step["id"]).execute()
    except Exception as exc:
        logger.error("Failed to retire variant %s: %s", retiring_step["id"], exc)
        return

    # Promote the winner (remove A/B label so it becomes the canonical step)
    try:
        sb.table("sequence_steps").update({
            "ab_variant": None,
            "ab_weight":  100,
        }).eq("id", winning_step["id"]).execute()
    except Exception as exc:
        logger.error("Failed to promote variant %s: %s", winning_step["id"], exc)
        return

    # Log decision to diagnostics_log
    try:
        sb.table("diagnostics_log").insert({
            "client_id":  None,
            "check_type": "ab_test",
            "entity_id":  winning_step["id"],
            "result":     "winner_declared",
            "details":    {
                "campaign_id":  campaign_id,
                "step_number":  step_number,
                "winner":       winner,
                "z_test":       z_result,
                "promoted_id":  winning_step["id"],
                "retired_id":   retiring_step["id"],
            },
            "timestamp":  _now_utc(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to log A/B decision: %s", exc)

    # Create notification (client_id fetched from campaign)
    try:
        camp_resp = sb.table("campaigns").select("client_id, name").eq("id", campaign_id).limit(1).execute()
        camp = (camp_resp.data or [{}])[0]
        client_id   = camp.get("client_id", "")
        camp_name   = camp.get("name", campaign_id)

        rate_win  = z_result[f"rate_{'a' if winner == 'A' else 'b'}"]
        rate_lose = z_result[f"rate_{'b' if winner == 'A' else 'a'}"]
        n_win     = z_result[f"n_{'a' if winner == 'A' else 'b'}"]
        n_lose    = z_result[f"n_{'b' if winner == 'A' else 'a'}"]

        message = (
            f"A/B test winner: Variant {winner} wins for step {step_number} "
            f"in campaign '{camp_name}'. "
            f"Reply rate {rate_win:.1%} vs {rate_lose:.1%} "
            f"({n_win} vs {n_lose} sends, {z_result['confidence_pct']}% confidence). "
            f"Losing variant retired. All future sends will use the winning variant."
        )
        sb.table("notifications").insert({
            "client_id": client_id,
            "type":      "ab_winner",
            "message":   message,
            "priority":  "normal",
            "read":      False,
            "timestamp": _now_utc(),
        }).execute()

        logger.info("A/B winner declared: variant %s for campaign %s step %d", winner, campaign_id, step_number)
    except Exception as exc:
        logger.warning("Failed to create ab_winner notification: %s", exc)


def _declare_inconclusive(sb: Client, test: dict, z_result: dict, total_sends: int) -> None:
    """
    Declare the A/B test inconclusive after MAX_SENDS_INCONCLUSIVE total sends.

    Keeps variant A as default by retiring variant B.
    """
    campaign_id = test["campaign_id"]
    step_number = test["step_number"]

    try:
        sb.table("sequence_steps").update({"ab_variant": "retired"}).eq("id", test["variant_b"]["id"]).execute()
        sb.table("sequence_steps").update({"ab_variant": None, "ab_weight": 100}).eq("id", test["variant_a"]["id"]).execute()
    except Exception as exc:
        logger.error("Failed to close inconclusive test: %s", exc)
        return

    try:
        sb.table("diagnostics_log").insert({
            "client_id":  None,
            "check_type": "ab_test",
            "entity_id":  test["variant_a"]["id"],
            "result":     "inconclusive",
            "details": {
                "campaign_id":  campaign_id,
                "step_number":  step_number,
                "total_sends":  total_sends,
                "z_test":       z_result,
            },
            "timestamp": _now_utc(),
        }).execute()

        camp_resp = sb.table("campaigns").select("client_id, name").eq("id", campaign_id).limit(1).execute()
        camp      = (camp_resp.data or [{}])[0]
        client_id = camp.get("client_id", "")
        camp_name = camp.get("name", campaign_id)

        message = (
            f"A/B test inconclusive for step {step_number} in campaign '{camp_name}'. "
            f"No statistically significant difference found after {total_sends} total sends "
            f"(reply rate A={z_result['rate_a']:.1%}, B={z_result['rate_b']:.1%}). "
            "Variant A kept as default. Variant B retired."
        )
        sb.table("notifications").insert({
            "client_id": client_id,
            "type":      "ab_winner",
            "message":   message,
            "priority":  "normal",
            "read":      False,
            "timestamp": _now_utc(),
        }).execute()

        logger.info("A/B test inconclusive for campaign %s step %d (%d sends)", campaign_id, step_number, total_sends)
    except Exception as exc:
        logger.warning("Failed to log inconclusive result: %s", exc)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run_ab_optimization(sb: Optional[Client] = None) -> list[dict]:
    """
    Check all active A/B tests and auto-declare winners where significance is reached.

    Returns a list of result summaries for each test evaluated.
    """
    if sb is None:
        sb = _sb()

    tests = load_active_ab_tests(sb)
    if not tests:
        logger.info("No active A/B tests found.")
        return []

    logger.info("Evaluating %d active A/B test(s)...", len(tests))
    results: list[dict] = []

    for test in tests:
        campaign_id = test["campaign_id"]
        step_number = test["step_number"]
        va          = test["variant_a"]
        vb          = test["variant_b"]

        try:
            n_a, x_a, n_b, x_b = load_variant_metrics(sb, campaign_id, va["id"], vb["id"])
        except Exception as exc:
            logger.error("Failed to load metrics for %s step %d: %s", campaign_id, step_number, exc)
            continue

        total_sends = n_a + n_b
        z_result    = two_proportion_z_test(n_a, x_a, n_b, x_b)

        summary = {
            "campaign_id":  campaign_id,
            "step_number":  step_number,
            "variant_a_id": va["id"],
            "variant_b_id": vb["id"],
            "total_sends":  total_sends,
            **z_result,
        }

        if z_result["significant"] and z_result["winner"]:
            _promote_winner(sb, test, z_result["winner"], z_result)
            summary["action"] = "winner_declared"
        elif total_sends >= MAX_SENDS_INCONCLUSIVE and not z_result["significant"]:
            _declare_inconclusive(sb, test, z_result, total_sends)
            summary["action"] = "inconclusive"
        else:
            summary["action"] = "waiting"
            logger.info(
                "A/B test campaign %s step %d: waiting (A=%d sends, B=%d sends) — %s",
                campaign_id, step_number, n_a, n_b, z_result["reason"],
            )

        results.append(summary)

    return results


def main() -> None:
    """Entry point for standalone loop (every 6 hours)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.critical("SUPABASE_URL or SUPABASE_KEY not set. Exiting.")
        raise SystemExit(1)

    logger.info("A/B optimizer started. Poll interval: %ds (6h)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_ab_optimization()
        except Exception as exc:
            logger.error("Unhandled A/B optimization error: %s", exc)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
