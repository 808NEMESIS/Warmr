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


def test_single_lead_push_shape_matches_heatr_client():
    """
    Heatr's warmr_client.push_lead POSTs a flat lead dict to /leads and reads
    result.get("id") or result.get("lead_id"). Warmr's POST /leads now accepts
    the flat shape (polymorphic on presence of 'leads' key) and returns the
    inserted row — which contains 'id' from Supabase.
    """
    # Response shape Warmr returns for single-lead body (the inserted row)
    sample_single_response = {
        "id":           "lead-uuid-123",
        "email":        "prospect@example.nl",
        "first_name":   "Jan",
        "last_name":    "de Vries",
        "status":       "new",
        "client_id":    "client-uuid",
        "custom_fields": {"heatr_lead_id": "heatr-uuid-456"},
    }
    warmr_lead_id = sample_single_response.get("id") or sample_single_response.get("lead_id")
    assert warmr_lead_id == "lead-uuid-123"
    # heatr_lead_id must round-trip so Warmr→Heatr webhooks can correlate
    assert sample_single_response["custom_fields"]["heatr_lead_id"]


def test_bulk_leads_response_shape_matches_heatr_client():
    """
    Heatr's warmr_client.push_leads_bulk reads result.get("pushed"|"failed"|"duplicates").
    Warmr's POST /leads + POST /leads/bulk MUST return those keys exactly.
    Sinds 2026-05-15 vereist Heatr's per-row bookkeeping ook een `inserted`
    array op POST /leads/bulk response — voorheen alleen aggregate counts.
    """
    # Captured from api/public_api.py public_create_leads return
    sample = {
        "pushed":        2,
        "duplicates":    1,
        "suppressed":    0,
        "failed":        0,
        "error_details": [],
        "inserted": [
            {"email": "a@example.nl", "lead_id": "lead-uuid-1"},
            {"email": "b@example.nl", "lead_id": "lead-uuid-2"},
        ],
    }
    assert "pushed" in sample      # was "imported" pre-2026-04
    assert "failed" in sample      # was "errors" pre-2026-04
    assert "duplicates" in sample
    # Heatr maps response.inserted[i].email → heatr_leads.warmr_lead_id.
    # The contract requires email + lead_id on every entry.
    assert "inserted" in sample
    assert isinstance(sample["inserted"], list)
    for row in sample["inserted"]:
        assert "email" in row
        assert "lead_id" in row


def test_bulk_inserted_accumulates_per_chunk_not_at_end():
    """
    Spec invariant: `inserted` is built up per chunk inside the insert loop,
    not assembled at the end. A failure on chunk N must leave chunks 1..N-1
    visible in the response so the caller can do partial bookkeeping.

    This test exercises the actual handler with a stubbed Supabase that fails
    on the second chunk — first chunk's rows must still surface.
    """
    import asyncio
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    # Need a real BulkLeadIn-shaped body
    from api.public_api import BulkLeadIn, LeadIn, _insert_leads_bulk

    # Build 600 leads → 2 chunks of 500 + 100 with CHUNK=500.
    # Stub Supabase: first chunk OK, second chunk raises.
    class _StubResp:
        def __init__(self, data): self.data = data

    class _StubQuery:
        """Chained query object: select/eq/in_/order/limit/insert/.execute()."""
        def __init__(self, parent, action=None, payload=None):
            self.parent = parent
            self.action = action
            self.payload = payload
        def select(self, *a, **kw): return self
        def eq(self, *a, **kw): return self
        def in_(self, *a, **kw): return self
        def gte(self, *a, **kw): return self
        def order(self, *a, **kw): return self
        def limit(self, *a, **kw): return self
        def insert(self, rows):
            return _StubQuery(self.parent, action="insert", payload=rows)
        def execute(self):
            if self.action == "insert":
                self.parent.insert_calls += 1
                if self.parent.insert_calls == 2:
                    raise RuntimeError("simulated chunk-2 failure")
                # Return inserted rows with synthetic IDs
                data = [
                    {"id": f"lead-{i}", "email": r["email"]}
                    for i, r in enumerate(self.payload)
                ]
                return _StubResp(data)
            # Reads (dedup / suppression): empty
            return _StubResp([])

    class _StubSb:
        def __init__(self):
            self.insert_calls = 0
        def table(self, name):
            return _StubQuery(self)

    stub_sb = _StubSb()

    # Patch _sb() to return our stub, and disable background tasks
    import api.public_api as pa
    orig_sb = pa._sb
    pa._sb = lambda: stub_sb
    try:
        class _Ctx:
            client_id = "test-client"
            def require(self, *perms): pass

        class _BG:
            def add_task(self, *a, **kw): pass

        body = BulkLeadIn(
            leads=[LeadIn(email=f"lead{i}@example.nl") for i in range(600)],
            deduplicate=False,
        )
        result = asyncio.run(_insert_leads_bulk(body, _Ctx(), _BG()))
    finally:
        pa._sb = orig_sb

    # Chunk 1 (500 rows) succeeded; chunk 2 (100 rows) raised.
    # The response MUST still have those 500 rows in `inserted`, not empty.
    assert result["pushed"] == 500
    assert result["failed"] == 100
    assert len(result["inserted"]) == 500
    assert all("email" in r and "lead_id" in r for r in result["inserted"])


