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
from api.rate_limiter import limiter
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
        "lead.clicked",
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
        .select("id, client_id, permissions, scopes, revoked, expires_at")
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

    # Check revocation + expiry
    if key_row.get("revoked"):
        raise HTTPException(status_code=401, detail="API key has been revoked.")
    if key_row.get("expires_at"):
        from datetime import datetime as _dt, timezone as _tz
        try:
            exp = _dt.fromisoformat(str(key_row["expires_at"]).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
            if exp < _dt.now(_tz.utc):
                raise HTTPException(status_code=401, detail="API key has expired.")
        except (ValueError, TypeError):
            pass

    # Suspended client → reject
    try:
        client_row = sb.table("clients").select("suspended").eq("id", key_row["client_id"]).limit(1).execute()
        if client_row.data and client_row.data[0].get("suspended"):
            raise HTTPException(status_code=403, detail="Client account suspended.")
    except HTTPException:
        raise
    except Exception:
        pass

    # Fire-and-forget last_used_at update (best effort)
    try:
        sb.table("api_keys").update({"last_used_at": _NOW_UTC()}).eq("id", key_row["id"]).execute()
    except Exception:
        pass

    # Merge legacy permissions + new scopes
    merged_scopes = list((key_row.get("scopes") or [])) + list((key_row.get("permissions") or []))

    return _ApiKeyContext(
        client_id=key_row["client_id"],
        key_id=key_row["id"],
        permissions=merged_scopes,
    )


# ── Scope enforcement helper ───────────────────────────────────────────────
def require_scope(required_scope: str):
    """
    Dependency factory: verify the API key has the required scope.

    Usage:
        @public_router.post("/leads", dependencies=[Depends(require_scope("write:leads"))])
        async def create_lead(...): ...

    Supports wildcard scopes: "read:all" grants access to any "read:*" endpoint.
    """
    from fastapi import Depends

    def checker(ctx: ApiCtx) -> "_ApiKeyContext":
        scopes = set(ctx.permissions or [])
        # Admin / wildcard scope passes everything
        if "admin" in scopes or "*" in scopes:
            return ctx
        # Exact match
        if required_scope in scopes:
            return ctx
        # Category wildcard: required "read:leads" matches scope "read:all"
        category = required_scope.split(":", 1)[0] if ":" in required_scope else required_scope
        if f"{category}:all" in scopes:
            return ctx
        raise HTTPException(
            status_code=403,
            detail=f"API key missing required scope: {required_scope}",
        )

    return Depends(checker)


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


class CampaignSequenceStep(BaseModel):
    """One step in a campaign email sequence (POST /campaigns)."""
    subject:    str
    body:       str
    delay_days: int = Field(default=0, ge=0, le=60)
    step_order: Optional[int] = None


class CampaignIn(BaseModel):
    """Payload for POST /campaigns."""
    name:     str = Field(..., min_length=1, max_length=200)
    steps:    list[CampaignSequenceStep] = Field(default_factory=list, max_length=20)
    settings: Optional[dict] = None


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
@limiter.limit("30/minute")
async def public_create_leads(
    request: Request,
    body: dict,
    ctx: ApiCtx,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Create lead(s). Accepts two body shapes — the response shape depends on it.

    Bulk:
        {"leads": [lead, lead, ...], "campaign_id": ..., "deduplicate": true}
      → {"pushed": N, "failed": N, "duplicates": N, "error_details": [...]}

    Single:
        {"email": "...", "first_name": "...", "campaign_id": ..., "custom_fields": {...}}
      → the inserted row: {"id": "...", "email": "...", ...}

    For new integrations, prefer POST /leads/bulk — it's always bulk and the
    response shape is stable. The single-lead form is supported here for
    compatibility with clients (e.g. Heatr's push_lead) that POST a flat dict.

    Required permission: write_leads
    """
    if isinstance(body, dict) and isinstance(body.get("leads"), list):
        try:
            bulk = BulkLeadIn(**body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid bulk lead payload: {exc}")
        return await _insert_leads_bulk(bulk, ctx, background_tasks)

    try:
        single = LeadIn(**body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid lead payload: {exc}")
    campaign_id = body.get("campaign_id")
    deduplicate = bool(body.get("deduplicate", True))
    return await _insert_lead_single(single, campaign_id, deduplicate, ctx, background_tasks)


async def _insert_lead_single(
    lead: LeadIn,
    campaign_id: Optional[str],
    deduplicate: bool,
    ctx: "_ApiKeyContext",
    background_tasks: BackgroundTasks,
) -> dict:
    """Insert one lead and return the row. 409 on duplicate when deduplicate=True."""
    ctx.require("write_leads")
    sb = _sb()

    email_lower = lead.email.lower()
    if deduplicate:
        existing = (
            sb.table("leads")
            .select("id")
            .eq("client_id", ctx.client_id)
            .eq("email", email_lower)
            .limit(1)
            .execute()
        )
        if existing.data:
            raise HTTPException(status_code=409, detail="Lead with this email already exists.")

    row = {
        "email":         email_lower,
        "first_name":    lead.first_name,
        "last_name":     lead.last_name,
        "company_name":  lead.company_name,
        "job_title":     lead.job_title,
        "domain":        lead.domain or (email_lower.split("@")[1] if "@" in email_lower else None),
        "phone":         lead.phone,
        "linkedin_url":  lead.linkedin_url,
        "custom_fields": lead.custom_fields or {},
        "status":        "new",
        "client_id":     ctx.client_id,
        "imported_at":   _NOW_UTC(),
    }

    try:
        resp = sb.table("leads").insert(row).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to insert lead: {exc}")
    if not resp.data:
        raise HTTPException(status_code=500, detail="Lead insert returned no row.")
    inserted = resp.data[0]
    lead_id = inserted["id"]

    if campaign_id:
        _require_campaign(sb, campaign_id, ctx.client_id)
        try:
            sb.table("campaign_leads").insert({
                "campaign_id": campaign_id,
                "lead_id":     lead_id,
                "client_id":   ctx.client_id,
            }).execute()
        except Exception as exc:
            logger.warning("Single lead %s created but campaign link failed: %s", lead_id, exc)

    try:
        from enrichment_queue import enqueue_leads_bulk
        priority = 1 if campaign_id else 5
        background_tasks.add_task(enqueue_leads_bulk, [lead_id], ctx.client_id, priority)
    except Exception as exc:
        logger.warning("Failed to queue enrichment for lead %s: %s", lead_id, exc)

    return inserted


async def _insert_leads_bulk(
    body: BulkLeadIn,
    ctx: "_ApiKeyContext",
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Bulk-create leads. Max 1000 per request.

    - Deduplication (by email + client_id) is on by default.
    - If campaign_id is provided, leads are also added to campaign_leads.
    - Returns counts: {pushed, failed, duplicates, error_details}.
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
        "pushed":        imported,
        "duplicates":    duplicates,
        "failed":        errors,
        "error_details": error_details[:20],  # cap to avoid huge responses
    }


# Always-bulk endpoint — unambiguous shape, preferred for new integrations.
@public_router.post("/leads/bulk", status_code=201)
@limiter.limit("10/minute")
async def public_create_leads_bulk(
    request: Request,
    body: BulkLeadIn,
    ctx: ApiCtx,
    background_tasks: BackgroundTasks,
) -> dict:
    return await _insert_leads_bulk(body, ctx, background_tasks)


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
# INBOX API (read-only — Heatr capacity checks)
# ===========================================================================

_INBOX_PUBLIC_FIELDS = (
    "id, email, domain, provider, status, reputation_score, "
    "daily_sent, daily_warmup_target, daily_campaign_target"
)


@public_router.get("/inboxes")
async def public_list_inboxes(
    ctx: ApiCtx,
    status: Optional[str] = Query(
        None,
        description="Filter by status (e.g. 'ready', 'warmup', 'paused', 'retired').",
    ),
) -> dict:
    """
    List this client's inboxes. Read-only — inbox management happens in the
    dashboard. Intended for external integrations (e.g. Heatr) to pick a
    sending inbox before pushing leads.

    Returns `{inboxes: [...]}`.

    Required permission: read_analytics
    """
    ctx.require("read_analytics")
    sb = _sb()
    q = sb.table("inboxes").select(_INBOX_PUBLIC_FIELDS).eq("client_id", ctx.client_id)
    if status:
        q = q.eq("status", status)
    resp = q.execute()
    return {"inboxes": resp.data or []}


# In-process cache for inbox-availability reads. 30-second TTL — the
# daily_sent counter only changes when the warmup/campaign engines actually
# send, which happens at most a few times per minute per inbox.
# Integrations that poll this endpoint (e.g. Heatr before each lead push)
# would otherwise hit Supabase once per call.
_AVAILABILITY_CACHE: dict[str, tuple[float, dict]] = {}
_AVAILABILITY_TTL_SECONDS = 30.0


def _availability_cache_get(key: str) -> Optional[dict]:
    import time
    entry = _AVAILABILITY_CACHE.get(key)
    if not entry:
        return None
    inserted_at, value = entry
    if time.monotonic() - inserted_at > _AVAILABILITY_TTL_SECONDS:
        _AVAILABILITY_CACHE.pop(key, None)
        return None
    return value


def _availability_cache_set(key: str, value: dict) -> None:
    import time
    _AVAILABILITY_CACHE[key] = (time.monotonic(), value)
    # Opportunistic eviction — keep the cache bounded even without a TTL sweep
    if len(_AVAILABILITY_CACHE) > 2000:
        now = time.monotonic()
        stale = [
            k for k, (t, _) in _AVAILABILITY_CACHE.items()
            if now - t > _AVAILABILITY_TTL_SECONDS
        ]
        for k in stale:
            _AVAILABILITY_CACHE.pop(k, None)


@public_router.get("/inboxes/{inbox_id}/availability")
async def public_inbox_availability(inbox_id: str, ctx: ApiCtx) -> dict:
    """
    Current sending capacity for a single inbox. `daily_remaining` is the
    number of sends still allowed today across warmup + campaigns combined.

    Response is cached in-process for 30 seconds per (client, inbox) pair.
    Poll at most 2 req/minute per inbox to stay within the cache — beyond
    that you'll still get fresh reads but pay a Supabase round-trip.

    Required permission: read_analytics
    """
    ctx.require("read_analytics")

    cache_key = f"{ctx.client_id}:{inbox_id}"
    cached = _availability_cache_get(cache_key)
    if cached is not None:
        return cached

    sb = _sb()
    resp = (
        sb.table("inboxes")
        .select(_INBOX_PUBLIC_FIELDS)
        .eq("id", inbox_id)
        .eq("client_id", ctx.client_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Inbox not found.")
    inbox = rows[0]

    warmup_target   = int(inbox.get("daily_warmup_target")   or 0)
    campaign_target = int(inbox.get("daily_campaign_target") or 0)
    daily_cap       = warmup_target + campaign_target
    daily_sent      = int(inbox.get("daily_sent") or 0)
    daily_remaining = max(0, daily_cap - daily_sent)

    result = {
        "id":                inbox["id"],
        "email":             inbox.get("email"),
        "status":            inbox.get("status"),
        "reputation_score":  inbox.get("reputation_score"),
        "daily_cap":         daily_cap,
        "daily_sent":        daily_sent,
        "daily_remaining":   daily_remaining,
    }
    _availability_cache_set(cache_key, result)
    return result


# ===========================================================================
# CAMPAIGN API
# ===========================================================================

@public_router.post("/campaigns", status_code=201)
async def public_create_campaign(body: CampaignIn, ctx: ApiCtx) -> dict:
    """
    Create a new campaign in draft status. Sequence steps are optional —
    a campaign can be created empty and steps added later via the private
    API or another integration.

    Returns `{id, name, status, ...}` where `id` is the campaign UUID.

    Required permission: trigger_campaigns
    """
    ctx.require("trigger_campaigns")
    sb = _sb()

    settings = body.settings or {}
    row = {
        "client_id": ctx.client_id,
        "name":      body.name.strip(),
        "status":    "draft",
    }
    for key in ("language", "daily_limit", "timezone", "send_days",
                "send_window_start", "send_window_end",
                "stop_on_reply", "stop_on_unsubscribe", "bounce_threshold"):
        if key in settings and settings[key] is not None:
            row[key] = settings[key]

    resp = sb.table("campaigns").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to create campaign.")
    created = resp.data[0]
    campaign_id = created["id"]

    steps_inserted = 0
    if body.steps:
        step_rows = []
        for i, step in enumerate(body.steps, start=1):
            step_rows.append({
                "campaign_id": campaign_id,
                "client_id":   ctx.client_id,
                "step_order":  step.step_order or i,
                "subject":     step.subject,
                "body":        step.body,
                "delay_days":  step.delay_days,
            })
        try:
            step_resp = sb.table("sequence_steps").insert(step_rows).execute()
            steps_inserted = len(step_resp.data or [])
        except Exception as exc:
            logger.warning("Campaign %s created but sequence_steps insert failed: %s",
                           campaign_id, exc)

    return {
        "id":             campaign_id,
        "campaign_id":    campaign_id,   # alias for clients that read either
        "name":           created.get("name"),
        "status":         created.get("status"),
        "steps_inserted": steps_inserted,
    }


@public_router.post("/campaigns/{campaign_id}/leads", status_code=201)
async def public_add_leads_to_campaign(
    campaign_id: str,
    body: BulkLeadIn,
    ctx: ApiCtx,
    background_tasks: BackgroundTasks,
) -> dict:
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

    body.campaign_id = campaign_id
    return await _insert_leads_bulk(body, ctx, background_tasks)


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

    # Encrypt at rest. The plaintext is returned to the caller ONCE below so
    # they can configure their receiving endpoint. After this POST, the
    # plaintext is unrecoverable from the API (list view never returns it).
    try:
        from utils.secrets_vault import encrypt as _vault_encrypt
        secret_stored = _vault_encrypt(secret)
    except ImportError:
        secret_stored = secret

    row = {
        "client_id":  ctx.client_id,
        "url":        body.url,
        "events":     body.events,
        "secret":     secret_stored,
        "active":     True,
        "created_at": _NOW_UTC(),
    }
    resp = sb.table("webhooks").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to register webhook.")

    webhook = resp.data[0]
    webhook["secret"] = secret  # return PLAINTEXT secret once (never again)
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
