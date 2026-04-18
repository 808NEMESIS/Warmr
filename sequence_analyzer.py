"""
sequence_analyzer.py

Weekly sequence performance analyzer for Warmr.

Runs every Monday at 07:00 (before the weekly report).

Logic:
  1. For each active campaign with >= 100 total sends:
     a. Pull per-step performance from email_events (sends, opens, replies per step)
     b. Find steps where reply rate dropped > REPLY_DROP_THRESHOLD (40%) vs previous step
     c. For underperforming steps: call Claude Sonnet with the step content + metrics
     d. Parse Claude's response into structured suggestions
     e. Upsert into sequence_suggestions table

Claude model: claude-sonnet-4-6 (quality matters — Sonnet not Haiku)
The prompt asks Claude to return structured analysis in a specific format.

Run standalone:
    python sequence_analyzer.py

Or call analyze_all_campaigns() from the weekly-report n8n workflow.
"""

import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic
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
ANTHROPIC_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

MIN_CAMPAIGN_SENDS:  int   = 100
MIN_STEP_SENDS:      int   = 20
REPLY_DROP_THRESHOLD: float = 0.40  # 40% relative drop in reply rate vs previous step

SONNET_MODEL: str = "claude-sonnet-4-6"


def _sb() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_campaigns_for_analysis(sb: Client) -> list[dict]:
    """
    Return active campaigns with >= MIN_CAMPAIGN_SENDS total email_events.
    """
    campaigns_resp = (
        sb.table("campaigns")
        .select("id, client_id, name, language, target_market")
        .eq("status", "active")
        .execute()
    )
    campaigns = campaigns_resp.data or []

    qualified: list[dict] = []
    for camp in campaigns:
        cid = camp["id"]
        try:
            count_resp = (
                sb.table("email_events")
                .select("id", count="exact")
                .eq("campaign_id", cid)
                .eq("event_type", "sent")
                .execute()
            )
            if (count_resp.count or 0) >= MIN_CAMPAIGN_SENDS:
                camp["total_sends"] = count_resp.count
                qualified.append(camp)
        except Exception as exc:
            logger.warning("Failed to count sends for campaign %s: %s", cid, exc)

    return qualified


def load_step_metrics(sb: Client, campaign_id: str) -> dict[int, dict]:
    """
    Return per-step performance metrics from email_events.

    Returns {step_number: {sends, opens, replies, open_rate, reply_rate}}
    Only steps with ab_variant IS NULL or ab_variant = 'A' are included
    (winning variants only, no retired B rows).
    """
    events_resp = (
        sb.table("email_events")
        .select("sequence_step_number, event_type, ab_variant")
        .eq("campaign_id", campaign_id)
        .execute()
    )
    events = events_resp.data or []

    by_step: dict[int, dict[str, int]] = defaultdict(lambda: {"sends": 0, "opens": 0, "replies": 0})

    for e in events:
        step_num = e.get("sequence_step_number")
        if step_num is None:
            continue
        variant = e.get("ab_variant")
        # Skip retired B variants
        if variant == "B":
            continue
        event_type = e.get("event_type")
        if event_type == "sent":
            by_step[step_num]["sends"] += 1
        elif event_type == "opened":
            by_step[step_num]["opens"] += 1
        elif event_type == "replied":
            by_step[step_num]["replies"] += 1

    result: dict[int, dict] = {}
    for step_num, counts in sorted(by_step.items()):
        sends   = counts["sends"]
        opens   = counts["opens"]
        replies = counts["replies"]
        result[step_num] = {
            "step_number": step_num,
            "sends":       sends,
            "opens":       opens,
            "replies":     replies,
            "open_rate":   round(opens / sends, 4) if sends else 0.0,
            "reply_rate":  round(replies / sends, 4) if sends else 0.0,
        }

    return result


def load_sequence_steps(sb: Client, campaign_id: str) -> dict[int, dict]:
    """
    Return sequence step content keyed by step_number.

    Returns {step_number: {id, subject, body, delay_days}}
    """
    resp = (
        sb.table("sequence_steps")
        .select("id, step_number, subject, body, delay_days, ab_variant")
        .eq("campaign_id", campaign_id)
        .neq("ab_variant", "retired")
        .order("step_number")
        .execute()
    )
    steps: dict[int, dict] = {}
    for row in (resp.data or []):
        # Prefer winning/canonical variant (no ab_variant label)
        num = row.get("step_number")
        if num is not None:
            if num not in steps or row.get("ab_variant") is None:
                steps[num] = row
    return steps


# ---------------------------------------------------------------------------
# Underperforming step detection
# ---------------------------------------------------------------------------

