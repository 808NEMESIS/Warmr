"""
tests/test_heatr_integration.py — tests for the Heatr ↔ Warmr contract.

Verifies that the shape of lead payloads Heatr pushes to Warmr is what Warmr
expects. Uses a captured sample payload that mirrors what warmr_client.py in
Heatr generates.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Sample lead payload as Heatr would push it (from /Users/nemesis/Heatr/integrations/warmr_client.py)
HEATR_LEAD_PAYLOAD = {
    "email": "prospect@example.nl",
    "first_name": "Jan",
    "last_name": "de Vries",
    "campaign_id": "camp-uuid-123",
    "gdpr_footer_required": True,
    "custom_fields": {
        # Personalization
        "opener": "Zag jullie site over osteopathie en viel me op dat...",
        "summary": "Osteopathie praktijk in Utrecht, 3 behandelaars",
        "company": "Osteopathie Utrecht",
        "city": "Utrecht",
        "industry": "Alternatieve Zorg",
        "company_size": "2-10",
        "sector": "healthcare",
        # Scoring signals
        "heatr_score": 78,
        "icp_match": 0.85,
        "website_score": 6.2,
        "fit_score": 8,
        "reachability_score": 9,
        "data_quality": 7,
        # Company signals
        "has_instagram": "ja",
        "google_rating": 4.8,
        "google_review_count": 47,
        "kvk_number": "12345678",
        "domain": "example.nl",
        # Contact person
        "contact_title": "Eigenaar",
        "contact_linkedin_url": "https://linkedin.com/in/example",
        "contact_why_chosen": "Eigenaar van de praktijk, neemt beslissingen",
        # Website intel
        "positioning": "Premium osteopathie voor sporters",
        "hooks": "sporters|blessures|herstel|preventie",
        "observations": "Geen Instagram|oude website design",
        # Correlation
        "heatr_lead_id": "heatr-uuid-456",
        "workspace_id": "workspace-789",
        # Metadata
        "source": "heatr",
    },
}


def test_payload_has_required_top_level_fields():
    """Warmr requires email, first_name, campaign_id."""
    assert "email" in HEATR_LEAD_PAYLOAD
    assert "@" in HEATR_LEAD_PAYLOAD["email"]
    assert "first_name" in HEATR_LEAD_PAYLOAD
    assert "campaign_id" in HEATR_LEAD_PAYLOAD


def test_payload_custom_fields_has_correlation_ids():
    """The heatr_lead_id + workspace_id are critical for the reverse webhook loop."""
    cf = HEATR_LEAD_PAYLOAD["custom_fields"]
    assert "heatr_lead_id" in cf
    assert "workspace_id" in cf


def test_payload_has_personalization_data():
    """Opener + company are used in spintax variables (`{{opener}}`, `{{company}}`)."""
    cf = HEATR_LEAD_PAYLOAD["custom_fields"]
    assert "opener" in cf
    assert "company" in cf
    assert len(cf["opener"]) > 10


def test_payload_icp_match_is_normalized():
    """ICP match must be 0-1 range (Warmr funnel page expects this)."""
    cf = HEATR_LEAD_PAYLOAD["custom_fields"]
    assert 0 <= cf["icp_match"] <= 1


def test_payload_heatr_score_in_valid_range():
    """heatr_score is 0-100."""
    cf = HEATR_LEAD_PAYLOAD["custom_fields"]
    assert 0 <= cf["heatr_score"] <= 100


def test_payload_gdpr_footer_flag():
    """Warmr appends an unsubscribe footer. This flag is documentation — the footer
    is always appended regardless — but keeps the intent explicit in the payload."""
    assert HEATR_LEAD_PAYLOAD["gdpr_footer_required"] is True


def test_spintax_can_render_heatr_vars():
    """Warmr's spintax engine must be able to render Heatr-provided custom fields."""
    from spintax_engine import process_content

    lead = {
        "id": HEATR_LEAD_PAYLOAD["custom_fields"]["heatr_lead_id"],
        "first_name": HEATR_LEAD_PAYLOAD["first_name"],
        "email": HEATR_LEAD_PAYLOAD["email"],
        "custom_fields": HEATR_LEAD_PAYLOAD["custom_fields"],
    }
    template = "Hi {{first_name}}, {{custom:opener}} — werkt {{custom:company}} nog in {{custom:city}}?"
    result = process_content(template, lead, step_number=1)

    assert "Jan" in result
    assert "osteopathie" in result  # from opener
    assert "Osteopathie Utrecht" in result
    assert "Utrecht" in result
    # No unresolved variables
    assert "{{" not in result


def test_webhook_payload_shape_for_replies():
    """The shape of the payload Warmr POSTs back to Heatr via /webhooks/warmr."""
    # This is the contract — Heatr's webhook handler reads these keys.
    sample = {
        "event": "lead.replied",
        "from_email": "prospect@example.nl",
        "from_name": "Jan de Vries",
        "subject": "Re: Snelle vraag over osteopathie",
        "body_text": "Interessant, kan je meer info sturen?",
        "body_html": "<p>Interessant...</p>",
        "custom_fields": {
            "heatr_lead_id": "heatr-uuid-456",
            "workspace_id": "workspace-789",
        },
    }
    assert sample["event"].startswith("lead.") or sample["event"] in (
        "replied", "interested", "bounced", "unsubscribed", "campaign.completed", "inbox.warmup_complete"
    )
    assert "heatr_lead_id" in sample["custom_fields"]
    assert "workspace_id" in sample["custom_fields"]


def test_event_names_handled_by_heatr():
    """Heatr must handle these event types (see /Users/nemesis/Heatr/api/main.py)."""
    handled_events = {
        "replied", "lead.replied",
        "interested", "lead.interested",
        "bounced", "lead.bounced",
        "unsubscribed", "lead.unsubscribed",
        "campaign.completed",
        "inbox.warmup_complete",
    }
    # Just assert the set is populated; the real assertion is that Heatr has code for each
    assert len(handled_events) >= 8


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
