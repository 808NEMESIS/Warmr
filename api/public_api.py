"""
api/public_api.py

Warmr Public API — machine-to-machine endpoints for external integrations.

Authentication:
    Pass your API key in the Authorization header:
        Authorization: Bearer wrmr_<your_key>

    Keys are created via the dashboard (POST /apikeys).
    The raw key is shown exactly once on creation.
    Only a SHA-256 hash is stored in the database.

Base URL:  /api/v1/...
Versioning: URL-based (v1). Breaking changes will increment to v2.

Supported permissions (per key):
    read_leads         — GET leads and lead status
    write_leads        — POST/PATCH leads, enrich
    trigger_campaigns  — add leads to campaigns, pause/resume
    read_analytics     — GET campaign stats

Mounted in main.py:
    app.include_router(public_router, prefix="/api/v1")
"""

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from api.models import WebhookPatch
from supabase import create_client, Client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase (service role — RLS bypass, we enforce client_id manually)
# ---------------------------------------------------------------------------
_SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

def _sb() -> Client:
    """Return a fresh Supabase client. Cheap — client is stateless."""
    return create_client(_SUPABASE_URL, _SUPABASE_KEY)


# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=True)
_NOW_UTC = lambda: datetime.now(timezone.utc).isoformat()

VALID_PERMISSIONS = frozenset(
    {"read_leads", "write_leads", "trigger_campaigns", "read_analytics"}
)
VALID_EVENTS = frozenset(
    {
        "lead.replied",
        "lead.interested",
        "lead.bounced",
        "lead.unsubscribed",
        "lead.enriched",
        "inbox.warmup_complete",
        "campaign.completed",
    }
)


def _hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of a raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


class _ApiKeyContext:
    """Resolved API key — passed to every protected endpoint."""

    def __init__(self, client_id: str, key_id: str, permissions: list[str]):
        self.client_id   = client_id
        self.key_id      = key_id
        self.permissions = set(permissions)

    def require(self, *perms: str) -> None:
        """Raise HTTP 403 if any required permission is missing."""
        missing = set(perms) - self.permissions
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key missing required permission(s): {', '.join(sorted(missing))}",
            )