def find_underperforming_steps(
    step_metrics: dict[int, dict],
) -> list[tuple[int, dict, dict]]:
    """
    Identify steps where reply rate dropped more than REPLY_DROP_THRESHOLD
    relative to the previous step.

    Returns list of (step_number, step_metrics, prev_step_metrics).
    """
    underperforming: list[tuple[int, dict, dict]] = []
    sorted_steps = sorted(step_metrics.items())

    for i in range(1, len(sorted_steps)):
        prev_num, prev_metrics = sorted_steps[i - 1]
        curr_num, curr_metrics = sorted_steps[i]

        prev_rate = prev_metrics["reply_rate"]
        curr_rate = curr_metrics["reply_rate"]

        # Skip if step doesn't have enough sends
        if curr_metrics["sends"] < MIN_STEP_SENDS:
            continue

        # Skip if previous step rate is 0 (can't compute a relative drop)
        if prev_rate == 0:
            continue

        relative_drop = (prev_rate - curr_rate) / prev_rate
        if relative_drop > REPLY_DROP_THRESHOLD:
            logger.info(
                "Underperforming step %d: reply rate %.1f%% → %.1f%% (drop %.0f%%)",
                curr_num, prev_rate * 100, curr_rate * 100, relative_drop * 100,
            )
            underperforming.append((curr_num, curr_metrics, prev_metrics))

    return underperforming


# ---------------------------------------------------------------------------
# Claude Sonnet analysis
# ---------------------------------------------------------------------------

_ANALYSIS_PROMPT = """You are an expert B2B cold email copywriter and conversion specialist.
Analyse the following underperforming email sequence step.

Campaign language: {language}
Target market: {target_market}

--- PREVIOUS STEP (performing better) ---
Open rate: {prev_open_rate}
Reply rate: {prev_reply_rate}

--- UNDERPERFORMING STEP ---
Subject line: {subject}

Email body:
{body}

Open rate: {curr_open_rate}
Reply rate: {curr_reply_rate}
Sends: {sends}

This step has a {drop_pct}% drop in reply rate compared to the previous step.

Provide your analysis in EXACTLY this format (use these exact section headers):

## REASONS
1. [First specific reason this email is losing engagement]
2. [Second specific reason]
3. [Third specific reason]

## REWRITTEN SUBJECT LINE
[Your rewritten subject line — max 60 characters]

## REWRITTEN OPENING
[Your rewritten opening sentence — max 25 words, highly personalised hook]

## STRUCTURAL CHANGE
[One specific structural change to improve reply rate — concrete and actionable]

## REASONING
[2-3 sentences explaining your analysis approach and why these changes will work]"""