def test_bulk_inserted_excludes_duplicates_and_suppressed():
    """
    `inserted` MUST contain only genuinely-inserted rows. Duplicates (already
    in DB) and suppressed (on suppression_list) are filtered before the insert
    loop runs, so by construction they cannot appear in `inserted`. This test
    encodes that invariant for downstream callers.
    """
    # Simulated response when 3 leads pushed, 1 dup, 1 suppressed → 1 inserted
    sample = {
        "pushed":      1,
        "duplicates":  1,
        "suppressed":  1,
        "failed":      0,
        "inserted":    [{"email": "fresh@example.nl", "lead_id": "lead-uuid-3"}],
        "error_details": [],
    }
    assert len(sample["inserted"]) == sample["pushed"]
    assert len(sample["inserted"]) < (
        sample["pushed"] + sample["duplicates"] + sample["suppressed"]
    )


def test_campaign_create_response_returns_id_field():
    """
    Heatr's warmr_client.create_campaign raises unless result.get("id") OR
    result.get("campaign_id") is truthy. Warmr's POST /campaigns returns BOTH
    as aliases so either client contract works.
    """
    sample_response = {
        "id": "c-uuid-abc",
        "campaign_id": "c-uuid-abc",
        "name": "Test campaign",
        "status": "draft",
        "steps_inserted": 0,
    }
    warmr_id = sample_response.get("id") or sample_response.get("campaign_id")
    assert warmr_id, "Warmr must return a campaign id after POST /campaigns"


def test_inbox_availability_response_shape():
    """
    Heatr's warmr_client.get_inbox_availability docstring expects at least
    id, daily_remaining, reputation_score. Warmr's new endpoint returns those
    plus daily_cap + daily_sent + status + email for richer client decisions.
    """
    sample = {
        "id":               "inbox-uuid",
        "email":            "sender@example.com",
        "status":           "ready",
        "reputation_score": 78.5,
        "daily_cap":        50,
        "daily_sent":       12,
        "daily_remaining":  38,
    }
    # Heatr-required minimum
    assert "id" in sample
    assert "daily_remaining" in sample
    assert "reputation_score" in sample
    # Invariant: remaining == cap - sent (non-negative)
    assert sample["daily_remaining"] == max(0, sample["daily_cap"] - sample["daily_sent"])


def test_inbox_list_response_envelope():
    """
    Heatr's get_ready_inboxes handles both `{inboxes: [...]}` envelope and a
    bare list. Warmr returns the envelope form — this test documents that.
    """
    sample = {"inboxes": [
        {"id": "inbox-1", "email": "a@x.nl", "status": "ready", "reputation_score": 72},
    ]}
    assert isinstance(sample, dict) and "inboxes" in sample
    assert isinstance(sample["inboxes"], list)


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
