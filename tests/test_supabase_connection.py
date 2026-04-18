"""
tests/test_supabase_connection.py — Verify Supabase connection and workspace row.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from supabase import create_client

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_KEY"]

print("=== Step 3 — Supabase connection test ===")
print(f"URL: {url}")
print(f"KEY: {key[:12]}...{key[-6:]}")
print()

CORE_TABLES   = ["clients", "inboxes", "domains", "warmup_logs", "reply_inbox", "bounce_log"]
MISSING_TABLES = ["campaign_sends", "warmup_network", "blacklist_checks", "ab_variants", "daily_metrics"]

try:
    sb = create_client(url, key)
    print("Client created — checking core tables...")
    all_present = True
    for t in CORE_TABLES:
        try:
            res = sb.table(t).select("id", count="exact").execute()
            print(f"  EXISTS  {t:<25} ({res.count or 0} rows)")
        except Exception as e:
            print(f"  MISSING {t} — {e}")
            all_present = False

    print()
    print("Checking clients table (equivalent of workspaces)...")
    clients = sb.table("clients").select("*").execute()
    if clients.data:
        for c in clients.data:
            print(f"  id={c.get('id')}  company={c.get('company_name')}  plan={c.get('plan')}")
    else:
        print("  (no client rows yet — this is expected before first login)")

    print()
    if all_present:
        print("RESULT: PASS — Supabase connected, all core tables present")
    else:
        print("RESULT: PARTIAL — connected but some tables missing (run full supabase_schema.sql)")
except Exception as e:
    print(f"RESULT: FAIL — {type(e).__name__}: {e}")
