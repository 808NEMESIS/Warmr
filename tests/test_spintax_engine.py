"""
tests/test_spintax_engine.py — unit tests for spintax and variable substitution.

Pure function tests — no mocks, no DB.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spintax_engine import (
    process_content,
    process_spintax,
    substitute_variables,
    validate_spintax,
    preview,
)


# ── Spintax ─────────────────────────────────────────────────────────────────

def test_simple_spintax_picks_one_option():
    result = process_spintax("{hi|hello|hey} there", lead_id="lead-1", step_number=1)
    assert result.endswith(" there")
    first_word = result.split(" ")[0]
    assert first_word in ("hi", "hello", "hey")


def test_spintax_is_deterministic_per_lead_and_step():
    r1 = process_spintax("{a|b|c}", lead_id="lead-1", step_number=1)
    r2 = process_spintax("{a|b|c}", lead_id="lead-1", step_number=1)
    assert r1 == r2


def test_spintax_differs_across_leads():
    # Run across 20 different leads — very unlikely they all pick the same option
    results = {process_spintax("{a|b|c|d|e}", lead_id=f"lead-{i}", step_number=1) for i in range(20)}
    assert len(results) > 1  # at least two distinct outputs


def test_nested_spintax():
    result = process_spintax("{hi|{hello|hey}} there", lead_id="lead-1", step_number=1)
    first_word = result.split(" ")[0]
    assert first_word in ("hi", "hello", "hey")


def test_no_spintax_returns_unchanged():
    text = "No spintax here, just plain text."
    assert process_spintax(text, lead_id="lead-1", step_number=1) == text


# ── Variable substitution ──────────────────────────────────────────────────

def test_substitute_first_name():
    lead = {"first_name": "Jan", "email": "jan@example.com"}
    assert substitute_variables("Hi {{first_name}}!", lead) == "Hi Jan!"


def test_substitute_full_name_computes_from_parts():
    lead = {"first_name": "Jan", "last_name": "de Vries"}
    assert substitute_variables("{{full_name}}", lead) == "Jan de Vries"


def test_substitute_unknown_var_left_unchanged():
    lead = {"first_name": "Jan"}
    assert substitute_variables("{{nonexistent}}", lead) == "{{nonexistent}}"


def test_substitute_missing_builtin_falls_back_naturally():
    """Missing first_name renders as 'there' — 'Hi there,' reads natural.
    Before this, it was 'Hi {{first_name}},' which shipped to the prospect
    and looked broken. The fallback protects deliverability + UX."""
    lead = {}
    assert substitute_variables("Hi {{first_name}},", lead) == "Hi there,"


def test_substitute_missing_company_falls_back_to_your_team():
    lead = {}
    assert substitute_variables("how's {{company}} doing?", lead) == "how's your team doing?"


def test_substitute_custom_field():
    lead = {"custom_fields": {"revenue": "5M"}}
    assert substitute_variables("Revenue: {{custom:revenue}}", lead) == "Revenue: 5M"


def test_substitute_custom_field_missing_becomes_empty():
    """Custom fields are optional personalization — empty is the right default."""
    lead = {"custom_fields": {}}
    assert substitute_variables("{{custom:missing}}", lead) == ""


def test_substitute_client_settings_calendar_link():
    lead = {"first_name": "Jan"}
    settings = {"booking_url": "https://cal.com/jan/15min"}
    result = substitute_variables("Agenda: {{calendar_link}}", lead, settings)
    assert result == "Agenda: https://cal.com/jan/15min"


def test_substitute_client_settings_sender_name():
    lead = {}
    settings = {"sender_name": "Alice"}
    assert substitute_variables("Groet, {{sender_name}}", lead, settings) == "Groet, Alice"


# ── Combined pipeline ──────────────────────────────────────────────────────

def test_process_content_spintax_then_vars():
    lead = {"id": "lead-1", "first_name": "Jan"}
    result = process_content("{Hi|Hello} {{first_name}}", lead, step_number=1)
    # Should contain "Jan" after variable substitution
    assert "Jan" in result
    # First word should be one of the spintax options
    first = result.split(" ")[0]
    assert first in ("Hi", "Hello")


def test_process_content_with_client_settings():
    lead = {"id": "lead-1", "first_name": "Jan"}
    settings = {"booking_url": "https://cal.com/x"}
    result = process_content("{{first_name}}, boek: {{calendar_link}}", lead, client_settings=settings)
    assert result == "Jan, boek: https://cal.com/x"


def test_process_content_spintax_disabled():
    lead = {"id": "lead-1"}
    result = process_content("{a|b|c}", lead, step_number=1, spintax_enabled=False)
    # Spintax NOT processed → remains literal
    assert result == "{a|b|c}"


# ── Validation ─────────────────────────────────────────────────────────────

def test_validate_spintax_detects_unbalanced():
    errors = validate_spintax("Hi {unclosed")
    assert len(errors) > 0


def test_validate_spintax_clean_returns_empty():
    errors = validate_spintax("{a|b} normal text")
    assert errors == []


# ── Preview ────────────────────────────────────────────────────────────────

def test_preview_generates_n_samples():
    lead = {"id": "lead-1", "first_name": "Jan"}
    samples = preview("{Hi|Hello} {{first_name}}", lead, n_samples=5)
    assert len(samples) == 5
    for s in samples:
        assert "Jan" in s


def test_preview_passes_client_settings():
    lead = {"id": "lead-1"}
    settings = {"sender_name": "Alice"}
    samples = preview("{{sender_name}}", lead, n_samples=2, client_settings=settings)
    assert all(s == "Alice" for s in samples)


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"  \u2713 {name}")
            except AssertionError as e:
                failed += 1
                print(f"  \u2717 {name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  \u2717 {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
