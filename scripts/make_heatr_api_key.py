"""
scripts/make_heatr_api_key.py — provision a Warmr API key for Heatr in one step.

What it does
------------
1. Calls Warmr's POST /apikeys with the four permissions Heatr's
   warmr_client.py uses (read_leads, write_leads, trigger_campaigns,
   read_analytics).
2. Prints the resulting `wrmr_<64hex>` key in the exact env format
   Heatr's .env reads (WARMR_API_URL + WARMR_API_KEY).
3. Optionally appends those two lines to a target .env file with --write-to,
   replacing existing WARMR_API_URL / WARMR_API_KEY entries if present.

Why this script and not just curl
---------------------------------
- The exact permissions match what warmr_client.py needs — no guessing.
- The output format matches Heatr's .env.example variable names (it's
  WARMR_API_URL, not BASE_URL — easy to get wrong by hand).
- The raw key is shown exactly once by Warmr and is unrecoverable; the
  script avoids the "I already closed the curl response" mistake.

Auth
----
You need a dashboard JWT (the same one your browser uses). Get it from:
    F12 → Application → Local Storage → sb-<project>-auth-token
    → JSON.parse the value → access_token

Pass it via --jwt or WARMR_OPERATOR_JWT.

Usage
-----
    python scripts/make_heatr_api_key.py \
        --warmr-url https://your-warmr.com \
        --jwt eyJhbGc... \
        --write-to ~/Heatr/.env

    # Or via env vars:
    export WARMR_API_URL=https://your-warmr.com
    export WARMR_OPERATOR_JWT=eyJhbGc...
    python scripts/make_heatr_api_key.py --write-to ~/Heatr/.env
"""

from __future__ import annotations

import argparse
import os
import re

# Permissions Heatr's warmr_client.py needs across its endpoints.
# read_leads      — get_inbox_availability, GET /leads (if used)
# write_leads     — push_lead, push_leads_bulk
# trigger_campaigns — create_campaign, pause/resume
# read_analytics  — get_campaign_stats, get_ready_inboxes
HEATR_PERMISSIONS = ["read_leads", "write_leads", "trigger_campaigns", "read_analytics"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--warmr-url",
        default=os.getenv("WARMR_API_URL", "").rstrip("/").removesuffix("/api/v1"),
        help="Warmr base URL WITHOUT /api/v1 suffix (e.g. https://your-warmr.com).",
    )
    parser.add_argument(
        "--jwt",
        default=os.getenv("WARMR_OPERATOR_JWT", ""),
        help="Dashboard JWT access_token from localStorage (sb-...-auth-token).",
    )
    parser.add_argument(
        "--name",
        default="Heatr integration",
        help="Human-readable name for the API key (shown in dashboard).",
    )
    parser.add_argument(
        "--write-to",
        type=str,
        default=None,
        help="Path to a .env file to append/update with WARMR_API_URL + WARMR_API_KEY.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Skip writing — only print the env snippet to stdout.",
    )
    args = parser.parse_args()

    if not args.warmr_url:
        print("ERROR: --warmr-url or WARMR_API_URL env must be set.")
        return 1
    if not args.jwt:
        print(
            "ERROR: --jwt or WARMR_OPERATOR_JWT env must be set.\n"
            "       Get it from your browser's localStorage on the Warmr dashboard:\n"
            "       F12 → Application → Local Storage → sb-...-auth-token → access_token."
        )
        return 1

    # Lazy import — avoids hard dep on httpx for users who only run --help.
    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed. pip install httpx")
        return 1

    create_url = args.warmr_url + "/apikeys"
    print(f"→ POST {create_url}")
    try:
        resp = httpx.post(
            create_url,
            headers={
                "Authorization": f"Bearer {args.jwt}",
                "Content-Type": "application/json",
            },
            json={"name": args.name, "permissions": HEATR_PERMISSIONS},
            timeout=15,
        )
    except Exception as exc:
        print(f"ERROR: request failed: {exc}")
        return 2

    if resp.status_code != 201:
        print(f"ERROR: Warmr returned {resp.status_code}")
        print(resp.text[:1000])
        return 3

    body = resp.json()
    raw_key = body.get("key", "")
    if not raw_key.startswith("wrmr_"):
        print(f"ERROR: unexpected response shape: {body}")
        return 4

    api_url = args.warmr_url.rstrip("/") + "/api/v1"
    snippet = (
        f"# Warmr API credentials (generated {body.get('id', '?')[:8]} · {args.name})\n"
        f"WARMR_API_URL={api_url}\n"
        f"WARMR_API_KEY={raw_key}\n"
    )

    print()
    print("─" * 60)
    print("API key created. Save the following — Warmr cannot reveal it again:")
    print("─" * 60)
    print(snippet)

    if args.write_to and not args.print_only:
        target = os.path.expanduser(args.write_to)
        existing = ""
        if os.path.exists(target):
            with open(target, encoding="utf-8") as fh:
                existing = fh.read()

        # Replace existing WARMR_API_URL / WARMR_API_KEY lines if present.
        if "WARMR_API_URL" in existing or "WARMR_API_KEY" in existing:
            existing = re.sub(r"^WARMR_API_URL=.*$", f"WARMR_API_URL={api_url}", existing, flags=re.MULTILINE)
            existing = re.sub(r"^WARMR_API_KEY=.*$", f"WARMR_API_KEY={raw_key}", existing, flags=re.MULTILINE)
            new_content = existing
            action = "updated"
        else:
            sep = "" if existing.endswith("\n") or not existing else "\n"
            new_content = existing + sep + "\n" + snippet
            action = "appended"

        with open(target, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        print(f"✓ {action} to {target}")
    else:
        print("(no --write-to specified — copy the lines above into Heatr's .env manually)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
