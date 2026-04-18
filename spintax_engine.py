"""
spintax_engine.py

Deterministic spintax processor and variable substitutor for campaign emails.

Spintax syntax: {option1|option2|option3}
Nesting:        {Hey {there|friend}|Hi|Hello}
Variables:      {{first_name}}, {{company}}, {{custom:field_name}}

Determinism guarantee: the same (lead_id, step_number) pair always produces
the same output, so a lead never receives contradictory messaging across
multiple runs of the scheduler. Different leads receive different variants,
creating natural message variety without A/B testing overhead.
"""

import hashlib
import random
import re
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Seeded RNG
# ---------------------------------------------------------------------------

def _make_rng(lead_id: str, step_number: int) -> random.Random:
    """
    Build a deterministic Random instance seeded from lead_id + step_number.

    The sha256 hash of the combined key is folded to a 32-bit seed so the
    seed space is large enough to avoid birthday collisions in typical list sizes.
    """
    key = f"{lead_id}:{step_number}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    seed = int(digest, 16) % (2**32)
    return random.Random(seed)


# ---------------------------------------------------------------------------
# Spintax parser
# ---------------------------------------------------------------------------

def _find_closing_brace(text: str, open_pos: int) -> int:
    """
    Find the position of the closing brace matching the opening brace at open_pos.

    Returns the index of '}', or -1 if no match found (malformed spintax).
    """
    depth = 0
    for i in range(open_pos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_at_pipe(content: str) -> list[str]:
    """
    Split content by '|' only at brace-nesting depth 0.

    Handles nested braces correctly: {Hey {there|friend}|Hi} splits into
    ["Hey {there|friend}", "Hi"], not ["Hey {there", "friend}", "Hi"].
    """
    options: list[str] = []
    depth = 0
    current: list[str] = []

    for char in content:
        if char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            depth -= 1
            current.append(char)
        elif char == "|" and depth == 0:
            options.append("".join(current))
            current = []
        else:
            current.append(char)

    options.append("".join(current))
    return options


def _apply_spintax(text: str, rng: random.Random) -> str:
    """
    Recursively resolve all spintax blocks in text using the provided RNG.

    Processes left-to-right; nested blocks are resolved after the outer choice
    is made, which means the RNG is consumed in the order blocks are encountered
    during traversal — consistent for a fixed template.
    """
    result: list[str] = []
    i = 0

    while i < len(text):
        if text[i] == "{":
            close = _find_closing_brace(text, i)
            if close == -1:
                # Malformed — treat as literal text
                result.append(text[i])
                i += 1
                continue

            inner = text[i + 1 : close]
            options = _split_at_pipe(inner)
            chosen = rng.choice(options)
            # Recurse: inner blocks of the chosen option are processed now
            result.append(_apply_spintax(chosen, rng))
            i = close + 1
        else:
            result.append(text[i])
            i += 1

    return "".join(result)


def process_spintax(text: str, lead_id: str, step_number: int = 1) -> str:
    """
    Public API: resolve all spintax in text deterministically for (lead_id, step_number).

    If the text contains no spintax blocks, it is returned unchanged.
    Malformed blocks (unmatched braces) are left as literal text.

    Args:
        text:        Raw template string with {option|option} blocks.
        lead_id:     UUID string of the lead. Determines the random seed.
        step_number: Sequence step number. Different steps use different seeds
                     so the same lead doesn't always pick the same slot.

    Returns:
        Resolved string with all spintax replaced by one chosen option.
    """
    if "{" not in text:
        return text
    rng = _make_rng(lead_id, step_number)
    return _apply_spintax(text, rng)


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------

# Supported built-in variable names mapped to lead dict keys
_BUILTIN_VARS: dict[str, str] = {
    "first_name": "first_name",
    "last_name": "last_name",
    "full_name": "__full_name__",       # computed below
    "company": "company",
    "company_name": "company",          # alias
    "email": "email",
    "job_title": "job_title",
    "phone": "phone",
    "country": "country",
    "linkedin_url": "linkedin_url",
    "domain": "domain",
}

# Matches {{variable_name}} or {{custom:key_name}}
_VAR_PATTERN = re.compile(r"\{\{([^}]+)\}\}")


def _resolve_var(name: str, lead: dict, client_settings: dict | None = None) -> str:
    """
    Resolve one {{variable}} token against a lead dict and optional client_settings.

    Unknown variables are left as-is so the output clearly flags missing data
    rather than silently inserting empty strings.
    """
    name = name.strip()
    settings = client_settings or {}

    # Custom fields: {{custom:revenue}} → lead['custom_fields']['revenue']
    if name.startswith("custom:"):
        key = name[7:].strip()
        custom = lead.get("custom_fields") or {}
        return str(custom.get(key, f"{{{{{name}}}}}"))

    # Computed: full_name = first_name + last_name
    if name == "full_name":
        parts = [
            (lead.get("first_name") or "").strip(),
            (lead.get("last_name") or "").strip(),
        ]
        combined = " ".join(p for p in parts if p)
        return combined or f"{{{{{name}}}}}"

    # Client-level variables (calendar/booking link, sender details)
    if name in ("calendar_link", "booking_url"):
        return settings.get("booking_url") or f"{{{{{name}}}}}"
    if name == "sender_name":
        return settings.get("sender_name") or f"{{{{{name}}}}}"
    if name == "sender_company":
        return settings.get("company_name") or f"{{{{{name}}}}}"
    if name == "signature":
        return settings.get("email_signature") or ""

    # Built-in fields
    lead_key = _BUILTIN_VARS.get(name)
    if lead_key:
        value = lead.get(lead_key) or ""
        return str(value) if value else f"{{{{{name}}}}}"

    # Unknown — return unchanged so the template error is visible
    return f"{{{{{name}}}}}"


def substitute_variables(text: str, lead: dict, client_settings: dict | None = None) -> str:
    """
    Replace all {{variable}} tokens in text with values from the lead dict
    and optional client_settings (booking_url, sender_name, signature, etc.).

    Supported tokens:
      Lead-level: {{first_name}}, {{last_name}}, {{full_name}}, {{company}},
                  {{email}}, {{job_title}}, {{phone}}, {{country}}, {{domain}},
                  {{linkedin_url}}, {{custom:any_key}}
      Client-level: {{calendar_link}}, {{booking_url}}, {{sender_name}},
                    {{sender_company}}, {{signature}}

    Unknown tokens are left unchanged (e.g. {{unknown}} stays {{unknown}}).
    """
    def replacer(match: re.Match) -> str:
        return _resolve_var(match.group(1), lead, client_settings)

    return _VAR_PATTERN.sub(replacer, text)


# ---------------------------------------------------------------------------
# Combined pipeline
# ---------------------------------------------------------------------------

def process_content(
    text: str,
    lead: dict,
    step_number: int = 1,
    spintax_enabled: bool = True,
    client_settings: dict | None = None,
) -> str:
    """
    Full content pipeline: spintax resolution → variable substitution.

    Spintax is processed first so variable values are never accidentally
    interpreted as spintax delimiters.

    Args:
        text:            Raw template (subject or body).
        lead:            Lead dict from Supabase (must contain at least 'id' and 'email').
        step_number:     Sequence step number for seeding the RNG.
        spintax_enabled: If False, spintax blocks are left unprocessed.

    Returns:
        Fully processed string ready to send.
    """
    lead_id = str(lead.get("id") or lead.get("email") or "unknown")

    if spintax_enabled:
        text = process_spintax(text, lead_id=lead_id, step_number=step_number)

    text = substitute_variables(text, lead, client_settings)
    return text


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_spintax(text: str) -> list[str]:
    """
    Check a template for spintax errors.

    Returns a list of human-readable error strings.
    An empty list means the template is valid.
    """
    errors: list[str] = []
    depth = 0
    for i, char in enumerate(text):
        if char == "{":
            # Skip {{ variable markers }}
            if i + 1 < len(text) and text[i + 1] == "{":
                continue
            depth += 1
        elif char == "}":
            if i > 0 and text[i - 1] == "}":
                continue
            depth -= 1
            if depth < 0:
                errors.append(f"Unexpected closing '}}' at position {i}.")
                depth = 0
    if depth > 0:
        errors.append(f"{depth} unclosed '{{' block(s) found in template.")
    return errors


def preview(
    text: str,
    lead: dict,
    step_number: int = 1,
    spintax_enabled: bool = True,
    n_samples: int = 3,
    client_settings: dict | None = None,
) -> list[str]:
    """
    Generate n_samples preview outputs for a template.

    Useful in the API to show the dashboard what different leads will see.
    Each sample uses a different synthetic lead_id to produce different spintax choices.
    """
    results: list[str] = []
    lead_id_base = str(lead.get("id") or "preview")
    for i in range(n_samples):
        synthetic_id = f"{lead_id_base}:sample:{i}"
        sample_lead = {**lead, "id": synthetic_id}
        results.append(process_content(text, sample_lead, step_number, spintax_enabled, client_settings))
    return results
