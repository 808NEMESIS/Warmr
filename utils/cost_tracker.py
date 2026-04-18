"""
utils/cost_tracker.py — Claude API cost tracking + daily budget enforcement.

Wraps every Claude API call with:
  1. Budget check — reject if daily spend exceeds DAILY_API_BUDGET_EUR
  2. Cost logging — store token counts + EUR cost in api_cost_log table
  3. Cost calculation — based on Anthropic pricing per model

Usage:
    from utils.cost_tracker import tracked_claude_call, check_budget

    # Check budget before expensive operation
    can_spend, remaining = check_budget(supabase, client_id)

    # Or use tracked_claude_call which checks automatically
    message = tracked_claude_call(
        claude_client, supabase,
        model="claude-haiku-4-5-20251001",
        messages=[{"role": "user", "content": "..."}],
        max_tokens=300,
        context="warmup_content",
        client_id="...",
    )
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing (EUR per token, as of 2026-04)
# ---------------------------------------------------------------------------
# Anthropic prices in USD; approximate EUR conversion at 0.92 EUR/USD
COST_PER_TOKEN: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {
        "input":  0.80 / 1_000_000 * 0.92,   # $0.80/M → ~€0.000000736
        "output": 4.00 / 1_000_000 * 0.92,   # $4.00/M → ~€0.00000368
    },
    "claude-sonnet-4-6": {
        "input":  3.00 / 1_000_000 * 0.92,   # $3/M → ~€0.00000276
        "output": 15.0 / 1_000_000 * 0.92,   # $15/M → ~€0.0000138
    },
}

# Fallback for unknown models
DEFAULT_COST = {"input": 3.0 / 1_000_000 * 0.92, "output": 15.0 / 1_000_000 * 0.92}

# Daily budget cap (EUR). Set to 0 to disable the cap.
DAILY_API_BUDGET_EUR = float(os.getenv("DAILY_API_BUDGET_EUR", "2.00"))


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate EUR cost for a Claude API call based on token counts."""
    rates = COST_PER_TOKEN.get(model, DEFAULT_COST)
    cost = (input_tokens * rates["input"]) + (output_tokens * rates["output"])
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Budget checking
# ---------------------------------------------------------------------------

def get_daily_spend(supabase_client, client_id: str | None = None) -> float:
    """
    Return total EUR spent today on Claude API calls.
    Returns 0.0 if the table doesn't exist or query fails.
    """
    try:
        today = date.today().isoformat()
        q = supabase_client.table("api_cost_log").select("cost_eur").eq("date", today)
        if client_id:
            q = q.eq("client_id", client_id)
        res = q.execute()
        return round(sum(r.get("cost_eur") or 0 for r in (res.data or [])), 6)
    except Exception as e:
        logger.debug("get_daily_spend failed (table may not exist): %s", e)
        return 0.0


def check_budget(
    supabase_client,
    client_id: str | None = None,
) -> tuple[bool, float]:
    """
    Check if daily API budget allows more spending.

    Returns:
        (can_spend: bool, remaining_eur: float)

    If DAILY_API_BUDGET_EUR is 0, budget is unlimited.
    """
    if DAILY_API_BUDGET_EUR <= 0:
        return True, float("inf")

    spent = get_daily_spend(supabase_client, client_id)
    remaining = round(DAILY_API_BUDGET_EUR - spent, 6)
    can_spend = remaining > 0

    if not can_spend:
        logger.warning(
            "Daily API budget exhausted: spent €%.4f / €%.2f",
            spent, DAILY_API_BUDGET_EUR,
        )

    return can_spend, remaining


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_api_cost(
    supabase_client,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_eur: float,
    context: str = "",
    client_id: str | None = None,
    inbox_id: str | None = None,
) -> None:
    """Log a Claude API call to api_cost_log. Never raises."""
    try:
        row: dict[str, Any] = {
            "date": date.today().isoformat(),
            "model": model,
            "prompt_tokens": input_tokens,
            "response_tokens": output_tokens,
            "cost_eur": cost_eur,
            "context": context,
        }
        if client_id:
            row["client_id"] = client_id
        if inbox_id:
            row["inbox_id"] = inbox_id

        supabase_client.table("api_cost_log").insert(row).execute()
    except Exception as e:
        logger.debug("log_api_cost failed (table may not exist): %s", e)