def _call_claude(
    subject: str,
    body: str,
    curr_metrics: dict,
    prev_metrics: dict,
    language: str,
    target_market: str,
) -> Optional[dict]:
    """
    Call Claude Sonnet to analyse an underperforming step.

    Returns parsed dict with:
        reasons (list[str]), new_subject, new_opening, structural_change, reasoning
    Or None if the call fails.
    """
    if not ANTHROPIC_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping Claude analysis.")
        return None

    drop_pct = round(
        (prev_metrics["reply_rate"] - curr_metrics["reply_rate"])
        / max(prev_metrics["reply_rate"], 0.001) * 100
    )

    prompt = _ANALYSIS_PROMPT.format(
        language       = language or "nl",
        target_market  = target_market or "BENELUX",
        prev_open_rate = f"{prev_metrics['open_rate']:.1%}",
        prev_reply_rate = f"{prev_metrics['reply_rate']:.1%}",
        subject        = subject or "(no subject)",
        body           = body or "(no body)",
        curr_open_rate = f"{curr_metrics['open_rate']:.1%}",
        curr_reply_rate = f"{curr_metrics['reply_rate']:.1%}",
        sends          = curr_metrics["sends"],
        drop_pct       = drop_pct,
    )

    try:
        client  = Anthropic(api_key=ANTHROPIC_KEY)
        message = client.messages.create(
            model      = SONNET_MODEL,
            max_tokens = 800,
            messages   = [{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()
        return _parse_claude_response(response_text)
    except Exception as exc:
        logger.error("Claude Sonnet call failed: %s", exc)
        return None


def _parse_claude_response(text: str) -> dict:
    """
    Parse Claude's structured response into a dict.

    Expected sections: REASONS, REWRITTEN SUBJECT LINE, REWRITTEN OPENING,
    STRUCTURAL CHANGE, REASONING.
    """
    def _extract_section(header: str, next_header: Optional[str] = None) -> str:
        pattern = rf"## {re.escape(header)}\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, text, re.DOTALL)
        return match.group(1).strip() if match else ""

    reasons_text = _extract_section("REASONS")
    reasons: list[str] = []
    for line in reasons_text.splitlines():
        line = line.strip()
        if re.match(r"^\d+\.", line):
            reasons.append(re.sub(r"^\d+\.\s*", "", line).strip())

    return {
        "reasons":          reasons or [reasons_text],
        "new_subject":      _extract_section("REWRITTEN SUBJECT LINE"),
        "new_opening":      _extract_section("REWRITTEN OPENING"),
        "structural_change": _extract_section("STRUCTURAL CHANGE"),
        "reasoning":        _extract_section("REASONING"),
        "raw_response":     text,
    }


# ---------------------------------------------------------------------------
# Write suggestions to DB
# ---------------------------------------------------------------------------

def _upsert_suggestion(
    sb: Client,
    client_id: str,
    campaign_id: str,
    step_id: str,
    suggestion_type: str,
    suggestion_text: str,
    reasoning: str,
    current_performance: dict,
) -> None:
    """
    Insert a new sequence suggestion, or skip if one already exists for this step
    with status='pending' (avoid duplicate suggestions on re-runs).
    """
    try:
        existing = (
            sb.table("sequence_suggestions")
            .select("id")
            .eq("sequence_step_id", step_id)
            .eq("suggestion_type", suggestion_type)
            .eq("status", "pending")
            .limit(1)
            .execute()
        ).data or []

        if existing:
            return  # Already has a pending suggestion of this type

        sb.table("sequence_suggestions").insert({
            "client_id":          client_id,
            "campaign_id":        campaign_id,
            "sequence_step_id":   step_id,
            "suggestion_type":    suggestion_type,
            "current_performance": current_performance,
            "suggestion_text":    suggestion_text,
            "claude_reasoning":   reasoning,
            "status":             "pending",
            "created_at":         _now_utc(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to upsert suggestion for step %s: %s", step_id, exc)


def process_campaign(sb: Client, campaign: dict) -> int:
    """
    Analyse one campaign and generate suggestions for underperforming steps.

    Returns the number of suggestions created.
    """
    cid         = campaign["id"]
    client_id   = campaign["client_id"]
    language    = campaign.get("language") or os.getenv("WARMUP_LANGUAGE", "nl")
    target      = campaign.get("target_market") or os.getenv("TARGET_MARKET", "BENELUX")

    step_metrics = load_step_metrics(sb, cid)
    if not step_metrics:
        return 0

    underperforming = find_underperforming_steps(step_metrics)
    if not underperforming:
        logger.info("Campaign %s (%s): all steps performing OK.", cid, campaign.get("name"))
        return 0

    sequence_steps = load_sequence_steps(sb, cid)
    suggestions_created = 0

    for step_num, curr_metrics, prev_metrics in underperforming:
        step_content = sequence_steps.get(step_num)
        if not step_content:
            logger.warning("No step content for campaign %s step %d — skipping.", cid, step_num)
            continue

        step_id = step_content["id"]
        subject = step_content.get("subject") or ""
        body    = step_content.get("body") or ""

        logger.info(
            "Calling Claude Sonnet for campaign %s step %d...",
            campaign.get("name"), step_num,
        )

        parsed = _call_claude(
            subject      = subject,
            body         = body,
            curr_metrics = curr_metrics,
            prev_metrics = prev_metrics,
            language     = language,
            target_market = target,
        )

        if not parsed:
            continue

        current_perf = {
            "sends":       curr_metrics["sends"],
            "open_rate":   curr_metrics["open_rate"],
            "reply_rate":  curr_metrics["reply_rate"],
            "prev_reply_rate": prev_metrics["reply_rate"],
            "step_number": step_num,
        }

        # Create one suggestion per type
        if parsed.get("new_subject"):
            _upsert_suggestion(
                sb, client_id, cid, step_id,
                suggestion_type  = "subject_line",
                suggestion_text  = parsed["new_subject"],
                reasoning        = "\n".join(parsed.get("reasons", [])),
                current_performance = current_perf,
            )
            suggestions_created += 1

        if parsed.get("new_opening"):
            _upsert_suggestion(
                sb, client_id, cid, step_id,
                suggestion_type  = "opening",
                suggestion_text  = parsed["new_opening"],
                reasoning        = parsed.get("reasoning", ""),
                current_performance = current_perf,
            )
            suggestions_created += 1

        if parsed.get("structural_change"):
            _upsert_suggestion(
                sb, client_id, cid, step_id,
                suggestion_type  = "structure",
                suggestion_text  = parsed["structural_change"],
                reasoning        = parsed.get("reasoning", ""),
                current_performance = current_perf,
            )
            suggestions_created += 1

    logger.info(
        "Campaign %s: %d underperforming step(s), %d suggestion(s) created.",
        campaign.get("name"), len(underperforming), suggestions_created,
    )
    return suggestions_created


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def analyze_all_campaigns(sb: Optional[Client] = None) -> int:
    """
    Run sequence analysis for all eligible active campaigns.

    Returns total number of suggestions created across all campaigns.
    """
    if sb is None:
        sb = _sb()

    campaigns = load_campaigns_for_analysis(sb)
    if not campaigns:
        logger.info("No campaigns with >= %d sends found.", MIN_CAMPAIGN_SENDS)
        return 0

    logger.info(
        "Analysing %d campaign(s) with >= %d sends...",
        len(campaigns), MIN_CAMPAIGN_SENDS,
    )

    total_suggestions = 0
    for campaign in campaigns:
        try:
            total_suggestions += process_campaign(sb, campaign)
        except Exception as exc:
            logger.error(
                "Failed to analyse campaign %s: %s",
                campaign.get("name", campaign["id"]), exc,
            )

    logger.info("Sequence analysis complete. Total suggestions: %d", total_suggestions)
    return total_suggestions


if __name__ == "__main__":
    analyze_all_campaigns()
