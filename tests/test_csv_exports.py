"""
tests/test_csv_exports.py — verify the 5 CSV export endpoints are registered
and that the CSV-helpers used by them produce well-formed output.

Endpoint behaviour against a live Supabase is not covered here — the helpers
are pure-Python and Supabase isolation is already exercised by
test_backend_service_role_isolation.py.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Route registration ──────────────────────────────────────────────────

EXPECTED_ROUTES = [
    "/export/leads.csv",
    "/export/email-events.csv",
    "/export/replies.csv",
    "/export/cost-breakdown.csv",
    "/export/inbox-reputation-history.csv",
]


def test_all_csv_export_routes_registered():
    from api.main import app
    paths = {r.path for r in app.routes if hasattr(r, "methods") and r.methods}
    for route in EXPECTED_ROUTES:
        assert route in paths, f"Missing CSV export route: {route}"


# ── _csv_value: type coercion ───────────────────────────────────────────

def test_csv_value_handles_none():
    from api.main import _csv_value
    assert _csv_value(None) == ""


def test_csv_value_handles_primitives():
    from api.main import _csv_value
    assert _csv_value("hello") == "hello"
    assert _csv_value(42) == "42"
    assert _csv_value(3.14) == "3.14"
    assert _csv_value(True) == "True"


def test_csv_value_serialises_dict_to_json():
    from api.main import _csv_value
    out = _csv_value({"b": 2, "a": 1})
    # Sorted keys so output is deterministic for diff'ing exports
    assert out == '{"a": 1, "b": 2}'


def test_csv_value_serialises_list_to_json():
    from api.main import _csv_value
    assert _csv_value([1, 2, 3]) == "[1, 2, 3]"


# ── _flatten_jsonb: prefix expansion ────────────────────────────────────

def test_flatten_jsonb_pops_and_prefixes_keys():
    from api.main import _flatten_jsonb
    rows = [{"id": "L1", "email": "a@x.nl", "custom_fields": {"score": 78, "city": "Utrecht"}}]
    out = _flatten_jsonb(rows, "custom_fields", prefix="cf")
    assert out[0]["id"] == "L1"
    assert out[0]["cf_score"] == 78
    assert out[0]["cf_city"] == "Utrecht"
    assert "custom_fields" not in out[0], "JSONB column should be removed after flattening"


def test_flatten_jsonb_handles_missing_column():
    from api.main import _flatten_jsonb
    rows = [{"id": "L1", "email": "a@x.nl"}]  # no custom_fields
    out = _flatten_jsonb(rows, "custom_fields", prefix="cf")
    assert out[0]["id"] == "L1"


def test_flatten_jsonb_handles_null_value():
    from api.main import _flatten_jsonb
    rows = [{"id": "L1", "intent_analysis": None}]
    out = _flatten_jsonb(rows, "intent_analysis", prefix="intent")
    assert out[0]["id"] == "L1"
    assert "intent_analysis" not in out[0]


# ── _csv_response: full pipeline ────────────────────────────────────────

def test_csv_response_priority_columns_come_first():
    """`id` and `created_at` should appear before custom columns."""
    from api.main import _csv_response
    rows = [{"zebra": 1, "id": "L1", "alpha": 2, "created_at": "2026-01-01"}]
    resp = _csv_response(rows, "test.csv")
    body = resp.body.decode()
    header = body.splitlines()[0].split(",")
    # id first, created_at before alphabetical rest
    assert header.index("id") < header.index("alpha")
    assert header.index("created_at") < header.index("alpha")


def test_csv_response_empty_rows_returns_empty_body():
    from api.main import _csv_response
    resp = _csv_response([], "empty.csv")
    assert resp.body == b""


def test_csv_response_attachment_header():
    from api.main import _csv_response
    resp = _csv_response([{"id": "x"}], "warmr-leads.csv")
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "warmr-leads.csv" in cd


def test_csv_response_dict_value_serialised_as_json_in_cell():
    """A dict cell value must end up as a parseable JSON string in the CSV."""
    from api.main import _csv_response
    rows = [{"id": "L1", "metadata": {"a": 1, "b": 2}}]
    resp = _csv_response(rows, "x.csv")
    body = resp.body.decode()
    parsed = list(csv.DictReader(io.StringIO(body)))
    import json as _j
    assert _j.loads(parsed[0]["metadata"]) == {"a": 1, "b": 2}


def test_csv_response_extra_keys_in_some_rows_are_handled():
    """Rows with sparse keys still produce a valid CSV (header is the union)."""
    from api.main import _csv_response
    rows = [
        {"id": "L1", "email": "a@x.nl"},
        {"id": "L2", "email": "b@x.nl", "phone": "+31..."},
    ]
    resp = _csv_response(rows, "x.csv")
    body = resp.body.decode()
    parsed = list(csv.DictReader(io.StringIO(body)))
    assert parsed[0]["phone"] == ""
    assert parsed[1]["phone"] == "+31..."


if __name__ == "__main__":
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        total += 1
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