# ---------------------------------------------------------------------------
# Tracked Claude call — main interface
# ---------------------------------------------------------------------------

class BudgetExceededError(Exception):
    """Raised when daily API budget is exhausted."""
    pass


def tracked_claude_call(
    claude_client: anthropic.Anthropic,
    supabase_client,
    *,
    model: str = "claude-haiku-4-5-20251001",
    messages: list[dict],
    max_tokens: int = 300,
    system: str | None = None,
    context: str = "",
    client_id: str | None = None,
    inbox_id: str | None = None,
    enforce_budget: bool = True,
) -> anthropic.types.Message:
    """
    Call Claude API with automatic cost tracking and budget enforcement.

    Args:
        claude_client: Anthropic client instance.
        supabase_client: Supabase client for logging.
        model: Claude model to use.
        messages: Chat messages.
        max_tokens: Max output tokens.
        system: Optional system prompt.
        context: What this call is for (e.g. 'warmup_content').
        client_id: Client UUID for per-client tracking.
        inbox_id: Related inbox UUID.
        enforce_budget: If True, raises BudgetExceededError when over budget.

    Returns:
        The Claude Message response.

    Raises:
        BudgetExceededError: If daily budget is exhausted and enforce_budget=True.
    """
    # Budget check
    if enforce_budget:
        can_spend, remaining = check_budget(supabase_client, client_id)
        if not can_spend:
            raise BudgetExceededError(
                f"Daily API budget exhausted (€{DAILY_API_BUDGET_EUR:.2f}). "
                f"Remaining: €{remaining:.4f}"
            )

    # Build kwargs
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system

    # Make the API call
    message = claude_client.messages.create(**kwargs)

    # Calculate and log cost
    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    cost = calculate_cost(model, input_tokens, output_tokens)

    log_api_cost(
        supabase_client,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_eur=cost,
        context=context,
        client_id=client_id,
        inbox_id=inbox_id,
    )

    logger.debug(
        "Claude API call: model=%s in=%d out=%d cost=€%.6f context=%s",
        model, input_tokens, output_tokens, cost, context,
    )

    return message


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def get_daily_summary(supabase_client, client_id: str | None = None) -> dict[str, Any]:
    """
    Return a summary of today's API usage.

    Returns:
        {
            "date": "2026-04-15",
            "total_calls": int,
            "total_cost_eur": float,
            "budget_eur": float,
            "remaining_eur": float,
            "by_context": {"warmup_content": {"calls": N, "cost": X}, ...},
            "by_model": {"claude-haiku-4-5-20251001": {"calls": N, "cost": X}, ...},
        }
    """
    today = date.today().isoformat()
    try:
        q = supabase_client.table("api_cost_log").select("*").eq("date", today)
        if client_id:
            q = q.eq("client_id", client_id)
        res = q.execute()
        rows = res.data or []
    except Exception:
        rows = []

    total_cost = sum(r.get("cost_eur") or 0 for r in rows)

    by_context: dict[str, dict] = {}
    by_model: dict[str, dict] = {}

    for r in rows:
        ctx = r.get("context") or "unknown"
        mdl = r.get("model") or "unknown"

        if ctx not in by_context:
            by_context[ctx] = {"calls": 0, "cost": 0.0}
        by_context[ctx]["calls"] += 1
        by_context[ctx]["cost"] = round(by_context[ctx]["cost"] + (r.get("cost_eur") or 0), 6)

        if mdl not in by_model:
            by_model[mdl] = {"calls": 0, "cost": 0.0}
        by_model[mdl]["calls"] += 1
        by_model[mdl]["cost"] = round(by_model[mdl]["cost"] + (r.get("cost_eur") or 0), 6)

    remaining = max(0, DAILY_API_BUDGET_EUR - total_cost) if DAILY_API_BUDGET_EUR > 0 else float("inf")

    return {
        "date": today,
        "total_calls": len(rows),
        "total_cost_eur": round(total_cost, 4),
        "budget_eur": DAILY_API_BUDGET_EUR,
        "remaining_eur": round(remaining, 4),
        "by_context": by_context,
        "by_model": by_model,
    }