async def _get_api_key_context(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> _ApiKeyContext:
    """
    FastAPI dependency: validate the Bearer API key and return context.

    Looks up key_hash in api_keys, updates last_used_at, and returns
    the client_id + permissions for the key.
    """
    raw_key = credentials.credentials
    key_hash = _hash_key(raw_key)

    sb = _sb()
    resp = (
        sb.table("api_keys")
        .select("id, client_id, permissions")
        .eq("key_hash", key_hash)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key.",
        )

    key_row = rows[0]
    # Fire-and-forget last_used_at update (best effort)
    try:
        sb.table("api_keys").update({"last_used_at": _NOW_UTC()}).eq("id", key_row["id"]).execute()
    except Exception:
        pass

    return _ApiKeyContext(
        client_id=key_row["client_id"],
        key_id=key_row["id"],
        permissions=key_row.get("permissions") or [],
    )


ApiCtx = Annotated[_ApiKeyContext, Depends(_get_api_key_context)]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

public_router = APIRouter(tags=["Public API v1"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class LeadIn(BaseModel):
    """One lead in a bulk import or single create request."""
    email:        EmailStr
    first_name:   Optional[str] = None
    last_name:    Optional[str] = None
    company_name: Optional[str] = None
    job_title:    Optional[str] = None
    domain:       Optional[str] = None
    phone:        Optional[str] = None
    linkedin_url: Optional[str] = None
    custom_fields: Optional[dict] = None


class LeadPatch(BaseModel):
    """Allowed fields for PATCH /leads/{id}."""
    first_name:    Optional[str] = None
    last_name:     Optional[str] = None
    company_name:  Optional[str] = None
    job_title:     Optional[str] = None
    phone:         Optional[str] = None
    linkedin_url:  Optional[str] = None
    custom_fields: Optional[dict] = None
    verified:      Optional[bool] = None
    enriched:      Optional[bool] = None
    notes:         Optional[str] = None


class BulkLeadIn(BaseModel):
    """Payload for POST /leads — up to 1000 leads per request."""
    leads:       list[LeadIn] = Field(..., max_length=1000)
    campaign_id: Optional[str] = None   # optionally attach to a campaign immediately
    deduplicate: bool = True             # skip leads whose email already exists for this client


class WebhookIn(BaseModel):
    url:    str = Field(..., min_length=8)
    events: list[str]
    name:   Optional[str] = None

    def validate_events(self) -> None:
        invalid = set(self.events) - VALID_EVENTS
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown event type(s): {', '.join(sorted(invalid))}. "
                       f"Supported: {', '.join(sorted(VALID_EVENTS))}",
            )


class ApiKeyIn(BaseModel):
    name:        str  = Field(..., min_length=1, max_length=100)
    permissions: list[str]

    def validate_permissions(self) -> None:
        invalid = set(self.permissions) - VALID_PERMISSIONS
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown permission(s): {', '.join(sorted(invalid))}. "
                       f"Supported: {', '.join(sorted(VALID_PERMISSIONS))}",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_campaign(sb: Client, campaign_id: str, client_id: str) -> dict:
    """Fetch a campaign row and verify ownership. Raises 404/403."""
    resp = sb.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    if rows[0].get("client_id") != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    return rows[0]


def _emit_event(sb: Client, client_id: str, event_type: str, payload: dict) -> None:
    """
    Write an event to webhook_events for the dispatcher to pick up.
    Non-blocking — errors are logged but not raised to the caller.
    """
    try:
        sb.table("webhook_events").insert({
            "client_id":  client_id,
            "event_type": event_type,
            "payload":    payload,
            "dispatched": False,
            "created_at": _NOW_UTC(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to emit webhook event %s: %s", event_type, exc)


# ===========================================================================
# LEADS API
# ===========================================================================

@public_router.post("/leads", status_code=201)
async def public_create_leads(body: BulkLeadIn, ctx: ApiCtx, background_tasks: BackgroundTasks) -> dict:
    """
    Bulk-create leads. Max 1000 per request.

    - Deduplication (by email + client_id) is on by default.
    - If campaign_id is provided, leads are also added to campaign_leads.
    - Returns counts of imported, duplicate, and errored rows.

    Required permission: write_leads
    """
    ctx.require("write_leads")
    sb = _sb()

    imported   = 0
    duplicates = 0
    errors     = 0
    error_details: list[str] = []
    inserted_ids: list[str] = []

    # Fetch existing emails for this client in one query for dedup
    existing_emails: set[str] = set()
    if body.deduplicate:
        existing_resp = (
            sb.table("leads")
            .select("email")
            .eq("client_id", ctx.client_id)
            .execute()
        )
        existing_emails = {r["email"].lower() for r in (existing_resp.data or [])}

    rows_to_insert = []
    for lead in body.leads:
        email_lower = lead.email.lower()
        if body.deduplicate and email_lower in existing_emails:
            duplicates += 1
            continue
        existing_emails.add(email_lower)
        rows_to_insert.append({
            "email":        email_lower,
            "first_name":   lead.first_name,
            "last_name":    lead.last_name,
            "company_name": lead.company_name,
            "job_title":    lead.job_title,
            "domain":       lead.domain or (email_lower.split("@")[1] if "@" in email_lower else None),
            "phone":        lead.phone,
            "linkedin_url": lead.linkedin_url,
            "custom_fields": lead.custom_fields or {},
            "status":       "new",
            "client_id":    ctx.client_id,
            "imported_at":  _NOW_UTC(),
        })

    # Batch insert in chunks of 500
    CHUNK = 500
    for i in range(0, len(rows_to_insert), CHUNK):
        chunk = rows_to_insert[i : i + CHUNK]
        try:
            resp = sb.table("leads").insert(chunk).execute()
            batch_ids = [r["id"] for r in (resp.data or [])]
            inserted_ids.extend(batch_ids)
            imported += len(batch_ids)
        except Exception as exc:
            errors += len(chunk)
            error_details.append(str(exc))

    # Attach to campaign if requested
    has_active_campaign = False
    if body.campaign_id and inserted_ids:
        _require_campaign(sb, body.campaign_id, ctx.client_id)
        camp_rows = [
            {"campaign_id": body.campaign_id, "lead_id": lid, "client_id": ctx.client_id}
            for lid in inserted_ids
        ]
        for i in range(0, len(camp_rows), CHUNK):
            try:
                sb.table("campaign_leads").insert(camp_rows[i : i + CHUNK]).execute()
            except Exception as exc:
                logger.warning("Failed to link leads to campaign: %s", exc)
        has_active_campaign = True

    # Queue enrichment for all newly inserted leads (background — does not block response)
    if inserted_ids:
        from enrichment_queue import enqueue_leads_bulk
        priority = 1 if has_active_campaign else 5
        background_tasks.add_task(
            enqueue_leads_bulk, inserted_ids, ctx.client_id, priority
        )

    return {
        "imported":     imported,
        "duplicates":   duplicates,
        "errors":       errors,
        "error_details": error_details[:20],  # cap to avoid huge responses
    }


@public_router.get("/leads/{lead_id}")
async def public_get_lead(lead_id: str, ctx: ApiCtx) -> dict:
    """
    Fetch a single lead with full status.

    Required permission: read_leads
    """
    ctx.require("read_leads")
    sb = _sb()
    resp = sb.table("leads").select("*").eq("id", lead_id).eq("client_id", ctx.client_id).limit(1).execute()
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return rows[0]


@public_router.patch("/leads/{lead_id}")
async def public_patch_lead(lead_id: str, body: LeadPatch, ctx: ApiCtx) -> dict:
    """
    Update enrichment fields on a lead.

    Required permission: write_leads
    """
    ctx.require("write_leads")
    sb = _sb()

    # Ownership check
    check = sb.table("leads").select("id").eq("id", lead_id).eq("client_id", ctx.client_id).limit(1).execute()
    if not (check.data or []):
        raise HTTPException(status_code=404, detail="Lead not found.")

    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update.")

    resp = sb.table("leads").update(patch).eq("id", lead_id).execute()
    return resp.data[0] if resp.data else {}


@public_router.post("/leads/{lead_id}/enrich", status_code=202)
async def public_enrich_lead(lead_id: str, ctx: ApiCtx) -> dict:
    """
    Mark a lead as pending enrichment.

    Sets enriched = false so the enrichment worker picks it up on its next run.
    The worker sets enriched = true when done.

    Required permission: write_leads
    """
    ctx.require("write_leads")
    sb = _sb()
    check = sb.table("leads").select("id").eq("id", lead_id).eq("client_id", ctx.client_id).limit(1).execute()
    if not (check.data or []):
        raise HTTPException(status_code=404, detail="Lead not found.")
    from enrichment_queue import enqueue_lead
    queued = enqueue_lead(lead_id, ctx.client_id)
    return {"queued": queued, "lead_id": lead_id}


@public_router.get("/leads")
async def public_list_leads(
    ctx:         ApiCtx,
    status:      Optional[str] = Query(default=None, description="new|active|bounced|replied|unsubscribed"),
    campaign_id: Optional[str] = Query(default=None),
    limit:       int           = Query(default=100, ge=1, le=1000),
    offset:      int           = Query(default=0, ge=0),
) -> list[dict]:
    """
    List leads with optional filters.

    Required permission: read_leads
    """
    ctx.require("read_leads")
    sb = _sb()

    q = sb.table("leads").select("*").eq("client_id", ctx.client_id)
    if status:
        q = q.eq("status", status)
    if campaign_id:
        # Filter via campaign_leads join
        cl_resp = sb.table("campaign_leads").select("lead_id").eq("campaign_id", campaign_id).eq("client_id", ctx.client_id).execute()
        lead_ids = [r["lead_id"] for r in (cl_resp.data or [])]
        if not lead_ids:
            return []
        q = q.in_("id", lead_ids)

    resp = q.order("imported_at", desc=True).range(offset, offset + limit - 1).execute()
    return resp.data or []


# ===========================================================================
# CAMPAIGN API
# ===========================================================================

@public_router.post("/campaigns/{campaign_id}/leads", status_code=201)
async def public_add_leads_to_campaign(campaign_id: str, body: BulkLeadIn, ctx: ApiCtx) -> dict:
    """
    Add leads to an existing campaign.

    Leads that don't exist yet are created first (write_leads required).
    Leads that already exist are linked directly.

    Required permissions: trigger_campaigns + write_leads
    """
    ctx.require("trigger_campaigns", "write_leads")
    sb = _sb()
    campaign = _require_campaign(sb, campaign_id, ctx.client_id)

    if campaign.get("status") not in ("draft", "active"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot add leads to a campaign with status '{campaign.get('status')}'.",
        )

    # Reuse bulk create logic — campaign_id is set
    body.campaign_id = campaign_id
    return await public_create_leads(body, ctx)


@public_router.get("/campaigns/{campaign_id}/stats")
async def public_campaign_stats(campaign_id: str, ctx: ApiCtx) -> dict:
    """
    Real-time campaign statistics.

    Required permission: read_analytics
    """
    ctx.require("read_analytics")
    sb = _sb()
    campaign = _require_campaign(sb, campaign_id, ctx.client_id)

    events_resp = (
        sb.table("email_events")
        .select("event_type")
        .eq("campaign_id", campaign_id)
        .eq("client_id", ctx.client_id)
        .execute()
    )
    events = events_resp.data or []

    counts: dict[str, int] = {}
    for e in events:
        t = e.get("event_type") or "unknown"
        counts[t] = counts.get(t, 0) + 1

    sent       = counts.get("sent", 0)
    opened     = counts.get("opened", 0)
    clicked    = counts.get("clicked", 0)
    replied    = counts.get("replied", 0)
    bounced    = counts.get("bounced", 0)
    unsubscribed = counts.get("unsubscribed", 0)

    return {
        "campaign_id":   campaign_id,
        "name":          campaign.get("name"),
        "status":        campaign.get("status"),
        "sent":          sent,
        "opened":        opened,
        "clicked":       clicked,
        "replied":       replied,
        "bounced":       bounced,
        "unsubscribed":  unsubscribed,
        "open_rate":     round(opened  / sent, 4) if sent else 0.0,
        "reply_rate":    round(replied / sent, 4) if sent else 0.0,
        "bounce_rate":   round(bounced / sent, 4) if sent else 0.0,
    }


@public_router.post("/campaigns/{campaign_id}/pause")
async def public_pause_campaign(campaign_id: str, ctx: ApiCtx) -> dict:
    """
    Pause an active campaign externally.

    Required permission: trigger_campaigns
    """
    ctx.require("trigger_campaigns")
    sb = _sb()
    campaign = _require_campaign(sb, campaign_id, ctx.client_id)

    if campaign.get("status") == "paused":
        return {"ok": True, "status": "paused", "message": "Already paused."}

    sb.table("campaigns").update({"status": "paused"}).eq("id", campaign_id).execute()
    return {"ok": True, "status": "paused", "campaign_id": campaign_id}


@public_router.post("/campaigns/{campaign_id}/resume")
async def public_resume_campaign(campaign_id: str, ctx: ApiCtx) -> dict:
    """
    Resume a paused campaign.

    Required permission: trigger_campaigns
    """
    ctx.require("trigger_campaigns")
    sb = _sb()
    campaign = _require_campaign(sb, campaign_id, ctx.client_id)

    if campaign.get("status") == "active":
        return {"ok": True, "status": "active", "message": "Already active."}
    if campaign.get("status") not in ("paused", "draft"):
        raise HTTPException(status_code=409, detail=f"Cannot resume a campaign with status '{campaign.get('status')}'.")

    sb.table("campaigns").update({"status": "active"}).eq("id", campaign_id).execute()
    return {"ok": True, "status": "active", "campaign_id": campaign_id}


# ===========================================================================
# WEBHOOK MANAGEMENT
# ===========================================================================

@public_router.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookIn, ctx: ApiCtx) -> dict:
    """
    Register a new webhook endpoint.

    Returns the webhook id and a signing secret.
    The secret is shown only once — store it securely.
    Warmr signs every delivery with HMAC-SHA256 using this secret.

    No specific permission required (API key holder manages their own webhooks).
    """
    body.validate_events()

    secret = secrets.token_hex(32)  # 64-char hex signing secret
    sb = _sb()

    row = {
        "client_id":  ctx.client_id,
        "url":        body.url,
        "events":     body.events,
        "secret":     secret,
        "active":     True,
        "created_at": _NOW_UTC(),
    }
    resp = sb.table("webhooks").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to register webhook.")

    webhook = resp.data[0]
    webhook["secret"] = secret  # return secret once
    return webhook


@public_router.get("/webhooks")
async def list_webhooks(ctx: ApiCtx) -> list[dict]:
    """List all webhooks for this client (secrets are omitted)."""
    sb = _sb()
    resp = (
        sb.table("webhooks")
        .select("id, url, events, active, created_at")
        .eq("client_id", ctx.client_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@public_router.patch("/webhooks/{webhook_id}")
async def update_webhook(webhook_id: str, body: WebhookPatch, ctx: ApiCtx) -> dict:
    """Update webhook url, events, or active status."""
    sb = _sb()
    check = sb.table("webhooks").select("client_id").eq("id", webhook_id).limit(1).execute()
    rows = check.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    if rows[0]["client_id"] != ctx.client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    patch = body.model_dump(exclude_none=True)
    if "events" in patch:
        invalid = set(patch["events"]) - VALID_EVENTS
        if invalid:
            raise HTTPException(status_code=422, detail=f"Unknown events: {', '.join(invalid)}")

    resp = sb.table("webhooks").update(patch).eq("id", webhook_id).execute()
    return resp.data[0] if resp.data else {}


@public_router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str, ctx: ApiCtx) -> None:
    """Delete a webhook and all its delivery logs."""
    sb = _sb()
    check = sb.table("webhooks").select("client_id").eq("id", webhook_id).limit(1).execute()
    rows = check.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    if rows[0]["client_id"] != ctx.client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    sb.table("webhook_logs").delete().eq("webhook_id", webhook_id).execute()
    sb.table("webhooks").delete().eq("id", webhook_id).execute()


@public_router.get("/webhooks/{webhook_id}/logs")
async def webhook_logs(
    webhook_id: str,
    ctx:        ApiCtx,
    limit:      int = Query(default=50, le=200),
) -> list[dict]:
    """Return delivery logs for a specific webhook, newest first."""
    sb = _sb()
    check = sb.table("webhooks").select("client_id").eq("id", webhook_id).limit(1).execute()
    rows = check.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    if rows[0]["client_id"] != ctx.client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    resp = (
        sb.table("webhook_logs")
        .select("id, event_type, response_status, attempt_count, success, timestamp")
        .eq("webhook_id", webhook_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


# ===========================================================================
# API KEY MANAGEMENT  (JWT-authenticated — managed via dashboard, not API key)
# These endpoints are mounted on the main FastAPI app, not the public_router,
# because they require a Supabase JWT (dashboard session), not an API key.
# They are defined here for proximity but exported as `apikey_router`.
# ===========================================================================

from fastapi import APIRouter as _APIRouter
from api.auth import get_current_client as _get_jwt_client

apikey_router = _APIRouter(tags=["API Keys"])
_JwtClient = Annotated[str, Depends(_get_jwt_client)]


@apikey_router.get("/apikeys")
async def list_api_keys(client_id: _JwtClient) -> list[dict]:
    """List all API keys for the authenticated client (hashes and secrets omitted)."""
    sb = _sb()
    resp = (
        sb.table("api_keys")
        .select("id, name, permissions, last_used_at, created_at")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@apikey_router.post("/apikeys", status_code=201)
async def create_api_key(body: ApiKeyIn, client_id: _JwtClient) -> dict:
    """
    Create a new API key.

    Returns the raw key exactly once. Store it immediately — it cannot be
    retrieved again. Only a SHA-256 hash is stored server-side.
    """
    body.validate_permissions()

    raw_key  = "wrmr_" + secrets.token_hex(32)   # wrmr_<64 hex chars>
    key_hash = _hash_key(raw_key)

    sb = _sb()
    resp = sb.table("api_keys").insert({
        "client_id":   client_id,
        "key_hash":    key_hash,
        "name":        body.name,
        "permissions": body.permissions,
        "created_at":  _NOW_UTC(),
    }).execute()

    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to create API key.")

    result = resp.data[0]
    result["key"] = raw_key  # returned once only — not stored
    del result["key_hash"]   # never expose the hash
    return result


@apikey_router.delete("/apikeys/{key_id}", status_code=204)
async def revoke_api_key(key_id: str, client_id: _JwtClient) -> None:
    """Revoke (delete) an API key. Existing requests using the key will fail immediately."""
    sb = _sb()
    check = sb.table("api_keys").select("client_id").eq("id", key_id).limit(1).execute()
    rows = check.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="API key not found.")
    if rows[0]["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    sb.table("api_keys").delete().eq("id", key_id).execute()


# ===========================================================================
# Webhook event emission helper (used by main.py and backend scripts)
# ===========================================================================

def emit_webhook_event(client_id: str, event_type: str, payload: dict) -> None:
    """
    Public helper: write an event to webhook_events.

    Import and call this from main.py or any backend script when an event occurs:
        from api.public_api import emit_webhook_event
        emit_webhook_event(client_id, "lead.replied", {"lead_id": ..., ...})
    """
    sb = _sb()
    _emit_event(sb, client_id, event_type, payload)
