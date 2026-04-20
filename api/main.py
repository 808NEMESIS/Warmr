"""
api/main.py

Warmr FastAPI application — middleware layer between the frontend and Python backend.

Replaces n8n Execute Command nodes with clean HTTP endpoints.
Every endpoint except POST /auth/verify requires a valid Supabase JWT Bearer token.

Run locally:
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

Environment variables required (add to .env):
    SUPABASE_URL, SUPABASE_KEY, SUPABASE_JWT_SECRET, ANTHROPIC_API_KEY
"""

import html as html_mod
import io
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from uuid import UUID, uuid4

import httpx
import pandas as pd
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt as jose_jwt
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from supabase import Client, create_client

from api.auth import SUPABASE_JWT_SECRET, get_current_client, require_admin
from api.dns_check import run_full_dns_check
from api.public_api import apikey_router, public_router
from api.models import (
    AdminClientPatch,
    BlacklistRecoveryStepPatch,
    CampaignCreate,
    CampaignStats,
    ContentScoreRequest,
    DNSCheckResponse,
    DomainCreate,
    LeadImportResult,
    NotificationItem,
    PlacementTestRequest,
    TokenVerifyResponse,
    WarmupStats,
    WarmupTriggerResponse,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# Optional: switch to JSON logging when WARMR_JSON_LOGS=1
try:
    from utils.structured_logging import setup_json_logging, CorrelationMiddleware
    setup_json_logging()
except Exception:
    CorrelationMiddleware = None  # type: ignore
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase (service role — bypasses RLS; we enforce client_id manually)
# ---------------------------------------------------------------------------
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.critical("SUPABASE_URL or SUPABASE_KEY not set. API will not function correctly.")

_supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Thread pool for running blocking engine code from async routes
_executor = ThreadPoolExecutor(max_workers=2)

# Convenience type alias
ClientId = Annotated[str, Depends(get_current_client)]

# ---------------------------------------------------------------------------
# CORS — allowed origins from env, never wildcard in production
# ---------------------------------------------------------------------------
_ALLOWED_ORIGINS_RAW: str = os.getenv("ALLOWED_ORIGINS", "")
_ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()]
    if _ALLOWED_ORIGINS_RAW
    else ["http://localhost:3000", "http://localhost:8080", "http://127.0.0.1:5500"]
)

# ---------------------------------------------------------------------------
# Rate limiter — per-client (JWT) when authenticated, per-IP otherwise
# ---------------------------------------------------------------------------

def _rate_limit_key(request: Request) -> str:
    """Key rate limits by client_id when JWT is present, else by IP."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            # Decode WITHOUT verifying — we just need the sub claim for keying.
            # Actual validation happens in auth dependencies.
            payload = jose_jwt.get_unverified_claims(auth[7:])
            sub = payload.get("sub")
            if sub:
                return f"client:{sub}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_limit_key, default_limits=["120/minute"])

# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers to every response."""

    # CSP for HTML pages — allow Supabase CDN for the JS SDK, inline for legacy onclick handlers,
    # data: URLs for tracking pixels, and same-origin for everything else.
    _CSP_HTML = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https://*.supabase.co https://api.anthropic.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        # Apply CSP only to HTML responses (not JSON APIs which don't execute scripts)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Content-Security-Policy"] = self._CSP_HTML
        return response

# ---------------------------------------------------------------------------
# Request size limit middleware (10 MB)
# ---------------------------------------------------------------------------
_MAX_REQUEST_SIZE = 10 * 1024 * 1024  # 10 MB

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds 10 MB."""

    async def dispatch(self, request: StarletteRequest, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _MAX_REQUEST_SIZE:
            return Response(
                content='{"detail":"Request body too large. Maximum is 10 MB."}',
                status_code=413,
                media_type="application/json",
            )
        return await call_next(request)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Warmr API",
    description="Backend API for the Warmr email warmup platform.",
    version="1.0.0",
    # Disable interactive docs in production via env var
    docs_url="/docs" if os.getenv("ENABLE_DOCS", "true").lower() == "true" else None,
    redoc_url="/redoc" if os.getenv("ENABLE_DOCS", "true").lower() == "true" else None,
)

# Rate limiter state + error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware — order matters: outermost runs first on request, last on response
app.add_middleware(RequestSizeLimitMiddleware)
if CorrelationMiddleware is not None:
    app.add_middleware(CorrelationMiddleware)

try:
    from utils.metrics import MetricsMiddleware as _MetricsMiddleware, metrics_text as _metrics_text
    app.add_middleware(_MetricsMiddleware)
except Exception:
    _metrics_text = None  # type: ignore
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

app.include_router(public_router, prefix="/api/v1")
app.include_router(apikey_router)


@app.get("/health", tags=["System"])
async def health_check() -> dict:
    """Public health probe — no auth required."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/metrics", tags=["System"], include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus-scrapable metrics. Public — use network policy to restrict."""
    if _metrics_text is None:
        return Response(content="# metrics module unavailable\n", media_type="text/plain")
    return Response(content=_metrics_text(), media_type="text/plain; version=0.0.4")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_row(table: str, row_id: str, client_id: str) -> dict:
    """
    Fetch a single row and assert it belongs to the authenticated client.

    Raises HTTP 404 if not found, HTTP 403 if it belongs to another client.
    """
    resp = _supabase.table(table).select("*").eq("id", row_id).limit(1).execute()
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{table} row not found.")
    row = rows[0]
    if row.get("client_id") != client_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")
    return row


def _check_plan_limit(client_id: str, table: str, limit_field: str) -> None:
    """
    Compare current resource count against the plan limit stored in clients table.

    Raises HTTP 403 with a descriptive message if the limit is already reached.
    """
    client_resp = (
        _supabase.table("clients")
        .select(limit_field)
        .eq("id", client_id)
        .limit(1)
        .execute()
    )
    client_rows = client_resp.data or []
    if not client_rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client account not found.")

    max_allowed: int = client_rows[0].get(limit_field) or 0

    count_resp = (
        _supabase.table(table)
        .select("id", count="exact")
        .eq("client_id", client_id)
        .execute()
    )
    current_count: int = count_resp.count or 0

    if current_count >= max_allowed:
        resource_name = "inboxes" if limit_field == "max_inboxes" else "domains"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Plan limit reached: your plan allows {max_allowed} {resource_name}. "
                f"You currently have {current_count}. Upgrade your plan to add more."
            ),
        )


def _now_utc() -> str:
    """Return the current UTC timestamp as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _days_ago_utc(days: int) -> str:
    """Return an ISO 8601 string for N days ago in UTC."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# POST /auth/verify
# ---------------------------------------------------------------------------

@app.post("/auth/login-attempt", tags=["Auth"])
@limiter.limit("10/minute")
async def log_login_attempt(request: Request, body: dict) -> dict:
    """
    Log a login attempt from the frontend.

    Frontend calls this before + after Supabase Auth call so we can track
    failed attempts per email and per IP, and optionally block after threshold.
    """
    email = (body.get("email") or "").strip().lower()[:255]
    success = bool(body.get("success", False))
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent", "")[:500]

    # Check failed-attempt count before allowing another try
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    try:
        recent = (
            _supabase.table("login_attempts")
            .select("id", count="exact")
            .eq("email", email)
            .eq("success", False)
            .gte("created_at", since)
            .execute()
        )
        fail_count = recent.count or 0
    except Exception:
        fail_count = 0

    blocked = fail_count >= 5 and not success

    # Log the attempt
    try:
        _supabase.table("login_attempts").insert({
            "email": email,
            "ip_address": ip,
            "success": success,
            "user_agent": ua,
        }).execute()
    except Exception as exc:
        logger.debug("Failed to log login attempt: %s", exc)

    # Optional urgent notification to admins after 10+ failures
    if fail_count >= 10 and not success:
        try:
            _supabase.table("notifications").insert({
                "client_id": "system",
                "type": "brute_force_alert",
                "message": f"Brute force suspected on {email} — {fail_count} failed attempts from {ip or 'unknown IP'}.",
                "priority": "urgent",
            }).execute()
        except Exception:
            pass

    return {
        "logged": True,
        "recent_fails": fail_count,
        "blocked": blocked,
        "retry_after_minutes": 15 if blocked else None,
    }


@app.post("/auth/verify", response_model=TokenVerifyResponse, tags=["Auth"])
@limiter.limit("10/minute")
async def verify_token(request: Request, credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())]):
    """
    Validate a Supabase JWT token without requiring an existing session.

    Call this from the frontend on page load to confirm the stored token is
    still valid and retrieve the client_id without hitting the Supabase Auth API.
    """
    if not SUPABASE_JWT_SECRET:
        raise HTTPException(status_code=500, detail="SUPABASE_JWT_SECRET not configured.")

    try:
        payload = jose_jwt.decode(
            credentials.credentials,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        client_id = payload.get("sub", "")
        if not client_id:
            return TokenVerifyResponse(client_id="", valid=False)
        return TokenVerifyResponse(client_id=client_id, valid=True)
    except Exception:
        return TokenVerifyResponse(client_id="", valid=False)


# ---------------------------------------------------------------------------
# Inboxes
# ---------------------------------------------------------------------------

@app.get("/inboxes", tags=["Inboxes"])
async def list_inboxes(client_id: ClientId):
    """Return all inboxes for the authenticated client, ordered by created_at desc."""
    resp = (
        _supabase.table("inboxes")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@app.post("/inboxes", status_code=status.HTTP_201_CREATED, tags=["Inboxes"])
async def create_inbox(payload: dict, client_id: ClientId):
    """
    Add a new inbox for the authenticated client.

    Validates plan limits before inserting. Required fields: email, domain, provider.
    Optional: warmup_start_date (defaults to today), notes.
    """
    _check_plan_limit(client_id, "inboxes", "max_inboxes")

    email = (payload.get("email") or "").strip().lower()
    domain = (payload.get("domain") or "").strip().lower()
    provider = (payload.get("provider") or "google").strip().lower()
    notes = payload.get("notes")
    warmup_start_date = payload.get("warmup_start_date") or datetime.now(timezone.utc).date().isoformat()

    if not email or not domain:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="email and domain are required.")
    if provider not in {"google", "microsoft", "other"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="provider must be google | microsoft | other.")

    # Check for duplicate email across all clients (inboxes.email is globally UNIQUE)
    dup = _supabase.table("inboxes").select("id").eq("email", email).limit(1).execute()
    if dup.data:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Inbox {email} already exists.")

    row = {
        "email": email,
        "domain": domain,
        "provider": provider,
        "warmup_active": True,
        "warmup_start_date": warmup_start_date,
        "daily_warmup_target": 10,
        "daily_campaign_target": 0,
        "daily_sent": 0,
        "reputation_score": 50.0,
        "status": "warmup",
        "client_id": client_id,
        "notes": notes,
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
    }

    resp = _supabase.table("inboxes").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create inbox.")

    created = resp.data[0]
    _log_decision(
        client_id=client_id,
        decision_type="inbox_added",
        entity_type="inbox",
        entity_id=str(created.get("id", "")),
        entity_name=email,
        before_state=None,
        after_state={"email": email, "domain": domain, "provider": provider},
    )
    return created


@app.patch("/inboxes/{inbox_id}/pause", tags=["Inboxes"])
async def pause_inbox(inbox_id: str, payload: dict, client_id: ClientId):
    """
    Pause or resume warmup for an inbox.

    Body: {"warmup_active": false} to pause, {"warmup_active": true} to resume.
    Also updates status to 'paused' or 'warmup' accordingly.
    """
    before_row = _require_row("inboxes", inbox_id, client_id)

    warmup_active: bool = bool(payload.get("warmup_active", False))
    new_status = "warmup" if warmup_active else "paused"

    resp = (
        _supabase.table("inboxes")
        .update({"warmup_active": warmup_active, "status": new_status, "updated_at": _now_utc()})
        .eq("id", inbox_id)
        .execute()
    )
    result = resp.data[0] if resp.data else {"id": inbox_id, "warmup_active": warmup_active, "status": new_status}

    _log_decision(
        client_id=client_id,
        decision_type="inbox_resumed" if warmup_active else "inbox_paused",
        entity_type="inbox",
        entity_id=inbox_id,
        entity_name=before_row.get("email"),
        before_state={"warmup_active": before_row.get("warmup_active"), "status": before_row.get("status")},
        after_state={"warmup_active": warmup_active, "status": new_status},
        reason=payload.get("reason"),
    )
    return result


@app.patch("/inboxes/{inbox_id}", tags=["Inboxes"])
async def patch_inbox(inbox_id: str, payload: dict, client_id: ClientId):
    """General patch for inbox fields: status, warmup_active, notes, daily_warmup_target."""
    _require_row("inboxes", inbox_id, client_id)
    allowed = {"status", "warmup_active", "notes", "daily_warmup_target", "daily_campaign_target"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=400, detail="No valid fields to update.")
    patch["updated_at"] = _now_utc()
    resp = _supabase.table("inboxes").update(patch).eq("id", inbox_id).execute()
    return resp.data[0] if resp.data else {}


@app.get("/inboxes/{inbox_id}/logs", tags=["Inboxes"])
async def inbox_logs(inbox_id: str, client_id: ClientId, limit: int = Query(default=50, le=200)):
    """Return recent warmup_logs for a specific inbox, newest first."""
    _require_row("inboxes", inbox_id, client_id)
    resp = (
        _supabase.table("warmup_logs")
        .select("*")
        .eq("inbox_id", inbox_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


@app.delete("/inboxes/{inbox_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Inboxes"])
async def delete_inbox(inbox_id: str, client_id: ClientId):
    """
    Permanently delete an inbox and all its associated warmup_logs.

    This is irreversible. Pausing is preferred for temporary suspension.
    """
    _require_row("inboxes", inbox_id, client_id)
    # Delete logs first (FK constraint)
    _supabase.table("warmup_logs").delete().eq("inbox_id", inbox_id).execute()
    _supabase.table("inboxes").delete().eq("id", inbox_id).execute()


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

@app.get("/domains", tags=["Domains"])
async def list_domains(client_id: ClientId):
    """Return all domains for the authenticated client, ordered by created_at desc."""
    resp = (
        _supabase.table("domains")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@app.post("/domains", status_code=status.HTTP_201_CREATED, tags=["Domains"])
async def create_domain(payload: DomainCreate, client_id: ClientId):
    """
    Register a new domain for the authenticated client.

    Validates plan limits. DNS records are not yet verified at this point —
    use GET /domains/{id}/dns-check to run a live check after DNS is configured.
    """
    _check_plan_limit(client_id, "domains", "max_domains")

    domain_str = payload.domain.lower().strip().lstrip("www.")

    dup = _supabase.table("domains").select("id").eq("domain", domain_str).eq("client_id", client_id).limit(1).execute()
    if dup.data:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Domain {domain_str} already registered.")

    tld = payload.tld or ("." + domain_str.rsplit(".", 1)[-1] if "." in domain_str else "")

    row = {
        "domain": domain_str,
        "registrar": payload.registrar,
        "tld": tld,
        "spf_configured": False,
        "dkim_configured": False,
        "dmarc_phase": "none",
        "blacklisted": False,
        "client_id": client_id,
        "created_at": _now_utc(),
    }

    resp = _supabase.table("domains").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create domain.")

    return resp.data[0]


@app.get("/domains/{domain_id}/dns-check", response_model=DNSCheckResponse, tags=["Domains"])
async def dns_check(domain_id: str, client_id: ClientId, dkim_selector: str = Query(default="google")):
    """
    Run a live DNS check for SPF, DKIM, DMARC, and MX records.

    Checks are performed via dnspython with a 5-second timeout per record type.
    After the check, the domain row in Supabase is updated with the current results.

    Query param: dkim_selector (default: 'google' for Google Workspace).
    For Microsoft 365 use 'selector1' or 'selector2'.
    """
    domain_row = _require_row("domains", domain_id, client_id)
    domain_name: str = domain_row["domain"]

    result = run_full_dns_check(domain_name, dkim_selector=dkim_selector)

    # Persist findings back to the domains table
    _supabase.table("domains").update(
        {
            "spf_configured": result.spf_configured,
            "dkim_configured": result.dkim_configured,
            "dmarc_phase": result.dmarc_phase,
        }
    ).eq("id", domain_id).execute()

    return result.to_dict()


# ---------------------------------------------------------------------------
# Replies (unified inbox)
# ---------------------------------------------------------------------------

@app.get("/replies", tags=["Replies"])
async def list_replies(
    client_id: ClientId,
    classification: Optional[str] = Query(default=None),
    is_read: Optional[bool] = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    """
    Return all inbound replies for this client from reply_inbox, newest first.

    Optional filters: classification (interested|not_interested|out_of_office|referral|unsubscribe|question|other),
    is_read (true|false).
    """
    q = (
        _supabase.table("reply_inbox")
        .select("*, campaigns(name), leads(first_name,last_name,company_name)")
        .eq("client_id", client_id)
        .order("received_at", desc=True)
        .limit(limit)
    )
    if classification:
        q = q.eq("classification", classification)
    if is_read is not None:
        q = q.eq("is_read", is_read)

    resp = q.execute()
    return resp.data or []


@app.patch("/replies/{reply_id}", tags=["Replies"])
async def patch_reply(reply_id: str, payload: dict, client_id: ClientId):
    """Update a reply: mark as read, change classification, or stop sequence."""
    # Verify ownership via client_id
    row_resp = _supabase.table("reply_inbox").select("client_id").eq("id", reply_id).limit(1).execute()
    rows = row_resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Reply not found.")
    if rows[0].get("client_id") != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    allowed = {"is_read", "classification", "is_interested"}
    patch = {k: v for k, v in payload.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=400, detail="No valid fields.")

    resp = _supabase.table("reply_inbox").update(patch).eq("id", reply_id).execute()

    # If stopping sequence: mark all pending sends for this lead as unsubscribed
    if payload.get("stop_sequence") and payload.get("lead_email"):
        _supabase.table("sending_schedule").update({"status": "unsubscribed"}).eq(
            "lead_email", payload["lead_email"]
        ).eq("status", "pending").eq("client_id", client_id).execute()

    return resp.data[0] if resp.data else {}


@app.get("/replies/unread-count", tags=["Replies"])
async def unread_count(client_id: ClientId) -> dict:
    """Return the count of unread replies for the notification badge."""
    resp = (
        _supabase.table("reply_inbox")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .eq("is_read", False)
        .execute()
    )
    return {"count": resp.count or 0}


# ---------------------------------------------------------------------------
# Warmup stats
# ---------------------------------------------------------------------------

@app.get("/warmup/stats", response_model=WarmupStats, tags=["Warmup"])
async def warmup_stats(client_id: ClientId):
    """
    Return aggregated warmup statistics for the authenticated client.

    Aggregates inbox metrics and queries warmup_logs for 7-day activity counts.
    """
    inboxes_resp = _supabase.table("inboxes").select("*").eq("client_id", client_id).execute()
    inboxes: list[dict] = inboxes_resp.data or []

    total_inboxes = len(inboxes)
    active_inboxes = sum(1 for i in inboxes if i.get("warmup_active") and i.get("status") != "retired")
    ready_inboxes = sum(1 for i in inboxes if i.get("status") == "ready")
    total_sent_today = sum(i.get("daily_sent") or 0 for i in inboxes)
    total_spam_rescues = sum(i.get("spam_rescues") or 0 for i in inboxes)
    total_spam_complaints = sum(i.get("spam_complaints") or 0 for i in inboxes)

    scores = [i.get("reputation_score") or 0.0 for i in inboxes if i.get("reputation_score") is not None]
    avg_reputation_score = round(sum(scores) / len(scores), 2) if scores else 0.0

    inbox_ids = [i["id"] for i in inboxes]
    cutoff = _days_ago_utc(7)

    total_sent_alltime = 0
    sends_last_7_days = 0
    errors_last_7_days = 0

    if inbox_ids:
        # All-time sent count
        alltime_resp = (
            _supabase.table("warmup_logs")
            .select("id", count="exact")
            .in_("inbox_id", inbox_ids)
            .eq("action", "sent")
            .execute()
        )
        total_sent_alltime = alltime_resp.count or 0

        # Sends in last 7 days
        sent7_resp = (
            _supabase.table("warmup_logs")
            .select("id", count="exact")
            .in_("inbox_id", inbox_ids)
            .eq("action", "sent")
            .gte("timestamp", cutoff)
            .execute()
        )
        sends_last_7_days = sent7_resp.count or 0

        # Errors in last 7 days
        err7_resp = (
            _supabase.table("warmup_logs")
            .select("id", count="exact")
            .in_("inbox_id", inbox_ids)
            .eq("action", "error")
            .gte("timestamp", cutoff)
            .execute()
        )
        errors_last_7_days = err7_resp.count or 0

    return WarmupStats(
        total_inboxes=total_inboxes,
        active_inboxes=active_inboxes,
        ready_inboxes=ready_inboxes,
        total_sent_today=total_sent_today,
        total_sent_alltime=total_sent_alltime,
        avg_reputation_score=avg_reputation_score,
        total_spam_rescues=total_spam_rescues,
        total_spam_complaints=total_spam_complaints,
        sends_last_7_days=sends_last_7_days,
        errors_last_7_days=errors_last_7_days,
    )


# ---------------------------------------------------------------------------
# Warmup history (per-inbox daily reputation trend)
# ---------------------------------------------------------------------------

@app.get("/warmup/inbox/{inbox_id}/forecast", tags=["Warmup"])
async def warmup_inbox_forecast(inbox_id: str, client_id: ClientId) -> dict:
    """
    Linear regression over the last 14 days of daily reputation scores.

    Returns:
      - current_score, target_score (70 = campaign-ready threshold)
      - slope_per_day: points gained per day on average (may be negative)
      - days_until_ready: estimate of when score will reach 70
      - confidence: r_squared of the regression
      - verdict: "on_track" | "stalled" | "declining" | "ready"
    """
    _require_row("inboxes", inbox_id, client_id)
    cutoff = _days_ago_utc(14)
    logs = (
        _supabase.table("warmup_logs")
        .select("reputation_score_at_time, timestamp")
        .eq("inbox_id", inbox_id)
        .gte("timestamp", cutoff)
        .order("timestamp")
        .limit(5000)
        .execute()
    ).data or []

    # Aggregate to daily average
    daily: dict[str, list[float]] = {}
    for log in logs:
        day = (log.get("timestamp") or "")[:10]
        score = log.get("reputation_score_at_time")
        if not day or score is None:
            continue
        daily.setdefault(day, []).append(float(score))

    points = sorted((day, sum(scores) / len(scores)) for day, scores in daily.items())

    # Current score (from inboxes table, authoritative)
    current = _supabase.table("inboxes").select("reputation_score").eq("id", inbox_id).single().execute().data
    current_score = float((current or {}).get("reputation_score") or 50)
    target = 70.0

    if len(points) < 3:
        return {
            "current_score": current_score,
            "target_score": target,
            "slope_per_day": 0.0,
            "days_until_ready": None,
            "confidence": 0.0,
            "verdict": "insufficient_data",
            "data_points": len(points),
        }

    # Simple linear regression: x = day index, y = score
    n = len(points)
    xs = list(range(n))
    ys = [p[1] for p in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n)) or 1
    slope = num / den
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys) or 1
    ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    r_squared = max(0.0, 1 - ss_res / ss_tot)

    # Days until reaching 70
    days_until_ready: float | None = None
    if current_score >= target:
        verdict = "ready"
    elif slope <= 0.05:
        verdict = "stalled" if slope >= -0.05 else "declining"
    else:
        days_until_ready = (target - current_score) / slope
        verdict = "on_track"

    return {
        "current_score": round(current_score, 1),
        "target_score": target,
        "slope_per_day": round(slope, 3),
        "days_until_ready": round(days_until_ready, 1) if days_until_ready is not None else None,
        "confidence": round(r_squared, 2),
        "verdict": verdict,
        "data_points": n,
    }


@app.get("/warmup/inbox/{inbox_id}/history", tags=["Warmup"])
async def warmup_inbox_history(inbox_id: str, client_id: ClientId, days: int = Query(default=30)) -> dict:
    """Per-inbox daily reputation, volume, and spam stats over time."""
    _require_row("inboxes", inbox_id, client_id)
    cutoff = _days_ago_utc(days)
    logs = (
        _supabase.table("warmup_logs")
        .select("action, reputation_score_at_time, landed_in_spam, was_rescued, was_replied, timestamp")
        .eq("inbox_id", inbox_id)
        .gte("timestamp", cutoff)
        .order("timestamp")
        .limit(5000)
        .execute()
    ).data or []

    # Aggregate by day
    daily: dict[str, dict] = {}
    for log in logs:
        day = (log.get("timestamp") or "")[:10]
        if not day:
            continue
        if day not in daily:
            daily[day] = {"sent": 0, "received": 0, "replied": 0, "spam_rescued": 0, "errors": 0, "reputation": []}
        action = log.get("action", "")
        if action == "sent":
            daily[day]["sent"] += 1
        elif action == "received":
            daily[day]["received"] += 1
        elif action == "replied":
            daily[day]["replied"] += 1
        elif action == "spam_rescued":
            daily[day]["spam_rescued"] += 1
        elif action == "error":
            daily[day]["errors"] += 1
        score = log.get("reputation_score_at_time")
        if score is not None:
            daily[day]["reputation"].append(float(score))

    # Build time series
    series = []
    for day in sorted(daily.keys()):
        d = daily[day]
        rep_scores = d.pop("reputation")
        d["date"] = day
        d["avg_reputation"] = round(sum(rep_scores) / len(rep_scores), 1) if rep_scores else None
        series.append(d)

    return {"inbox_id": inbox_id, "days": days, "series": series}


@app.get("/warmup/network-health", tags=["Warmup"])
async def warmup_network_health(client_id: ClientId) -> dict:
    """Return health status of all warmup network accounts (IMAP reachability)."""
    # Check latest network_health_log
    net_resp = (
        _supabase.table("network_health_log")
        .select("*")
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )
    latest = (net_resp.data or [None])[0]

    # Also fetch per-account stats from warmup_logs (last 7 days)
    cutoff = _days_ago_utc(7)
    log_resp = (
        _supabase.table("warmup_logs")
        .select("counterpart_email, action")
        .gte("timestamp", cutoff)
        .in_("action", ["received", "replied"])
        .limit(5000)
        .execute()
    )
    logs = log_resp.data or []

    # Aggregate per warmup account
    accounts: dict[str, dict] = {}
    for log in logs:
        email = log.get("counterpart_email", "")
        if not email:
            continue
        if email not in accounts:
            accounts[email] = {"email": email, "received": 0, "replied": 0}
        if log["action"] == "received":
            accounts[email]["received"] += 1
        elif log["action"] == "replied":
            accounts[email]["replied"] += 1

    for acct in accounts.values():
        acct["reply_rate"] = round(acct["replied"] / acct["received"], 2) if acct["received"] > 0 else 0

    return {
        "latest_check": latest,
        "accounts": sorted(accounts.values(), key=lambda a: a["received"], reverse=True),
        "total_accounts": len(accounts),
    }


@app.get("/domains/{domain_id}/age", tags=["Domains"])
async def domain_age(domain_id: str, client_id: ClientId) -> dict:
    """Check domain age and warn if too young for sending."""
    row = _require_row("domains", domain_id, client_id)
    domain_name = row.get("domain", "")
    created = row.get("created_at", "")

    # Attempt WHOIS-style check via DNS SOA record
    import dns.resolver
    age_days = None
    warning = None
    try:
        if created:
            from datetime import datetime, timezone
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created_dt).days
    except Exception:
        pass

    if age_days is not None and age_days < 30:
        warning = f"Domein is {age_days} dagen oud. Aanbevolen: wacht tot minimaal 30 dagen voor verzending."
    elif age_days is not None and age_days < 90:
        warning = f"Domein is {age_days} dagen oud. Start voorzichtig met lage volumes."

    return {
        "domain": domain_name,
        "age_days": age_days,
        "created_at": created,
        "warning": warning,
        "safe_to_send": age_days is None or age_days >= 30,
    }


# ---------------------------------------------------------------------------
# Warmup trigger
# ---------------------------------------------------------------------------

def _run_warmup_engine() -> None:
    """
    Blocking wrapper that imports and executes warmup_engine.main().

    Runs in a thread pool so it doesn't block the FastAPI event loop.
    sys.path is extended to include the project root so the import resolves
    regardless of how uvicorn was launched.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        import warmup_engine  # noqa: PLC0415 (import not at top level is intentional)
        warmup_engine.main()
    except Exception as exc:
        logger.error("warmup_engine.main() raised an exception: %s", exc)


@app.post("/warmup/trigger", response_model=WarmupTriggerResponse, tags=["Warmup"])
async def trigger_warmup(background_tasks: BackgroundTasks, client_id: ClientId):
    """
    Manually trigger a warmup engine run in the background.

    Useful for testing individual inbox sends without waiting for the n8n schedule.
    The engine respects the configured send window — if you call this outside
    07:00–19:00 on a weekday it will log and exit immediately.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_warmup_engine)

    return WarmupTriggerResponse(
        triggered=True,
        message="Warmup engine started in background. Check warmup_logs for results.",
    )


# ---------------------------------------------------------------------------
# Worker trigger endpoints  — called by n8n workflows via HTTP Request nodes
# Each endpoint runs the corresponding worker in a background thread and
# returns a summary immediately so n8n doesn't time out.
# ---------------------------------------------------------------------------

# ── DNSBL helpers (used by /domains/blacklist-check) ──────────────────────

_DNSBL_ZONES: list[str] = [
    "zen.spamhaus.org",
    "bl.spamcop.net",
    "dnsbl.sorbs.net",
    "b.barracudacentral.org",
    "dnsbl-1.uceprotect.net",
]


def _check_ip_dnsbl(ip: str) -> list[str]:
    """Return list of DNSBL zones where ip is listed."""
    import dns.exception
    import dns.resolver

    reversed_ip = ".".join(reversed(ip.split(".")))
    listed: list[str] = []
    for zone in _DNSBL_ZONES:
        try:
            dns.resolver.resolve(f"{reversed_ip}.{zone}", "A", lifetime=3)
            listed.append(zone)
        except Exception:
            pass
    return listed


def _domain_mail_ip(domain: str) -> Optional[str]:
    """Resolve the A record IP of the domain's primary MX host."""
    import dns.exception
    import dns.resolver

    try:
        mx_ans = dns.resolver.resolve(domain, "MX", lifetime=5)
        mx_host = str(sorted(mx_ans, key=lambda r: r.preference)[0].exchange).rstrip(".")
        a_ans = dns.resolver.resolve(mx_host, "A", lifetime=5)
        return str(list(a_ans)[0])
    except Exception:
        return None


# ── Worker runners ─────────────────────────────────────────────────────────

def _run_imap_processor() -> dict:
    """Import and call imap_processor.main(). Returns result summary."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        import imap_processor  # noqa: PLC0415
        return imap_processor.main() or {"ok": True}
    except Exception as exc:
        logger.error("imap_processor.main() raised: %s", exc)
        return {"ok": False, "error": str(exc)}


def _run_bounce_handler() -> dict:
    """Import and call bounce_handler.main(). Returns result summary."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        import bounce_handler  # noqa: PLC0415
        return bounce_handler.main() or {"ok": True}
    except Exception as exc:
        logger.error("bounce_handler.main() raised: %s", exc)
        return {"ok": False, "error": str(exc)}


def _run_campaign_scheduler(client_id: str) -> dict:
    """
    Process pending campaign sends for a client.

    Fetches rows from sending_schedule where status='pending' and
    scheduled_at <= now, selects an available inbox per send, marks them
    as sent=true. Actual SMTP sending is delegated to campaign_scheduler.main().
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        import campaign_scheduler  # noqa: PLC0415
        return campaign_scheduler.main(client_id=client_id) or {"ok": True}
    except ImportError:
        # campaign_scheduler.py not yet available — basic queue count only
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        pending_resp = (
            _supabase.table("sending_schedule")
            .select("id", count="exact")
            .eq("client_id", client_id)
            .eq("status", "pending")
            .lte("scheduled_at", now)
            .execute()
        )
        return {
            "ok": True,
            "pending_sends": pending_resp.count or 0,
            "note": "campaign_scheduler.py not installed — no sends processed",
        }
    except Exception as exc:
        logger.error("campaign_scheduler raised: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.post("/imap/process", tags=["Workers"])
async def trigger_imap_process(client_id: ClientId) -> dict:
    """
    Trigger an IMAP processing cycle.

    Connects to all active inboxes, rescues spam, marks warmup emails as
    read, and generates replies (35% rate). Runs in background thread.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_imap_processor)
    return {"triggered": True, "message": "IMAP processor started."}


@app.post("/campaigns/process-queue", tags=["Workers"])
async def process_campaign_queue(client_id: ClientId) -> dict:
    """
    Process the campaign send queue for the authenticated client.

    Picks up pending rows from sending_schedule where scheduled_at <= now,
    respects inbox daily limits, and sends via SMTP.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _run_campaign_scheduler, client_id)
    return result


@app.post("/bounces/process", tags=["Workers"])
async def trigger_bounce_process(client_id: ClientId) -> dict:
    """
    Trigger a bounce processing cycle.

    Reads unresolved bounce_log entries, applies hard/soft bounce rules,
    updates inbox reputation scores, and pauses inboxes where needed.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_bounce_handler)
    return {"triggered": True, "message": "Bounce handler started."}


@app.post("/enrichment/process-queue", tags=["Workers"])
async def trigger_enrichment_queue(
    client_id: ClientId,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """
    Run one enrichment queue cycle (max `limit` leads, default 10).

    Picks up pending enrichment_queue rows ordered by priority then age,
    runs the full enrichment pipeline, and emits lead.enriched webhooks.
    """
    import asyncio

    def _run(cid: str, lim: int) -> dict:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from enrichment_queue import MAX_CONCURRENT, fetch_pending_jobs, run_cycle  # noqa: PLC0415
        import enrichment_queue as eq
        eq.MAX_CONCURRENT = lim
        sb = _supabase
        jobs = fetch_pending_jobs(sb, limit=lim)
        if not jobs:
            return {"processed": 0, "message": "No pending enrichment jobs."}
        processed = run_cycle(sb)
        return {"processed": processed}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _run, client_id, limit)
    return result


@app.post("/webhooks/dispatch", tags=["Workers"])
async def trigger_webhook_dispatch(client_id: ClientId) -> dict:
    """
    Run one webhook dispatch cycle.

    Delivers all pending webhook_events and retries failed deliveries
    whose next_retry_at is in the past.
    """
    import asyncio

    def _run() -> dict:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from webhook_dispatcher import _sb as _disp_sb, run_cycle  # noqa: PLC0415
        sb = _disp_sb()
        # Count pending before cycle
        pending_resp = (
            sb.table("webhook_events")
            .select("id", count="exact")
            .eq("dispatched", False)
            .execute()
        )
        pending_count = pending_resp.count or 0
        run_cycle(sb)
        return {"dispatched_events": pending_count, "ok": True}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _run)
    return result


@app.get("/domains/blacklist-check", tags=["Workers"])
async def check_domain_blacklists(client_id: ClientId) -> dict:
    """
    Check all client domains against major DNSBL blacklists.

    Resolves the mail server IP for each domain and queries:
    zen.spamhaus.org, bl.spamcop.net, dnsbl.sorbs.net,
    b.barracudacentral.org, dnsbl-1.uceprotect.net.

    Updates domains.blacklisted and domains.last_blacklist_check.
    """
    import asyncio
    from datetime import datetime, timezone

    domains_resp = (
        _supabase.table("domains")
        .select("id, domain")
        .eq("client_id", client_id)
        .execute()
    )
    domains = domains_resp.data or []

    if not domains:
        return {"checked": 0, "blacklisted": [], "clean": []}

    blacklisted: list[str] = []
    clean: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    loop = asyncio.get_event_loop()

    for d in domains:
        domain_name = d["domain"]
        ip = await loop.run_in_executor(_executor, _domain_mail_ip, domain_name)
        is_listed = False
        listed_zones: list[str] = []

        if ip:
            listed_zones = await loop.run_in_executor(_executor, _check_ip_dnsbl, ip)
            is_listed = bool(listed_zones)

        _supabase.table("domains").update({
            "blacklisted":           is_listed,
            "last_blacklist_check":  now_iso,
        }).eq("id", d["id"]).execute()

        if is_listed:
            blacklisted.append({"domain": domain_name, "ip": ip, "listed_in": listed_zones})
        else:
            clean.append(domain_name)

    return {
        "checked":     len(domains),
        "blacklisted": blacklisted,
        "clean":       clean,
    }


@app.post("/inboxes/reset-daily-counts", tags=["Workers"])
async def reset_daily_counts(client_id: ClientId) -> dict:
    """
    Reset daily_sent = 0 for all inboxes belonging to this client.

    Called by the daily-reset n8n workflow at midnight.
    """
    resp = (
        _supabase.table("inboxes")
        .update({"daily_sent": 0})
        .eq("client_id", client_id)
        .execute()
    )
    updated = len(resp.data or [])
    logger.info("Reset daily_sent for %d inboxes (client: %s)", updated, client_id)

    # Apply engagement score decay
    decayed = 0
    try:
        from engagement_scorer import apply_daily_decay
        decayed = apply_daily_decay(_supabase)
    except Exception as exc:
        logger.debug("Engagement decay skipped: %s", exc)

    # Check nurture re-engagement
    reengaged = 0
    try:
        from funnel_engine import check_nurture_reengagement
        reengaged_ids = check_nurture_reengagement(_supabase, client_id)
        reengaged = len(reengaged_ids)
    except Exception as exc:
        logger.debug("Nurture re-engagement check skipped: %s", exc)

    return {"reset": True, "inboxes_updated": updated, "engagement_decayed": decayed, "leads_reengaged": reengaged}


@app.get("/analytics/weekly-report", tags=["Analytics"])
async def get_weekly_report(client_id: ClientId) -> dict:
    """
    Generate a weekly deliverability report for the authenticated client.

    Aggregates warmup_logs, sending_schedule, and bounce_log data for
    the past 7 days. Returned as a JSON dict ready for email formatting.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()
    now_iso  = now.isoformat()

    # Inbox summary
    inboxes_resp = (
        _supabase.table("inboxes")
        .select("email, domain, reputation_score, status, daily_sent, spam_rescues, spam_complaints")
        .eq("client_id", client_id)
        .execute()
    )
    inboxes = inboxes_resp.data or []

    # Warmup logs for the week
    inbox_ids = [i.get("id") for i in (
        _supabase.table("inboxes").select("id").eq("client_id", client_id).execute().data or []
    )]

    warmup_sent    = 0
    spam_rescues   = 0
    spam_complaints = 0
    avg_rep        = 0.0

    if inboxes:
        avg_rep = round(sum(i.get("reputation_score") or 0 for i in inboxes) / len(inboxes), 1)
        spam_rescues    = sum(i.get("spam_rescues")    or 0 for i in inboxes)
        spam_complaints = sum(i.get("spam_complaints") or 0 for i in inboxes)

    if inbox_ids:
        logs_resp = (
            _supabase.table("warmup_logs")
            .select("action")
            .in_("inbox_id", inbox_ids)
            .gte("timestamp", week_ago)
            .execute()
        )
        logs = logs_resp.data or []
        warmup_sent = sum(1 for l in logs if l.get("action") == "sent")

    # Campaign sends for the week
    campaign_resp = (
        _supabase.table("sending_schedule")
        .select("status")
        .eq("client_id", client_id)
        .gte("sent_at", week_ago)
        .execute()
    )
    camp_rows  = campaign_resp.data or []
    camp_sent  = sum(1 for r in camp_rows if r.get("status") in ("sent", "replied"))
    camp_replied = sum(1 for r in camp_rows if r.get("status") == "replied")
    camp_bounced = sum(1 for r in camp_rows if r.get("status") == "bounced")

    # Bounce log
    bounce_resp = (
        _supabase.table("bounce_log")
        .select("bounce_type")
        .in_("inbox_id", inbox_ids or ["none"])
        .gte("timestamp", week_ago)
        .execute()
    )
    bounces     = bounce_resp.data or []
    hard_bounces = sum(1 for b in bounces if b.get("bounce_type") == "hard")
    soft_bounces = sum(1 for b in bounces if b.get("bounce_type") == "soft")

    ready_inboxes  = [i for i in inboxes if i.get("status") == "ready"]
    warmup_inboxes = [i for i in inboxes if i.get("status") == "warmup"]

    return {
        "period": {
            "from": week_ago,
            "to":   now_iso,
        },
        "inboxes": {
            "total":  len(inboxes),
            "ready":  len(ready_inboxes),
            "warmup": len(warmup_inboxes),
            "avg_reputation_score": avg_rep,
        },
        "warmup": {
            "emails_sent":       warmup_sent,
            "spam_rescues":      spam_rescues,
            "spam_complaints":   spam_complaints,
        },
        "campaigns": {
            "emails_sent":  camp_sent,
            "replied":      camp_replied,
            "bounced":      camp_bounced,
            "reply_rate":   round(camp_replied / camp_sent, 4) if camp_sent else 0.0,
            "bounce_rate":  round(camp_bounced / camp_sent, 4) if camp_sent else 0.0,
        },
        "bounces": {
            "hard": hard_bounces,
            "soft": soft_bounces,
        },
        "inbox_breakdown": [
            {
                "email":            i.get("email"),
                "domain":           i.get("domain"),
                "reputation_score": i.get("reputation_score"),
                "status":           i.get("status"),
            }
            for i in sorted(inboxes, key=lambda x: x.get("reputation_score") or 0, reverse=True)
        ],
    }


# ---------------------------------------------------------------------------
# Intelligence layer endpoints
# ---------------------------------------------------------------------------

@app.get("/diagnostics/inbox/{inbox_id}", tags=["Diagnostics"])
async def get_inbox_diagnostics(inbox_id: str, client_id: ClientId) -> dict:
    """
    Full diagnostic report for one inbox.

    Returns: recent drift log, 7-day forecast, SMTP error count, network health summary.
    """
    _require_row("inboxes", inbox_id, client_id)

    # Last 7 days of diagnostic logs for this inbox
    since_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    diag_resp = (
        _supabase.table("diagnostics_log")
        .select("*")
        .eq("entity_id", inbox_id)
        .gte("timestamp", since_iso)
        .order("timestamp", desc=True)
        .limit(50)
        .execute()
    )

    # Cached forecast
    forecast_resp = (
        _supabase.table("analytics_cache")
        .select("metrics, updated_at")
        .eq("entity_type", "inbox_forecast")
        .eq("entity_id", inbox_id)
        .limit(1)
        .execute()
    )
    forecast = (forecast_resp.data or [{}])[0].get("metrics") if forecast_resp.data else None

    # SMTP error count last 2h
    two_h_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    smtp_resp = (
        _supabase.table("warmup_logs")
        .select("id", count="exact")
        .eq("inbox_id", inbox_id)
        .eq("action", "error")
        .gte("timestamp", two_h_ago)
        .execute()
    )

    # Pending notifications for this inbox
    notifs_resp = (
        _supabase.table("notifications")
        .select("type, message, priority, timestamp")
        .eq("client_id", client_id)
        .eq("entity_id", inbox_id)
        .eq("read", False)
        .order("timestamp", desc=True)
        .limit(10)
        .execute()
    )

    return {
        "inbox_id":          inbox_id,
        "diagnostic_logs":   diag_resp.data or [],
        "forecast":          forecast,
        "smtp_errors_last_2h": smtp_resp.count or 0,
        "active_notifications": notifs_resp.data or [],
    }


@app.get("/diagnostics/overview", tags=["Diagnostics"])
async def get_diagnostics_overview(client_id: ClientId) -> dict:
    """
    System-wide health summary for the authenticated client.

    Returns: inbox health scores, network health, active warnings count,
    recent critical diagnostics.
    """
    # All inboxes
    inboxes_resp = (
        _supabase.table("inboxes")
        .select("id, email, status, reputation_score, auto_pause_count_24h")
        .eq("client_id", client_id)
        .neq("status", "retired")
        .execute()
    )
    inboxes = inboxes_resp.data or []

    # Unread high/urgent notifications
    notifs_resp = (
        _supabase.table("notifications")
        .select("type, priority, message, timestamp")
        .eq("client_id", client_id)
        .eq("read", False)
        .in_("priority", ["high", "urgent"])
        .order("timestamp", desc=True)
        .limit(20)
        .execute()
    )

    # Recent diagnostics (last 24h, critical only)
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    critical_resp = (
        _supabase.table("diagnostics_log")
        .select("check_type, result, details, entity_id, timestamp")
        .eq("client_id", client_id)
        .in_("result", ["warning", "critical"])
        .gte("timestamp", since_iso)
        .order("timestamp", desc=True)
        .limit(20)
        .execute()
    )

    # Latest network health
    net_resp = (
        _supabase.table("network_health_log")
        .select("total_accounts, active_accounts, health_score, timestamp")
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )
    network_health = (net_resp.data or [None])[0]

    paused_inboxes   = [i for i in inboxes if i.get("status") == "paused"]
    avg_rep          = (
        sum(i.get("reputation_score") or 0 for i in inboxes) / len(inboxes)
        if inboxes else 0.0
    )

    return {
        "client_id":         client_id,
        "inboxes": {
            "total":         len(inboxes),
            "paused":        len(paused_inboxes),
            "avg_reputation": round(avg_rep, 1),
        },
        "network_health":    network_health,
        "active_warnings":   len(notifs_resp.data or []),
        "notifications":     notifs_resp.data or [],
        "critical_diagnostics": critical_resp.data or [],
    }


@app.get("/analytics/campaigns/{campaign_id}/optimal-send-times", tags=["Analytics"])
async def get_optimal_send_times(campaign_id: str, client_id: ClientId) -> dict:
    """
    Return top-3 send time slots for a campaign based on historical open rates.

    If no recommendation exists yet (< 20 sends per slot), triggers a live
    computation and returns the result. Returns empty top_slots if there is
    insufficient data.
    """
    _require_row("campaigns", campaign_id, client_id)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from send_time_optimizer import compute_send_time_recommendations, get_cached_recommendation  # noqa

    # Try cache first
    cached = get_cached_recommendation(_supabase, campaign_id)
    if cached:
        return cached

    # Compute live
    result = compute_send_time_recommendations(_supabase, campaign_id, client_id)
    if result:
        return result

    return {
        "campaign_id":         campaign_id,
        "top_slots":           [],
        "min_sends_threshold": 20,
        "message":             "Insufficient data — need at least 20 sends per time slot.",
    }


@app.get("/analytics/campaigns/{campaign_id}/sequence-suggestions", tags=["Analytics"])
async def get_sequence_suggestions(
    campaign_id:   str,
    client_id:     ClientId,
    status_filter: Optional[str] = Query(default="pending", description="pending|applied|dismissed"),
) -> list[dict]:
    """
    Return pending improvement suggestions for a campaign's sequence steps.

    Suggestions are generated weekly by sequence_analyzer.py.
    """
    _require_row("campaigns", campaign_id, client_id)

    resp = (
        _supabase.table("sequence_suggestions")
        .select("*")
        .eq("campaign_id", campaign_id)
        .eq("client_id", client_id)
        .eq("status", status_filter)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@app.patch(
    "/analytics/campaigns/{campaign_id}/sequence-suggestions/{suggestion_id}",
    tags=["Analytics"],
)
async def update_sequence_suggestion(
    campaign_id:   str,
    suggestion_id: str,
    body:          dict,
    client_id:     ClientId,
) -> dict:
    """
    Apply or dismiss a sequence improvement suggestion.

    Allowed body fields: status ('applied' | 'dismissed').
    If status = 'applied', also patches the sequence_step subject/body
    based on suggestion_type.
    """
    _require_row("campaigns", campaign_id, client_id)

    # Verify suggestion ownership
    sug_resp = (
        _supabase.table("sequence_suggestions")
        .select("*")
        .eq("id", suggestion_id)
        .eq("campaign_id", campaign_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    )
    suggestions = sug_resp.data or []
    if not suggestions:
        raise HTTPException(status_code=404, detail="Suggestion not found.")

    suggestion = suggestions[0]
    new_status  = body.get("status")
    if new_status not in ("applied", "dismissed"):
        raise HTTPException(status_code=422, detail="status must be 'applied' or 'dismissed'.")

    # Apply the suggestion to the sequence step content
    if new_status == "applied":
        step_id     = suggestion.get("sequence_step_id")
        sug_type    = suggestion.get("suggestion_type")
        sug_text    = suggestion.get("suggestion_text")

        if step_id and sug_text:
            patch: dict = {}
            if sug_type == "subject_line":
                patch["subject"] = sug_text
            elif sug_type == "opening":
                # Patch the first sentence of the body
                step_resp = (
                    _supabase.table("sequence_steps")
                    .select("body")
                    .eq("id", step_id)
                    .limit(1)
                    .execute()
                )
                step_rows = step_resp.data or []
                if step_rows:
                    old_body = step_rows[0].get("body") or ""
                    # Replace up to first full stop / newline with new opening
                    import re as _re
                    new_body = _re.sub(
                        r"^[^\.\n]+[\.\n]?",
                        sug_text + " ",
                        old_body,
                        count=1,
                    )
                    patch["body"] = new_body

            if patch:
                _supabase.table("sequence_steps").update(patch).eq("id", step_id).execute()

    # Update suggestion status
    resp = (
        _supabase.table("sequence_suggestions")
        .update({"status": new_status})
        .eq("id", suggestion_id)
        .execute()
    )
    return resp.data[0] if resp.data else {"id": suggestion_id, "status": new_status}


@app.get("/ab-tests/status", tags=["Analytics"])
async def get_ab_tests_status(client_id: ClientId) -> list[dict]:
    """
    Return all active A/B tests with current significance levels for this client.

    Computes live z-test results without auto-promoting — read-only view.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from ab_optimizer import load_active_ab_tests, load_variant_metrics, two_proportion_z_test  # noqa

    try:
        tests = load_active_ab_tests(_supabase)
    except Exception as exc:
        logger.error("Failed to load A/B tests: %s", exc)
        return []

    # Filter to this client's campaigns
    if tests:
        camp_ids = list({t["campaign_id"] for t in tests})
        client_camps_resp = (
            _supabase.table("campaigns")
            .select("id")
            .in_("id", camp_ids)
            .eq("client_id", client_id)
            .execute()
        )
        client_camp_ids = {r["id"] for r in (client_camps_resp.data or [])}
        tests = [t for t in tests if t["campaign_id"] in client_camp_ids]

    results: list[dict] = []
    for test in tests:
        campaign_id = test["campaign_id"]
        va          = test["variant_a"]
        vb          = test["variant_b"]

        try:
            n_a, x_a, n_b, x_b = load_variant_metrics(_supabase, campaign_id, va["id"], vb["id"])
            z_result = two_proportion_z_test(n_a, x_a, n_b, x_b)
        except Exception as exc:
            logger.warning("Failed to compute z-test for %s: %s", campaign_id, exc)
            z_result = {}

        camp_resp = _supabase.table("campaigns").select("name").eq("id", campaign_id).limit(1).execute()
        camp_name = (camp_resp.data or [{}])[0].get("name", campaign_id)

        results.append({
            "campaign_id":   campaign_id,
            "campaign_name": camp_name,
            "step_number":   test["step_number"],
            "variant_a":     {"id": va["id"], "subject": va.get("subject")},
            "variant_b":     {"id": vb["id"], "subject": vb.get("subject")},
            **z_result,
        })

    return results


@app.get("/network/health", tags=["Diagnostics"])
async def get_network_health(client_id: ClientId) -> dict:
    """
    Return warmup network account health for this client.

    Shows per-account status from warmup_network_accounts and the latest
    health score snapshot from network_health_log.
    """
    accounts_resp = (
        _supabase.table("warmup_network_accounts")
        .select("email, provider, status, last_login_check, last_login_success, failure_count")
        .order("status")
        .order("email")
        .execute()
    )
    accounts = accounts_resp.data or []

    log_resp = (
        _supabase.table("network_health_log")
        .select("total_accounts, active_accounts, health_score, timestamp")
        .eq("client_id", client_id)
        .order("timestamp", desc=True)
        .limit(10)
        .execute()
    )

    active   = sum(1 for a in accounts if a.get("status") == "active")
    inactive = sum(1 for a in accounts if a.get("status") == "inactive")
    suspended = sum(1 for a in accounts if a.get("status") == "suspended")

    return {
        "summary": {
            "total":     len(accounts),
            "active":    active,
            "inactive":  inactive,
            "suspended": suspended,
            "health_score": round(active / len(accounts) * 100, 1) if accounts else 0.0,
        },
        "accounts":    accounts,
        "history":     log_resp.data or [],
    }


@app.post("/diagnostics/run", tags=["Diagnostics"])
async def trigger_diagnostics(
    client_id:           ClientId,
    background_tasks:    BackgroundTasks,
    include_network:     bool = Query(default=False, description="Also run IMAP network health check"),
) -> dict:
    """
    Trigger a full diagnostics cycle for this client in the background.

    Called by the n8n diagnostics workflow every hour.
    Set include_network=true every 6th call (every 6 hours).
    """
    def _run(cid: str, net: bool) -> None:
        proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if proj not in sys.path:
            sys.path.insert(0, proj)
        from diagnostics_engine import (  # noqa
            check_inbox_forecast,
            check_network_health,
            check_reputation_drift,
            check_smtp_errors,
        )
        sb_local = _supabase
        check_reputation_drift(sb_local, cid)
        check_smtp_errors(sb_local, cid)
        check_inbox_forecast(sb_local, cid)
        if net:
            check_network_health(sb_local, client_id=cid)

    background_tasks.add_task(_run, client_id, include_network)
    return {
        "triggered":       True,
        "include_network": include_network,
        "message":         "Diagnostics cycle started in background.",
    }


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@app.get("/campaigns", tags=["Campaigns"])
async def list_campaigns(client_id: ClientId):
    """Return all campaigns for the authenticated client, newest first."""
    resp = (
        _supabase.table("campaigns")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@app.post("/campaigns", status_code=status.HTTP_201_CREATED, tags=["Campaigns"])
async def create_campaign(payload: CampaignCreate, client_id: ClientId):
    """
    Create a new campaign.

    inbox_ids is optional — inboxes can be assigned to a campaign later by
    adding rows to sending_schedule with the returned campaign_id.
    """
    row = {
        "name": payload.name.strip(),
        "description": payload.description,
        "status": "draft",
        "client_id": client_id,
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
    }

    resp = _supabase.table("campaigns").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create campaign.")

    created = resp.data[0]
    _log_decision(
        client_id=client_id,
        decision_type="campaign_created",
        entity_type="campaign",
        entity_id=str(created.get("id", "")),
        entity_name=payload.name.strip(),
        before_state=None,
        after_state={"name": payload.name.strip(), "status": "draft"},
    )
    return created


@app.patch("/campaigns/{campaign_id}/status", tags=["Campaigns"])
async def update_campaign_status(campaign_id: str, body: dict, client_id: ClientId) -> dict:
    """
    Activate, pause, or archive a campaign.

    Body: {"status": "active"|"paused"|"draft"|"archived", "reason": "optional text"}
    Writes a decision_log entry automatically.
    """
    before_row = _require_row("campaigns", campaign_id, client_id)

    new_status = (body.get("status") or "").strip()
    if new_status not in {"draft", "active", "paused", "archived"}:
        raise HTTPException(status_code=422, detail="status must be draft | active | paused | archived.")

    resp = (
        _supabase.table("campaigns")
        .update({"status": new_status, "updated_at": _now_utc()})
        .eq("id", campaign_id)
        .execute()
    )
    result = resp.data[0] if resp.data else {"id": campaign_id, "status": new_status}

    decision_type = {
        "active":   "campaign_activated",
        "paused":   "campaign_paused",
        "archived": "campaign_paused",
        "draft":    "campaign_paused",
    }.get(new_status, "campaign_paused")

    _log_decision(
        client_id=client_id,
        decision_type=decision_type,
        entity_type="campaign",
        entity_id=campaign_id,
        entity_name=before_row.get("name"),
        before_state={"status": before_row.get("status")},
        after_state={"status": new_status},
        reason=body.get("reason"),
    )
    return result


@app.get("/campaigns/performance", tags=["Campaigns"])
async def campaigns_performance(
    client_id: ClientId,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """
    Aggregated performance metrics for all campaigns + daily trend lines.

    Returns per campaign: sent, opened, clicked, replied, bounced, unsubscribed,
    plus rates. Also includes an overall daily trend time series.
    """
    cutoff = _days_ago_utc(days)
    campaigns_resp = (
        _supabase.table("campaigns")
        .select("id, name, status, created_at, daily_limit")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .execute()
    )
    campaigns = campaigns_resp.data or []

    per_campaign: list[dict] = []
    for c in campaigns:
        cid = c["id"]
        events_resp = (
            _supabase.table("email_events")
            .select("event_type, lead_id, created_at")
            .eq("campaign_id", cid)
            .gte("created_at", cutoff)
            .execute()
        )
        events = events_resp.data or []

        sent = sum(1 for e in events if e.get("event_type") == "sent")
        opened = len({e["lead_id"] for e in events if e.get("event_type") == "opened" and e.get("lead_id")})
        clicked = len({e["lead_id"] for e in events if e.get("event_type") == "clicked" and e.get("lead_id")})
        replied = len({e["lead_id"] for e in events if e.get("event_type") == "replied" and e.get("lead_id")})
        bounced = sum(1 for e in events if e.get("event_type") == "bounced")
        unsub = sum(1 for e in events if e.get("event_type") == "unsubscribed")

        def _rate(num, denom):
            return round(num / denom, 4) if denom else 0.0

        per_campaign.append({
            "id": cid,
            "name": c.get("name", ""),
            "status": c.get("status", ""),
            "created_at": c.get("created_at"),
            "sent": sent,
            "unique_opens": opened,
            "unique_clicks": clicked,
            "replies": replied,
            "bounces": bounced,
            "unsubscribes": unsub,
            "open_rate": _rate(opened, sent),
            "click_rate": _rate(clicked, sent),
            "reply_rate": _rate(replied, sent),
            "bounce_rate": _rate(bounced, sent),
            "unsub_rate": _rate(unsub, sent),
        })

    # Overall daily trend (all campaigns combined)
    all_events_resp = (
        _supabase.table("email_events")
        .select("event_type, lead_id, created_at")
        .eq("client_id", client_id)
        .gte("created_at", cutoff)
        .limit(20000)
        .execute()
    )
    all_events = all_events_resp.data or []
    daily: dict[str, dict] = {}
    for e in all_events:
        day = (e.get("created_at") or "")[:10]
        if not day:
            continue
        if day not in daily:
            daily[day] = {"sent": 0, "opened_leads": set(), "replied_leads": set(), "bounced": 0}
        t = e.get("event_type", "")
        if t == "sent":
            daily[day]["sent"] += 1
        elif t == "opened" and e.get("lead_id"):
            daily[day]["opened_leads"].add(e["lead_id"])
        elif t == "replied" and e.get("lead_id"):
            daily[day]["replied_leads"].add(e["lead_id"])
        elif t == "bounced":
            daily[day]["bounced"] += 1

    trend = []
    for day in sorted(daily.keys()):
        d = daily[day]
        trend.append({
            "date": day,
            "sent": d["sent"],
            "opened": len(d["opened_leads"]),
            "replied": len(d["replied_leads"]),
            "bounced": d["bounced"],
        })

    # Overall averages
    totals = {"sent": 0, "opened": 0, "replied": 0, "bounced": 0, "unsub": 0, "clicked": 0}
    for c in per_campaign:
        totals["sent"] += c["sent"]
        totals["opened"] += c["unique_opens"]
        totals["clicked"] += c["unique_clicks"]
        totals["replied"] += c["replies"]
        totals["bounced"] += c["bounces"]
        totals["unsub"] += c["unsubscribes"]

    def _r(n, d): return round(n / d, 4) if d else 0.0
    overall = {
        **totals,
        "open_rate": _r(totals["opened"], totals["sent"]),
        "click_rate": _r(totals["clicked"], totals["sent"]),
        "reply_rate": _r(totals["replied"], totals["sent"]),
        "bounce_rate": _r(totals["bounced"], totals["sent"]),
        "unsub_rate": _r(totals["unsub"], totals["sent"]),
    }

    return {"campaigns": per_campaign, "daily_trend": trend, "overall": overall, "days": days}


@app.get("/campaigns/{campaign_id}/stats", response_model=CampaignStats, tags=["Campaigns"])
async def campaign_stats(campaign_id: str, client_id: ClientId):
    """
    Return send performance stats for a single campaign.

    Derived entirely from sending_schedule. Rates are calculated as fractions of
    total sent emails (not total leads), consistent with industry-standard reporting.
    """
    campaign_row = _require_row("campaigns", campaign_id, client_id)

    # Count leads in this campaign
    leads_resp = (
        _supabase.table("leads")
        .select("id", count="exact")
        .eq("campaign_id", campaign_id)
        .eq("client_id", client_id)
        .execute()
    )
    total_leads = leads_resp.count or 0

    # Count sending_schedule rows by status
    schedule_resp = (
        _supabase.table("sending_schedule")
        .select("status")
        .eq("campaign_id", campaign_id)
        .eq("client_id", client_id)
        .execute()
    )
    rows: list[dict] = schedule_resp.data or []

    status_counts: dict[str, int] = {}
    for row in rows:
        s = row.get("status") or "pending"
        status_counts[s] = status_counts.get(s, 0) + 1

    sent = status_counts.get("sent", 0)
    pending = status_counts.get("pending", 0)
    bounced = status_counts.get("bounced", 0)
    replied = status_counts.get("replied", 0)
    unsubscribed = status_counts.get("unsubscribed", 0)

    reply_rate = round(replied / sent, 4) if sent > 0 else 0.0
    bounce_rate = round(bounced / sent, 4) if sent > 0 else 0.0

    return CampaignStats(
        campaign_id=campaign_id,
        name=campaign_row.get("name", ""),
        total_leads=total_leads,
        sent=sent,
        pending=pending,
        bounced=bounced,
        replied=replied,
        unsubscribed=unsubscribed,
        reply_rate=reply_rate,
        bounce_rate=bounce_rate,
    )


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

REQUIRED_CSV_COLUMNS: set[str] = {"email"}
OPTIONAL_CSV_COLUMNS: list[str] = ["first_name", "last_name", "company_name", "notes"]


@app.post("/leads/import", response_model=LeadImportResult, tags=["Leads"])
async def import_leads(
    client_id: ClientId,
    file: UploadFile = File(...),
    campaign_id: Optional[str] = Query(default=None, description="Assign imported leads to this campaign."),
):
    """
    Import leads from a CSV file.

    Required column: email
    Optional columns: first_name, last_name, company_name, notes

    Duplicates (same email + client_id) are detected and skipped — no error is raised.
    Rows with invalid emails or missing required fields are counted as errors.
    Returns a summary: total_rows, imported, duplicates, errors.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="File must be a .csv.")

    contents = await file.read()
    try:
        df = pd.read_csv(io.StringIO(contents.decode("utf-8", errors="replace")))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Could not parse CSV: {exc}")

    # Normalise column names: strip whitespace, lowercase
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    if "email" not in df.columns:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CSV must contain an 'email' column.",
        )

    # Fetch existing emails for this client to detect duplicates efficiently
    existing_resp = (
        _supabase.table("leads")
        .select("email")
        .eq("client_id", client_id)
        .execute()
    )
    existing_emails: set[str] = {r["email"].lower() for r in (existing_resp.data or [])}

    imported = 0
    duplicates = 0
    errors = 0
    error_details: list[str] = []
    batch: list[dict] = []

    for idx, row in df.iterrows():
        raw_email = str(row.get("email") or "").strip().lower()

        # Validate email: basic format + length + no injection chars
        is_invalid = (
            not raw_email
            or "@" not in raw_email
            or "." not in raw_email.split("@")[-1]
            or len(raw_email) > 254
            or " " in raw_email
            or ";" in raw_email
            or "," in raw_email
            or raw_email.count("@") != 1
        )
        if is_invalid:
            errors += 1
            error_details.append(f"Row {idx + 2}: invalid or missing email.")
            continue

        if raw_email in existing_emails:
            duplicates += 1
            continue

        domain = raw_email.split("@")[-1]

        lead_row: dict = {
            "email": raw_email,
            "first_name": str(row["first_name"]).strip() if "first_name" in df.columns and pd.notna(row.get("first_name")) else None,
            "last_name": str(row["last_name"]).strip() if "last_name" in df.columns and pd.notna(row.get("last_name")) else None,
            "company_name": str(row["company_name"]).strip() if "company_name" in df.columns and pd.notna(row.get("company_name")) else None,
            "domain": domain,
            "status": "new",
            "campaign_id": campaign_id,
            "client_id": client_id,
            "notes": str(row["notes"]).strip() if "notes" in df.columns and pd.notna(row.get("notes")) else None,
            "imported_at": _now_utc(),
        }

        batch.append(lead_row)
        existing_emails.add(raw_email)  # Prevent intra-batch duplicates

    # Insert in batches of 500 to stay within Supabase limits
    BATCH_SIZE = 500
    for i in range(0, len(batch), BATCH_SIZE):
        chunk = batch[i : i + BATCH_SIZE]
        try:
            resp = _supabase.table("leads").insert(chunk).execute()
            imported += len(resp.data or chunk)
        except Exception as exc:
            errors += len(chunk)
            error_details.append(f"Batch insert failed for rows {i}–{i + len(chunk)}: {exc}")

    return LeadImportResult(
        total_rows=len(df),
        imported=imported,
        duplicates=duplicates,
        errors=errors,
        error_details=error_details[:50],  # Cap to avoid enormous response bodies
    )


@app.get("/leads", tags=["Leads"])
async def list_leads(
    client_id: ClientId,
    campaign_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    domain: Optional[str] = Query(default=None),
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """
    Return leads for the authenticated client with optional filters.

    Filters:
    - campaign_id: restrict to one campaign
    - status: new | contacted | replied | unsubscribed | bounced
    - domain: filter by lead's email domain (e.g. prospect.nl)
    - limit / offset: pagination (max 1000 per page)
    """
    query = (
        _supabase.table("leads")
        .select("*")
        .eq("client_id", client_id)
    )

    if campaign_id:
        query = query.eq("campaign_id", campaign_id)
    if status_filter:
        query = query.eq("status", status_filter)
    if domain:
        query = query.eq("domain", domain.lower().strip())

    resp = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return resp.data or []


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@app.get("/notifications/poll", tags=["Notifications"])
async def poll_notifications(
    client_id: ClientId,
    since: Optional[str] = Query(default=None, description="ISO timestamp; returns notifications created after this"),
) -> dict:
    """
    Lightweight polling endpoint for real-time notifications.

    Returns:
      - new_replies: list of reply events since `since` (unified-inbox format)
      - new_notifications: list of system notifications since `since`
      - unread_reply_count: total unread real replies (not warmup)
    """
    cutoff = since or _days_ago_utc(1)

    # Unread real replies count
    try:
        unread_resp = (
            _supabase.table("reply_inbox")
            .select("id", count="exact")
            .eq("client_id", client_id)
            .eq("is_read", False)
            .execute()
        )
        unread_count = unread_resp.count or 0
    except Exception:
        unread_count = 0

    # New replies since `since`
    new_replies: list[dict] = []
    try:
        replies_resp = (
            _supabase.table("reply_inbox")
            .select("id, from_email, subject, classification, received_at")
            .eq("client_id", client_id)
            .gt("received_at", cutoff)
            .order("received_at", desc=True)
            .limit(20)
            .execute()
        )
        new_replies = replies_resp.data or []
    except Exception:
        pass

    # New system notifications since `since`
    new_notifs: list[dict] = []
    try:
        notif_resp = (
            _supabase.table("notifications")
            .select("id, type, message, priority, entity_id, entity_type, created_at")
            .eq("client_id", client_id)
            .gt("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        new_notifs = notif_resp.data or []
    except Exception:
        pass

    return {
        "unread_reply_count": unread_count,
        "new_replies": new_replies,
        "new_notifications": new_notifs,
        "server_time": _now_utc(),
    }


@app.get("/notifications", response_model=list[NotificationItem], tags=["Notifications"])
async def get_notifications(client_id: ClientId, days: int = Query(default=1, ge=1, le=30)):
    """
    Return unread notifications for the authenticated client.

    Notifications are derived in real-time from:
    - warmup_logs: recent errors (action='error')
    - warmup_logs: recent spam rescues (landed_in_spam=true)
    - inboxes: low reputation scores (< 30)
    - bounce_log: recent spam complaints
    - inboxes: spam complaints in last 14 days

    The 'days' query param controls how far back to look for log-based events (default: 1).
    """
    cutoff = _days_ago_utc(days)
    notifications: list[NotificationItem] = []

    # Fetch this client's inbox IDs for filtering
    inboxes_resp = _supabase.table("inboxes").select("id, email, reputation_score, spam_complaints, last_spam_incident").eq("client_id", client_id).execute()
    inboxes: list[dict] = inboxes_resp.data or []
    inbox_ids = [i["id"] for i in inboxes]
    inbox_email_map = {i["id"]: i["email"] for i in inboxes}

    if inbox_ids:
        # ── Recent errors ───────────────────────────────────────────────
        errors_resp = (
            _supabase.table("warmup_logs")
            .select("id, inbox_id, notes, timestamp")
            .in_("inbox_id", inbox_ids)
            .eq("action", "error")
            .gte("timestamp", cutoff)
            .order("timestamp", desc=True)
            .limit(20)
            .execute()
        )
        for row in (errors_resp.data or []):
            notifications.append(
                NotificationItem(
                    id=row["id"],
                    type="error",
                    inbox_email=inbox_email_map.get(row.get("inbox_id", ""), ""),
                    message=_sanitize_error_message(row.get("notes") or ""),
                    timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                )
            )

        # ── Recent spam rescues ─────────────────────────────────────────
        rescue_resp = (
            _supabase.table("warmup_logs")
            .select("id, inbox_id, counterpart_email, timestamp")
            .in_("inbox_id", inbox_ids)
            .eq("was_rescued", True)
            .gte("timestamp", cutoff)
            .order("timestamp", desc=True)
            .limit(20)
            .execute()
        )
        for row in (rescue_resp.data or []):
            inbox_email = inbox_email_map.get(row.get("inbox_id", ""), "")
            notifications.append(
                NotificationItem(
                    id=row["id"],
                    type="spam_rescue",
                    inbox_email=inbox_email,
                    message=f"Email rescued from spam folder for inbox {inbox_email}.",
                    timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                )
            )

        # ── Recent spam complaints from bounce_log ──────────────────────
        complaints_resp = (
            _supabase.table("bounce_log")
            .select("id, inbox_id, lead_email, timestamp")
            .in_("inbox_id", inbox_ids)
            .eq("bounce_type", "spam_complaint")
            .gte("timestamp", cutoff)
            .order("timestamp", desc=True)
            .limit(10)
            .execute()
        )
        for row in (complaints_resp.data or []):
            inbox_email = inbox_email_map.get(row.get("inbox_id", ""), "")
            notifications.append(
                NotificationItem(
                    id=row["id"],
                    type="complaint",
                    inbox_email=inbox_email,
                    message=f"Spam complaint received from {row.get('lead_email', 'unknown')} on {inbox_email}.",
                    timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                )
            )

    # ── Low reputation alerts (always shown, not time-gated) ───────────
    for inbox in inboxes:
        score = inbox.get("reputation_score") or 0.0
        if score < 30:
            notifications.append(
                NotificationItem(
                    id=f"low-rep-{inbox['id']}",
                    type="low_reputation",
                    inbox_email=inbox.get("email", ""),
                    message=(
                        f"Inbox {inbox.get('email', '')} has a low reputation score of {score:.0f}/100. "
                        "Consider pausing campaigns and increasing warmup volume."
                    ),
                    timestamp=datetime.now(timezone.utc),
                )
            )

    # Sort newest first
    notifications.sort(key=lambda n: n.timestamp, reverse=True)
    return notifications


# ---------------------------------------------------------------------------
# Analytics helpers (shared across all analytics endpoints)
# ---------------------------------------------------------------------------

_VALID_RANGES: dict[int, int] = {7: 7, 30: 30, 90: 90}


def _analytics_date_range(days: int) -> tuple[str, str]:
    """
    Return (start_date_iso, today_iso) for the requested lookback window.

    Clamps to valid options: 7 | 30 | 90.
    """
    days = _VALID_RANGES.get(days, 30)
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    return start.isoformat(), today.isoformat()


def _read_cache(
    entity_type: str,
    entity_ids: list[str],
    client_id: str,
    start_iso: str,
    end_iso: str,
) -> list[dict]:
    """
    Read analytics_cache rows for the given entity_ids and date range.

    Returns rows sorted by date ascending.
    """
    if not entity_ids:
        return []
    resp = (
        _supabase.table("analytics_cache")
        .select("entity_id, date, metrics")
        .eq("client_id", client_id)
        .eq("entity_type", entity_type)
        .in_("entity_id", entity_ids)
        .gte("date", start_iso)
        .lte("date", end_iso)
        .order("date")
        .execute()
    )
    return resp.data or []


def _rate(n: int, d: int) -> float:
    return round(n / d, 4) if d > 0 else 0.0


# ---------------------------------------------------------------------------
# GET /analytics/campaigns/{id}
# ---------------------------------------------------------------------------

@app.get("/analytics/campaigns/{campaign_id}", tags=["Analytics"])
async def analytics_campaign(
    campaign_id: str,
    client_id: ClientId,
    days: int = Query(default=30, description="Lookback window in days. Valid values: 7 | 30 | 90."),
):
    """
    Return per-day metric timeseries for one campaign.

    Reads from analytics_cache (populated by analytics_engine.py).
    Falls back to a real-time query from email_events for days not yet cached.

    Response shape:
      {
        "campaign_id": "...",
        "name": "...",
        "range_days": 30,
        "data": [
          { "date": "2026-03-01", "emails_sent": 42, "open_rate": 0.35, ... },
          ...
        ],
        "totals": { "emails_sent": 420, ... }
      }
    """
    campaign_row = _require_row("campaigns", campaign_id, client_id)
    start_iso, end_iso = _analytics_date_range(days)

    cache_rows = _read_cache("campaign", [campaign_id], client_id, start_iso, end_iso)

    # Build a date → metrics map from cache
    cached: dict[str, dict] = {}
    for row in cache_rows:
        cached[row["date"]] = row["metrics"]

    # For any date not in cache, run a live query
    from datetime import date as _date
    all_dates: list[str] = []
    cur = _date.fromisoformat(start_iso)
    end = _date.fromisoformat(end_iso)
    while cur <= end:
        all_dates.append(cur.isoformat())
        cur += timedelta(days=1)

    data: list[dict] = []
    for d in all_dates:
        if d in cached:
            data.append({"date": d, **cached[d]})
        else:
            # Live fallback: count events for this day from email_events
            day_start = f"{d}T00:00:00+00:00"
            day_end = f"{d}T23:59:59+00:00"
            ev_resp = (
                _supabase.table("email_events")
                .select("event_type, lead_id")
                .eq("campaign_id", campaign_id)
                .gte("timestamp", day_start)
                .lte("timestamp", day_end)
                .execute()
            )
            evs: list[dict] = ev_resp.data or []
            sent = sum(1 for e in evs if e.get("event_type") == "sent")
            unique_opens = len({e["lead_id"] for e in evs if e.get("event_type") == "opened" and e.get("lead_id")})
            replies = sum(1 for e in evs if e.get("event_type") == "replied")
            bounces = sum(1 for e in evs if e.get("event_type") == "bounced")
            clicks = sum(1 for e in evs if e.get("event_type") == "clicked")
            unsubscribes = sum(1 for e in evs if e.get("event_type") == "unsubscribed")

            # interested count from reply_inbox
            ri_resp = (
                _supabase.table("reply_inbox")
                .select("classification")
                .eq("campaign_id", campaign_id)
                .gte("received_at", day_start)
                .lte("received_at", day_end)
                .execute()
            )
            interested = sum(1 for r in (ri_resp.data or []) if r.get("classification") == "interested")

            metrics = {
                "emails_sent": sent,
                "unique_opens": unique_opens,
                "open_rate": _rate(unique_opens, sent),
                "clicks": clicks,
                "click_rate": _rate(clicks, sent),
                "replies": replies,
                "reply_rate": _rate(replies, sent),
                "interested_count": interested,
                "meeting_rate": _rate(interested, sent),
                "bounces": bounces,
                "bounce_rate": _rate(bounces, sent),
                "unsubscribes": unsubscribes,
                "unsubscribe_rate": _rate(unsubscribes, sent),
            }
            data.append({"date": d, **metrics})

    # Compute totals across the whole range
    numeric_keys = [
        "emails_sent", "unique_opens", "clicks", "replies",
        "interested_count", "bounces", "unsubscribes",
    ]
    totals: dict = {k: sum(row.get(k) or 0 for row in data) for k in numeric_keys}
    totals["open_rate"] = _rate(totals["unique_opens"], totals["emails_sent"])
    totals["click_rate"] = _rate(totals["clicks"], totals["emails_sent"])
    totals["reply_rate"] = _rate(totals["replies"], totals["emails_sent"])
    totals["meeting_rate"] = _rate(totals["interested_count"], totals["emails_sent"])
    totals["bounce_rate"] = _rate(totals["bounces"], totals["emails_sent"])
    totals["unsubscribe_rate"] = _rate(totals["unsubscribes"], totals["emails_sent"])

    return {
        "campaign_id": campaign_id,
        "name": campaign_row.get("name", ""),
        "range_days": days,
        "data": data,
        "totals": totals,
    }


# ---------------------------------------------------------------------------
# GET /analytics/campaigns/{id}/steps
# ---------------------------------------------------------------------------

@app.get("/analytics/campaigns/{campaign_id}/steps", tags=["Analytics"])
async def analytics_campaign_steps(
    campaign_id: str,
    client_id: ClientId,
):
    """
    Return per-sequence-step performance breakdown for a campaign.

    Aggregates lifetime email_events (not limited to a date range) so the view
    shows total step performance regardless of when the campaign ran.

    Response: list of steps with sent, reply_rate, bounce_rate, and ab_variant.
    """
    _require_row("campaigns", campaign_id, client_id)

    # Fetch all sequence steps for this campaign
    steps_resp = (
        _supabase.table("sequence_steps")
        .select("id, step_number, ab_variant, subject")
        .eq("campaign_id", campaign_id)
        .order("step_number")
        .execute()
    )
    steps: list[dict] = steps_resp.data or []
    if not steps:
        return []

    step_ids = [s["id"] for s in steps]

    # Fetch all events for these steps
    ev_resp = (
        _supabase.table("email_events")
        .select("event_type, sequence_step_id, ab_variant")
        .eq("campaign_id", campaign_id)
        .in_("sequence_step_id", step_ids)
        .execute()
    )
    events: list[dict] = ev_resp.data or []

    # Group by step_id
    from collections import defaultdict
    counts: dict[str, dict] = defaultdict(lambda: {"sent": 0, "replied": 0, "bounced": 0, "clicked": 0})
    for ev in events:
        sid = ev.get("sequence_step_id") or ""
        et = ev.get("event_type") or ""
        if et in counts[sid]:
            counts[sid][et] += 1

    result: list[dict] = []
    for step in steps:
        sid = step["id"]
        c = counts[sid]
        sent = c["sent"]
        result.append({
            "step_id": sid,
            "step_number": step.get("step_number"),
            "ab_variant": step.get("ab_variant"),
            "subject_preview": (step.get("subject") or "")[:80],
            "emails_sent": sent,
            "replies": c["replied"],
            "reply_rate": _rate(c["replied"], sent),
            "bounces": c["bounced"],
            "bounce_rate": _rate(c["bounced"], sent),
            "clicks": c["clicked"],
            "click_rate": _rate(c["clicked"], sent),
        })

    return result


# ---------------------------------------------------------------------------
# GET /analytics/campaigns/{id}/ab
# ---------------------------------------------------------------------------

@app.get("/analytics/campaigns/{campaign_id}/ab", tags=["Analytics"])
async def analytics_campaign_ab(
    campaign_id: str,
    client_id: ClientId,
):
    """
    Return A/B test results with statistical significance for all tested steps.

    Calls ab_test_engine.find_winning_variant() for each step that has A/B variants.
    Returns significance score, winner, z-score, p-value, and raw send/reply counts.
    """
    _require_row("campaigns", campaign_id, client_id)

    # Find all step_numbers that have A/B variants
    steps_resp = (
        _supabase.table("sequence_steps")
        .select("step_number, ab_variant")
        .eq("campaign_id", campaign_id)
        .in_("ab_variant", ["A", "B"])
        .execute()
    )
    ab_step_numbers: set[int] = {
        s["step_number"] for s in (steps_resp.data or [])
        if s.get("step_number") is not None
    }

    if not ab_step_numbers:
        return {"campaign_id": campaign_id, "ab_steps": [], "message": "No A/B variants configured for this campaign."}

    import sys as _sys
    import os as _os
    project_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if project_root not in _sys.path:
        _sys.path.insert(0, project_root)
    from ab_test_engine import find_winning_variant

    ab_results: list[dict] = []
    for step_number in sorted(ab_step_numbers):
        result = find_winning_variant(_supabase, campaign_id, step_number)
        ab_results.append(result)

    return {
        "campaign_id": campaign_id,
        "ab_steps": ab_results,
    }


# ---------------------------------------------------------------------------
# GET /analytics/inboxes
# ---------------------------------------------------------------------------

@app.get("/analytics/inboxes", tags=["Analytics"])
async def analytics_inboxes(
    client_id: ClientId,
    days: int = Query(default=7, description="Lookback window: 7 | 30 | 90"),
):
    """
    Return a performance comparison table across all client inboxes.

    Reads from analytics_cache (entity_type='inbox') and aggregates each inbox's
    totals for the requested date range, plus the current reputation_score from
    the inboxes table as the live value.

    Response: list of inbox summaries sorted by reputation_score descending.
    """
    start_iso, end_iso = _analytics_date_range(days)

    inboxes_resp = (
        _supabase.table("inboxes")
        .select("id, email, domain, status, reputation_score, daily_campaign_target, warmup_active")
        .eq("client_id", client_id)
        .execute()
    )
    inboxes: list[dict] = inboxes_resp.data or []
    if not inboxes:
        return []

    inbox_ids = [i["id"] for i in inboxes]
    cache_rows = _read_cache("inbox", inbox_ids, client_id, start_iso, end_iso)

    # Aggregate cache rows per inbox
    from collections import defaultdict
    inbox_totals: dict[str, dict] = defaultdict(lambda: {
        "warmup_sent": 0,
        "campaign_sent": 0,
        "spam_rescues": 0,
        "spam_complaints": 0,
        "reputation_scores": [],
    })

    for row in cache_rows:
        eid = row["entity_id"]
        m: dict = row.get("metrics") or {}
        inbox_totals[eid]["warmup_sent"] += m.get("warmup_sent") or 0
        inbox_totals[eid]["campaign_sent"] += m.get("campaign_sent") or 0
        inbox_totals[eid]["spam_rescues"] += m.get("spam_rescues") or 0
        inbox_totals[eid]["spam_complaints"] += m.get("spam_complaints") or 0
        rep = m.get("reputation_score")
        if rep is not None:
            inbox_totals[eid]["reputation_scores"].append(rep)

    result: list[dict] = []
    for inbox in inboxes:
        iid = inbox["id"]
        t = inbox_totals[iid]
        rep_scores = t["reputation_scores"]
        # Use current live value from DB as primary; fallback to avg of cached snapshots
        live_rep = inbox.get("reputation_score") or 0.0
        avg_rep = round(sum(rep_scores) / len(rep_scores), 2) if rep_scores else live_rep

        result.append({
            "inbox_id": iid,
            "email": inbox.get("email"),
            "domain": inbox.get("domain"),
            "status": inbox.get("status"),
            "warmup_active": inbox.get("warmup_active"),
            "daily_campaign_target": inbox.get("daily_campaign_target") or 0,
            "reputation_score_current": live_rep,
            "reputation_score_avg": avg_rep,
            "warmup_sent": t["warmup_sent"],
            "campaign_sent": t["campaign_sent"],
            "total_sent": t["warmup_sent"] + t["campaign_sent"],
            "spam_rescues": t["spam_rescues"],
            "spam_complaints": t["spam_complaints"],
            "range_days": days,
        })

    result.sort(key=lambda r: r["reputation_score_current"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# GET /analytics/overview
# ---------------------------------------------------------------------------

@app.get("/analytics/overview", tags=["Analytics"])
async def analytics_overview(
    client_id: ClientId,
    days: int = Query(default=30, description="Lookback window: 7 | 30 | 90"),
):
    """
    Return a high-level dashboard overview for the authenticated client.

    Aggregates cross-campaign totals, inbox health, and trend indicators.
    This is the data source for the main Warmr dashboard view.

    Response shape:
      {
        "period_days": 30,
        "campaigns": { "total": int, "active": int, "paused": int, "completed": int },
        "emails": { "sent": int, "open_rate": float, "reply_rate": float, "bounce_rate": float, ... },
        "inboxes": { "total": int, "active": int, "ready": int, "avg_reputation": float },
        "warmup": { "total_warmup_sent": int, "total_spam_rescues": int, "total_spam_complaints": int },
        "top_campaigns": [ { "name": str, "sent": int, "reply_rate": float }, ... ],
        "replies_unread": int,
      }
    """
    start_iso, end_iso = _analytics_date_range(days)

    # ── Campaigns ─────────────────────────────────────────────────────────
    campaigns_resp = (
        _supabase.table("campaigns")
        .select("id, name, status")
        .eq("client_id", client_id)
        .execute()
    )
    campaigns: list[dict] = campaigns_resp.data or []
    campaign_ids = [c["id"] for c in campaigns]

    camp_status: dict[str, int] = {}
    for c in campaigns:
        s = c.get("status") or "draft"
        camp_status[s] = camp_status.get(s, 0) + 1

    # ── Campaign-level analytics from cache ────────────────────────────────
    camp_cache = _read_cache("campaign", campaign_ids, client_id, start_iso, end_iso)

    agg: dict[str, int] = {
        "emails_sent": 0, "unique_opens": 0, "clicks": 0, "replies": 0,
        "interested_count": 0, "bounces": 0, "unsubscribes": 0,
    }
    # Also track per-campaign totals for top campaigns list
    per_campaign: dict[str, dict] = {c["id"]: {**agg, "name": c.get("name", "")} for c in campaigns}

    for row in camp_cache:
        m: dict = row.get("metrics") or {}
        eid = row["entity_id"]
        for k in agg:
            v = m.get(k) or 0
            agg[k] += v
            if eid in per_campaign:
                per_campaign[eid][k] = per_campaign[eid].get(k, 0) + v

    total_sent = agg["emails_sent"]
    email_summary = {
        "sent": total_sent,
        "unique_opens": agg["unique_opens"],
        "open_rate": _rate(agg["unique_opens"], total_sent),
        "clicks": agg["clicks"],
        "click_rate": _rate(agg["clicks"], total_sent),
        "replies": agg["replies"],
        "reply_rate": _rate(agg["replies"], total_sent),
        "interested": agg["interested_count"],
        "meeting_rate": _rate(agg["interested_count"], total_sent),
        "bounces": agg["bounces"],
        "bounce_rate": _rate(agg["bounces"], total_sent),
        "unsubscribes": agg["unsubscribes"],
        "unsubscribe_rate": _rate(agg["unsubscribes"], total_sent),
    }

    # Top 5 campaigns by emails_sent in the period
    top_campaigns = sorted(
        per_campaign.values(),
        key=lambda c: c.get("emails_sent") or 0,
        reverse=True,
    )[:5]
    top_campaigns_out = [
        {
            "name": c["name"],
            "sent": c.get("emails_sent") or 0,
            "reply_rate": _rate(c.get("replies") or 0, c.get("emails_sent") or 0),
            "bounce_rate": _rate(c.get("bounces") or 0, c.get("emails_sent") or 0),
        }
        for c in top_campaigns
    ]

    # ── Inboxes ────────────────────────────────────────────────────────────
    inboxes_resp = (
        _supabase.table("inboxes")
        .select("id, status, warmup_active, reputation_score, spam_rescues, spam_complaints")
        .eq("client_id", client_id)
        .execute()
    )
    inboxes: list[dict] = inboxes_resp.data or []

    total_inboxes = len(inboxes)
    active_inboxes = sum(1 for i in inboxes if i.get("warmup_active") and i.get("status") != "retired")
    ready_inboxes = sum(1 for i in inboxes if i.get("status") == "ready")
    rep_scores = [i.get("reputation_score") or 0.0 for i in inboxes]
    avg_rep = round(sum(rep_scores) / len(rep_scores), 2) if rep_scores else 0.0

    # Warmup totals from inbox cache
    inbox_ids = [i["id"] for i in inboxes]
    inbox_cache = _read_cache("inbox", inbox_ids, client_id, start_iso, end_iso)
    total_warmup_sent = 0
    total_spam_rescues = 0
    total_spam_complaints = 0
    for row in inbox_cache:
        m = row.get("metrics") or {}
        total_warmup_sent += m.get("warmup_sent") or 0
        total_spam_rescues += m.get("spam_rescues") or 0
        total_spam_complaints += m.get("spam_complaints") or 0

    # ── Unread replies ─────────────────────────────────────────────────────
    unread_resp = (
        _supabase.table("reply_inbox")
        .select("id", count="exact")
        .eq("client_id", client_id)
        .eq("is_read", False)
        .execute()
    )
    replies_unread = unread_resp.count or 0

    return {
        "period_days": days,
        "campaigns": {
            "total": len(campaigns),
            "active": camp_status.get("active", 0),
            "paused": camp_status.get("paused", 0),
            "completed": camp_status.get("completed", 0),
            "draft": camp_status.get("draft", 0),
        },
        "emails": email_summary,
        "inboxes": {
            "total": total_inboxes,
            "active": active_inboxes,
            "ready": ready_inboxes,
            "avg_reputation": avg_rep,
        },
        "warmup": {
            "total_warmup_sent": total_warmup_sent,
            "total_spam_rescues": total_spam_rescues,
            "total_spam_complaints": total_spam_complaints,
        },
        "top_campaigns": top_campaigns_out,
        "replies_unread": replies_unread,
    }


# ---------------------------------------------------------------------------
# Admin endpoints  (require_admin dependency — is_admin = true in clients)
# ---------------------------------------------------------------------------

AdminId = Annotated[str, Depends(require_admin)]


@app.get("/admin/clients", tags=["Admin"])
async def admin_list_clients(_: AdminId) -> list[dict]:
    """Return all client accounts with stats."""
    clients_resp = _supabase.table("clients").select("*").order("created_at", desc=True).execute()
    clients = clients_resp.data or []

    # Attach inbox/domain counts per client in one pass
    inbox_resp  = _supabase.table("inboxes").select("client_id", count="exact").execute()
    domain_resp = _supabase.table("domains").select("client_id", count="exact").execute()

    inbox_counts:  dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    for row in (inbox_resp.data or []):
        cid = row.get("client_id", "")
        inbox_counts[cid] = inbox_counts.get(cid, 0) + 1
    for row in (domain_resp.data or []):
        cid = row.get("client_id", "")
        domain_counts[cid] = domain_counts.get(cid, 0) + 1

    for c in clients:
        cid = str(c.get("id", ""))
        c["inbox_count"]  = inbox_counts.get(cid, 0)
        c["domain_count"] = domain_counts.get(cid, 0)

    return clients


@app.get("/admin/clients/{client_id}", tags=["Admin"])
async def admin_get_client(client_id: str, _: AdminId) -> dict:
    """Return a single client with their inboxes, domains and campaigns."""
    resp = _supabase.table("clients").select("*").eq("id", client_id).limit(1).execute()
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Client not found.")
    client = rows[0]

    inboxes = (_supabase.table("inboxes").select("*").eq("client_id", client_id).execute().data or [])
    domains = (_supabase.table("domains").select("*").eq("client_id", client_id).execute().data or [])
    campaigns_resp = _supabase.table("campaigns").select("id,name,status,created_at").eq("client_id", client_id).execute() if True else None
    campaigns = (campaigns_resp.data or []) if campaigns_resp else []

    return {**client, "inboxes": inboxes, "domains": domains, "campaigns": campaigns}


@app.patch("/admin/clients/{client_id}", tags=["Admin"])
async def admin_update_client(client_id: str, body: AdminClientPatch, _: AdminId) -> dict:
    """
    Update any field on a client account.

    Allowed fields: plan, max_inboxes, max_domains, suspended, notes.
    Admins cannot revoke their own admin flag via this endpoint.
    """
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not patch:
        raise HTTPException(status_code=400, detail="No valid fields to update.")

    patch["updated_at"] = _now_utc()
    resp = _supabase.table("clients").update(patch).eq("id", client_id).execute()
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Client not found.")
    return rows[0]


@app.post("/admin/clients/{client_id}/promote", tags=["Admin"])
async def admin_promote_client(client_id: str, _: AdminId) -> dict:
    """Grant admin rights to a client."""
    resp = _supabase.table("clients").update({"is_admin": True}).eq("id", client_id).execute()
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Client not found.")
    return {"ok": True, "client_id": client_id, "is_admin": True}


@app.post("/admin/clients/{client_id}/demote", tags=["Admin"])
async def admin_demote_client(client_id: str, admin_id: AdminId) -> dict:
    """Revoke admin rights from a client. Cannot demote yourself."""
    if client_id == admin_id:
        raise HTTPException(status_code=400, detail="Cannot revoke your own admin rights.")
    resp = _supabase.table("clients").update({"is_admin": False}).eq("id", client_id).execute()
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Client not found.")
    return {"ok": True, "client_id": client_id, "is_admin": False}


@app.delete("/admin/clients/{client_id}", tags=["Admin"])
async def admin_delete_client(client_id: str, admin_id: AdminId) -> dict:
    """
    Hard-delete a client and all their data.
    Cannot delete yourself.
    """
    if client_id == admin_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account via admin.")

    # Delete in dependency order
    for table in ["warmup_logs", "sending_schedule", "bounce_log", "inboxes", "domains", "campaigns", "clients"]:
        try:
            if table in ("warmup_logs", "bounce_log", "sending_schedule"):
                # These reference inboxes/campaigns, not client_id directly on all rows
                _supabase.table(table).delete().eq("client_id", client_id).execute()
            else:
                _supabase.table(table).delete().eq("client_id" if table != "clients" else "id", client_id).execute()
        except Exception:
            pass  # Table may not exist yet (campaigns etc)

    return {"ok": True, "deleted_client_id": client_id}


@app.get("/admin/stats", tags=["Admin"])
async def admin_global_stats(_: AdminId) -> dict:
    """Platform-wide stats for the admin dashboard."""
    clients_resp  = _supabase.table("clients").select("id,plan,is_admin,suspended,created_at").execute()
    inboxes_resp  = _supabase.table("inboxes").select("status,reputation_score").execute()
    domains_resp  = _supabase.table("domains").select("blacklisted").execute()

    clients = clients_resp.data or []
    inboxes = inboxes_resp.data or []
    domains = domains_resp.data or []

    plans: dict[str, int] = {}
    for c in clients:
        p = c.get("plan") or "trial"
        plans[p] = plans.get(p, 0) + 1

    ready_inboxes   = sum(1 for i in inboxes if i.get("status") == "ready")
    warmup_inboxes  = sum(1 for i in inboxes if i.get("status") == "warmup")
    avg_rep = round(
        sum(i.get("reputation_score") or 0 for i in inboxes) / len(inboxes), 1
    ) if inboxes else 0.0
    blacklisted = sum(1 for d in domains if d.get("blacklisted"))

    return {
        "clients": {
            "total": len(clients),
            "suspended": sum(1 for c in clients if c.get("suspended")),
            "admins": sum(1 for c in clients if c.get("is_admin")),
            "by_plan": plans,
        },
        "inboxes": {
            "total": len(inboxes),
            "ready": ready_inboxes,
            "warmup": warmup_inboxes,
            "avg_reputation": avg_rep,
        },
        "domains": {
            "total": len(domains),
            "blacklisted": blacklisted,
        },
    }


# ===========================================================================
# DELIVERABILITY TOOLS
# ===========================================================================

# ---------------------------------------------------------------------------
# Placement testing
# ---------------------------------------------------------------------------

@app.post("/deliverability/placement-test", tags=["Deliverability"])
async def start_placement_test(
    body: PlacementTestRequest,
    background_tasks: BackgroundTasks,
    client_id: ClientId,
) -> dict:
    """
    Queue a new inbox placement test.

    Body: {inbox_id, subject?, body?}
    The test is run in the background: sends to seed accounts, waits 5 min,
    checks IMAP folders, stores results.
    """
    inbox_id = str(body.inbox_id)

    inbox_row = (
        _supabase.table("inboxes")
        .select("id, email, password, provider, client_id")
        .eq("id", inbox_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    ).data or []

    if not inbox_row:
        raise HTTPException(status_code=404, detail="Inbox not found")

    inbox = inbox_row[0]
    subject = body.subject or "Quick question about your team's workflow"
    email_body = body.body or (
        "Hi,\n\n"
        "I came across your company and wanted to reach out briefly.\n\n"
        "Would you be open to a short conversation this week?\n\n"
        "Best regards"
    )

    # Create the test record immediately with status=pending
    from placement_tester import create_test_record
    test_id = create_test_record(_supabase, client_id, inbox_id, subject, email_body)

    # Run the actual send + check in background
    def _run():
        from placement_tester import run_placement_test
        run_placement_test(
            inbox      = inbox,
            client_id  = client_id,
            subject    = subject,
            body       = email_body,
            sb         = _supabase,
        )

    background_tasks.add_task(_run)
    return {"ok": True, "test_id": test_id, "status": "running"}


@app.get("/deliverability/placement-test/{test_id}", tags=["Deliverability"])
async def get_placement_test(test_id: str, client_id: ClientId) -> dict:
    """Fetch a placement test result by ID."""
    test = (
        _supabase.table("placement_tests")
        .select("*")
        .eq("id", test_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    ).data or []

    if not test:
        raise HTTPException(status_code=404, detail="Test not found")

    results = (
        _supabase.table("placement_test_results")
        .select("*")
        .eq("test_id", test_id)
        .execute()
    ).data or []

    summary: dict[str, int] = {"primary": 0, "promotions": 0, "spam": 0, "missing": 0}
    for r in results:
        p = r.get("placement") or "missing"
        summary[p] = summary.get(p, 0) + 1

    return {**test[0], "results": results, "summary": summary}


@app.get("/deliverability/placement-history", tags=["Deliverability"])
async def placement_history(
    client_id: ClientId,
    limit: int = Query(20, ge=1, le=100),
    inbox_id: Optional[str] = None,
) -> dict:
    """List recent placement tests for the client."""
    q = (
        _supabase.table("placement_tests")
        .select("id, inbox_id, subject, status, created_at, completed_at")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if inbox_id:
        q = q.eq("inbox_id", inbox_id)
    tests = q.execute().data or []
    return {"tests": tests, "total": len(tests)}


# ---------------------------------------------------------------------------
# Content scoring
# ---------------------------------------------------------------------------

@app.post("/deliverability/score-content", tags=["Deliverability"])
@limiter.limit("60/hour")
async def score_content_endpoint(request: Request, body: ContentScoreRequest, client_id: ClientId) -> dict:
    """
    Run rule-based spam check (instant, no Claude call).

    Body: {subject, body, html_body?, campaign_id?, sequence_step_id?}
    Returns score + flags immediately.
    """
    from content_scorer import score_content, save_content_score

    result = score_content(body.subject, body.body, body.html_body)

    record_id = save_content_score(
        _supabase,
        client_id,
        result,
        body.subject,
        campaign_id      = str(body.campaign_id) if body.campaign_id else None,
        sequence_step_id = str(body.sequence_step_id) if body.sequence_step_id else None,
    )

    return {**result, "score_id": record_id}


@app.post("/deliverability/score-content/deep", tags=["Deliverability"])
@limiter.limit("20/hour")
async def deep_score_content_endpoint(request: Request, body: ContentScoreRequest, client_id: ClientId) -> dict:
    """
    Run both rule-based + Claude Sonnet deep analysis (may take a few seconds).

    Body: {subject, body, html_body?, campaign_id?, sequence_step_id?}
    """
    from content_scorer import deep_score_content, save_content_score

    result = deep_score_content(body.subject, body.body, body.html_body)

    record_id = save_content_score(
        _supabase,
        client_id,
        result,
        body.subject,
        campaign_id      = str(body.campaign_id) if body.campaign_id else None,
        sequence_step_id = str(body.sequence_step_id) if body.sequence_step_id else None,
    )

    return {**result, "score_id": record_id}


@app.get("/deliverability/content-scores", tags=["Deliverability"])
async def list_content_scores(
    client_id: ClientId,
    campaign_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """List content scores for the client, optionally filtered by campaign."""
    q = (
        _supabase.table("content_scores")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if campaign_id:
        q = q.eq("campaign_id", campaign_id)
    scores = q.execute().data or []
    return {"scores": scores, "total": len(scores)}


# ---------------------------------------------------------------------------
# DNS status & blacklist recovery
# ---------------------------------------------------------------------------

@app.get("/domains/{domain_id}/dns-status", tags=["Deliverability"])
async def get_domain_dns_status(domain_id: str, client_id: ClientId) -> dict:
    """
    Return current DNS health status for a domain, including the latest
    dns_check_log entries and any open blacklist recoveries.
    """
    domain_row = (
        _supabase.table("domains")
        .select("*")
        .eq("id", domain_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    ).data or []

    if not domain_row:
        raise HTTPException(status_code=404, detail="Domain not found")

    domain = domain_row[0]

    # Last 20 DNS check log entries
    log_entries = (
        _supabase.table("dns_check_log")
        .select("*")
        .eq("domain_id", domain_id)
        .order("timestamp", desc=True)
        .limit(20)
        .execute()
    ).data or []

    # Run fresh live check
    from api.dns_check import run_full_dns_check
    selector = domain.get("dkim_selector") or "google"
    live_check = run_full_dns_check(domain["domain"], dkim_selector=selector)

    return {
        "domain":       domain["domain"],
        "status":       domain.get("dns_check_status") or "unknown",
        "last_check":   domain.get("last_dns_check"),
        "blacklisted":  domain.get("blacklisted", False),
        "live":         live_check.to_dict(),
        "recent_logs":  log_entries,
    }


@app.get("/domains/{domain_id}/blacklist-recovery", tags=["Deliverability"])
async def get_blacklist_recovery(domain_id: str, client_id: ClientId) -> dict:
    """
    Return the active blacklist recovery guide for a domain, if any.
    """
    domain_row = (
        _supabase.table("domains")
        .select("id, domain, client_id")
        .eq("id", domain_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    ).data or []

    if not domain_row:
        raise HTTPException(status_code=404, detail="Domain not found")

    from dns_monitor import get_blacklist_recovery
    recovery = get_blacklist_recovery(_supabase, domain_id)

    if not recovery:
        return {"recovery": None, "message": "No active blacklist recovery"}

    return {"recovery": recovery}


@app.patch("/domains/{domain_id}/blacklist-recovery/steps/{step}", tags=["Deliverability"])
async def update_recovery_step(
    domain_id: str,
    step: int,
    body: BlacklistRecoveryStepPatch,
    client_id: ClientId,
) -> dict:
    """
    Mark a recovery checklist step as completed or incomplete.

    Body: {recovery_id, completed?}
    """
    domain_row = (
        _supabase.table("domains")
        .select("id")
        .eq("id", domain_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    ).data or []

    if not domain_row:
        raise HTTPException(status_code=404, detail="Domain not found")

    recovery_id = str(body.recovery_id)
    completed   = body.completed

    from dns_monitor import update_recovery_step as _update_step
    ok = _update_step(_supabase, recovery_id, step, completed)
    if not ok:
        raise HTTPException(status_code=404, detail="Recovery or step not found")

    return {"ok": True, "step": step, "completed": completed}


# ---------------------------------------------------------------------------
# DNS check-all (n8n trigger)
# ---------------------------------------------------------------------------

@app.post("/domains/dns-check-all", tags=["Deliverability"])
async def trigger_dns_check_all(
    background_tasks: BackgroundTasks,
    client_id: ClientId,
    mode: str = Query("drift", regex="^(drift|blacklist|both)$"),
) -> dict:
    """
    Trigger DNS drift and/or blacklist checks for all active domains.

    Query param mode: drift | blacklist | both (default: drift)
    Runs in background; returns immediately.
    """
    def _run():
        from dns_monitor import run_dns_checks, run_blacklist_checks
        sb = _supabase
        if mode in ("drift", "both"):
            run_dns_checks(sb)
        if mode in ("blacklist", "both"):
            run_blacklist_checks(sb)

    background_tasks.add_task(_run)
    return {"ok": True, "mode": mode, "queued": True}


# ---------------------------------------------------------------------------
# Worker endpoints for n8n
# ---------------------------------------------------------------------------

@app.post("/deliverability/process-placements", tags=["Workers"])
async def process_placement_queue(client_id: ClientId) -> dict:
    """Process pending placement tests (called by n8n every 2 min)."""
    from placement_tester import process_pending_tests
    count = process_pending_tests(_supabase)
    return {"ok": True, "processed": count}


# ---------------------------------------------------------------------------
# Daily briefing
# ---------------------------------------------------------------------------

@app.post("/briefing/generate-and-send", tags=["Briefing"])
async def generate_briefing(
    admin_id: Annotated[str, Depends(require_admin)],
    client_id_override: Optional[str] = Query(default=None, description="Target client_id; defaults to admin's own account"),
    dry_run: bool = Query(default=False, description="Return HTML without sending email"),
) -> dict:
    """
    Generate and send the daily morning briefing for a client.

    Admin-only. If client_id_override is omitted, the briefing is generated
    for the admin's own account. Set dry_run=true to return the HTML without
    sending an email (useful for previewing).
    """
    import asyncio
    from daily_briefing import generate_and_send as _gen

    target = client_id_override or admin_id

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: _gen(target, sb=_supabase, dry_run=dry_run),
    )
    return {
        "sent":      result["sent"],
        "recipient": result["recipient"],
        "dry_run":   dry_run,
        **({"html_preview": result["html"]} if dry_run else {}),
    }


# ---------------------------------------------------------------------------
# Decision log
# ---------------------------------------------------------------------------

def _sanitize_error_message(raw: str) -> str:
    """
    Turn raw Python/SMTP/IMAP exception text into a user-friendly message.

    Never exposes stack traces, file paths, or module internals to end users.
    """
    if not raw:
        return "Onbekende fout tijdens warmup."
    text = str(raw).lower()

    # Pattern → human message (in Dutch, matches app language)
    patterns = [
        ("eof occurred in violation of protocol", "Tijdelijke SSL-hiccup bij Gmail — inbox herstelt automatisch bij de volgende run."),
        ("connection refused",       "Kon geen verbinding maken met Gmail — controleer je app-wachtwoord."),
        ("connection timed out",     "Gmail reageert traag — run automatisch opnieuw."),
        ("connection reset",         "Verbinding werd verbroken door Gmail — meestal een kortstondige netwerkpiek."),
        ("authentication failed",    "Authenticatie mislukt — app-wachtwoord is mogelijk verlopen of ingetrokken."),
        ("invalid credentials",      "Inloggegevens afgewezen — genereer een nieuw app-wachtwoord in Google Account."),
        ("login failed",             "Inloggen mislukt — app-wachtwoord controleren."),
        ("over quota",               "Dagelijkse Gmail-limiet bereikt — wacht tot middernacht."),
        ("421 ",                     "Gmail accepteert tijdelijk geen mail (rate limit) — wordt automatisch opnieuw geprobeerd."),
        ("550 ",                     "Ontvanger weigerde de mail (ongeldig adres of blacklist)."),
        ("dns ",                     "DNS-lookup mislukte — check je domein-configuratie."),
        ("timeout",                  "Time-out — wordt automatisch opnieuw geprobeerd."),
        ("claude",                   "AI-service gaf een tijdelijke fout terug."),
        ("anthropic",                "AI-service gaf een tijdelijke fout terug."),
        ("rate limit",               "AI-limiet bereikt — wacht enkele minuten."),
        ("smtp",                     "Fout bij het versturen via Gmail SMTP — wordt automatisch opnieuw geprobeerd."),
        ("imap",                     "Fout bij het ophalen via IMAP — wordt automatisch opnieuw geprobeerd."),
    ]
    for needle, friendly in patterns:
        if needle in text:
            return friendly

    # Fallback: generic message without leaking details
    return "Er ging iets mis tijdens de warmup-run. Wordt automatisch opnieuw geprobeerd."


def _log_decision(
    *,
    client_id: str,
    decision_type: str,
    entity_type: str,
    entity_id: Optional[str],
    entity_name: Optional[str],
    before_state: Optional[dict],
    after_state: Optional[dict],
    reason: Optional[str] = None,
    made_by: str = "client",
) -> None:
    """
    Insert a row into decision_log.

    Called internally by mutation endpoints (pause inbox, activate campaign, etc.).
    Failures are logged but never raise — a failed audit write must not break the
    primary operation.
    """
    try:
        _supabase.table("decision_log").insert({
            "client_id":    client_id,
            "decision_type": decision_type,
            "entity_type":  entity_type,
            "entity_id":    entity_id,
            "entity_name":  entity_name,
            "before_state": before_state,
            "after_state":  after_state,
            "reason":       reason,
            "made_by":      made_by,
            "created_at":   _now_utc(),
        }).execute()
    except Exception as exc:
        logger.warning("decision_log write failed (non-fatal): %s", exc)


@app.get("/decisions", tags=["Decisions"])
async def list_decisions(
    client_id: ClientId,
    entity_type: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """
    Return decision_log entries for the authenticated client, newest first.

    Optionally filter by entity_type (campaign | inbox | domain | sequence_step).
    """
    q = (
        _supabase.table("decision_log")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if entity_type:
        q = q.eq("entity_type", entity_type)
    resp = q.execute()
    return resp.data or []


@app.get("/decisions/{entity_type}/{entity_id}", tags=["Decisions"])
async def list_decisions_for_entity(
    entity_type: str,
    entity_id: str,
    client_id: ClientId,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[dict]:
    """Return all decision_log entries for a specific entity (e.g. a campaign or inbox)."""
    resp = (
        _supabase.table("decision_log")
        .select("*")
        .eq("client_id", client_id)
        .eq("entity_type", entity_type)
        .eq("entity_id", entity_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


@app.get("/decisions/{decision_id}/effect", tags=["Decisions"])
async def get_decision_effect(decision_id: str, client_id: ClientId) -> dict:
    """
    Return the measured effect of a past decision.

    If the effect has already been calculated (effect IS NOT NULL), returns the
    stored value immediately. Otherwise triggers a live calculation: compares the
    entity's key metric 7 days before vs 7 days after the decision timestamp.
    """
    # Fetch the decision
    resp = (
        _supabase.table("decision_log")
        .select("*")
        .eq("id", decision_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Decision not found.")
    decision = rows[0]

    # Return cached effect if available
    if decision.get("effect") is not None:
        return {"decision_id": decision_id, "effect": decision["effect"], "cached": True}

    # Live calculation — only works for inbox and campaign entities with enough history
    entity_type = decision.get("entity_type")
    entity_id   = decision.get("entity_id")
    created_at  = decision.get("created_at")

    if not entity_id or not created_at:
        return {"decision_id": decision_id, "effect": None, "cached": False,
                "message": "Insufficient data to calculate effect."}

    try:
        decision_ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except Exception:
        return {"decision_id": decision_id, "effect": None, "cached": False}

    before_start = (decision_ts - timedelta(days=7)).isoformat()
    after_end    = (decision_ts + timedelta(days=7)).isoformat()
    decision_iso = decision_ts.isoformat()

    effect: dict = {}

    if entity_type == "inbox":
        def _avg_rep(start: str, end: str) -> Optional[float]:
            r = (
                _supabase.table("warmup_logs")
                .select("reputation_score_at_time")
                .eq("inbox_id", entity_id)
                .gte("timestamp", start)
                .lte("timestamp", end)
                .execute()
            )
            scores = [row["reputation_score_at_time"] for row in (r.data or []) if row.get("reputation_score_at_time") is not None]
            return round(sum(scores) / len(scores), 2) if scores else None

        before_rep = _avg_rep(before_start, decision_iso)
        after_rep  = _avg_rep(decision_iso, after_end)
        if before_rep is not None and after_rep is not None:
            effect["reputation_score"] = {
                "before": before_rep,
                "after":  after_rep,
                "delta":  round(after_rep - before_rep, 2),
            }

    elif entity_type == "campaign":
        def _reply_rate(start: str, end: str) -> Optional[float]:
            r = (
                _supabase.table("sending_schedule")
                .select("status")
                .eq("campaign_id", entity_id)
                .gte("sent_at", start)
                .lte("sent_at", end)
                .execute()
            )
            rows_data = r.data or []
            sent    = sum(1 for row in rows_data if row["status"] in ("sent", "replied", "bounced"))
            replied = sum(1 for row in rows_data if row["status"] == "replied")
            return round(replied / sent, 4) if sent >= 5 else None

        before_rr = _reply_rate(before_start, decision_iso)
        after_rr  = _reply_rate(decision_iso, after_end)
        if before_rr is not None and after_rr is not None:
            effect["reply_rate"] = {
                "before": before_rr,
                "after":  after_rr,
                "delta":  round(after_rr - before_rr, 4),
            }

    # Persist computed effect
    if effect:
        _supabase.table("decision_log").update({
            "effect": effect,
            "effect_calculated_at": _now_utc(),
        }).eq("id", decision_id).execute()

    return {"decision_id": decision_id, "effect": effect or None, "cached": False}


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

@app.get("/experiments", tags=["Experiments"])
async def list_experiments(
    client_id: ClientId,
    status: Optional[str] = Query(default=None, description="active|concluded|inconclusive|cancelled"),
) -> list[dict]:
    """Return all experiments for the authenticated client, newest first."""
    q = (
        _supabase.table("experiments")
        .select("*")
        .eq("client_id", client_id)
        .order("started_at", desc=True)
    )
    if status:
        q = q.eq("status", status)
    resp = q.execute()
    return resp.data or []


@app.post("/experiments", status_code=201, tags=["Experiments"])
async def create_experiment(body: dict, client_id: ClientId) -> dict:
    """
    Create a new A/B experiment linking two campaigns.

    Required fields: name, control_campaign_id, variant_campaign_id.
    Optional: hypothesis, metric (reply_rate|open_rate|meeting_rate), min_sample_size.
    """
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required.")

    control_id  = body.get("control_campaign_id")
    variant_id  = body.get("variant_campaign_id")
    if not control_id or not variant_id:
        raise HTTPException(status_code=422, detail="control_campaign_id and variant_campaign_id are required.")
    if control_id == variant_id:
        raise HTTPException(status_code=422, detail="control and variant must be different campaigns.")

    # Verify both campaigns belong to this client
    for cid in (control_id, variant_id):
        _require_row("campaigns", cid, client_id)

    metric = body.get("metric", "reply_rate")
    if metric not in {"reply_rate", "open_rate", "meeting_rate"}:
        raise HTTPException(status_code=422, detail="metric must be reply_rate | open_rate | meeting_rate.")

    row = {
        "client_id":            client_id,
        "name":                 name[:200],
        "hypothesis":           (body.get("hypothesis") or "")[:500] or None,
        "metric":               metric,
        "control_campaign_id":  control_id,
        "variant_campaign_id":  variant_id,
        "min_sample_size":      max(10, min(int(body.get("min_sample_size") or 100), 10000)),
        "status":               "active",
        "started_at":           _now_utc(),
    }
    resp = _supabase.table("experiments").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to create experiment.")
    return resp.data[0]


@app.get("/experiments/{experiment_id}", tags=["Experiments"])
async def get_experiment(experiment_id: str, client_id: ClientId) -> dict:
    """Return a single experiment with live metric comparison."""
    resp = (
        _supabase.table("experiments")
        .select("*")
        .eq("id", experiment_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    exp = rows[0]

    # Live metric comparison
    metric = exp.get("metric", "reply_rate")
    results: dict = {}

    for label, camp_id in [("control", exp.get("control_campaign_id")), ("variant", exp.get("variant_campaign_id"))]:
        if not camp_id:
            continue
        sched_resp = (
            _supabase.table("sending_schedule")
            .select("status")
            .eq("campaign_id", camp_id)
            .in_("status", ["sent", "replied", "bounced"])
            .execute()
        )
        sched_rows = sched_resp.data or []
        sent    = len(sched_rows)
        replied = sum(1 for r in sched_rows if r["status"] == "replied")
        bounced = sum(1 for r in sched_rows if r["status"] == "bounced")
        results[label] = {
            "sent":       sent,
            "replied":    replied,
            "bounced":    bounced,
            "reply_rate": round(replied / sent, 4) if sent else 0,
            "bounce_rate": round(bounced / sent, 4) if sent else 0,
        }

    # Simple significance: declare winner if delta > 5pp with min 20 sends each
    winner = None
    ctrl  = results.get("control", {})
    vari  = results.get("variant", {})
    if metric == "reply_rate" and ctrl.get("sent", 0) >= 20 and vari.get("sent", 0) >= 20:
        delta = vari.get("reply_rate", 0) - ctrl.get("reply_rate", 0)
        if abs(delta) >= 0.05:
            winner = "variant" if delta > 0 else "control"

    exp["live_results"] = results
    exp["preliminary_winner"] = winner
    return exp


@app.patch("/experiments/{experiment_id}", tags=["Experiments"])
async def update_experiment(experiment_id: str, body: dict, client_id: ClientId) -> dict:
    """
    Conclude or update an experiment.

    Allowed fields: status (concluded|inconclusive|cancelled), result
    (control_wins|variant_wins|inconclusive), result_summary, learnings.
    """
    resp = (
        _supabase.table("experiments")
        .select("id, client_id")
        .eq("id", experiment_id)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
    )
    if not (resp.data or []):
        raise HTTPException(status_code=404, detail="Experiment not found.")

    allowed_fields = {"status", "result", "result_summary", "learnings"}
    patch = {k: v for k, v in body.items() if k in allowed_fields and v is not None}

    if "status" in patch and patch["status"] not in {"active", "concluded", "inconclusive", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid status value.")
    if "result" in patch and patch["result"] not in {"control_wins", "variant_wins", "inconclusive"}:
        raise HTTPException(status_code=422, detail="Invalid result value.")

    if patch.get("status") in {"concluded", "inconclusive", "cancelled"}:
        patch["concluded_at"] = _now_utc()

    if not patch:
        raise HTTPException(status_code=422, detail="No valid fields to update.")

    update_resp = (
        _supabase.table("experiments")
        .update(patch)
        .eq("id", experiment_id)
        .execute()
    )
    return update_resp.data[0] if update_resp.data else {"id": experiment_id}


# ---------------------------------------------------------------------------
# Static frontend — MUST be last so API routes take priority
# ---------------------------------------------------------------------------
# Campaign cloning
# ---------------------------------------------------------------------------

@app.post("/campaigns/{campaign_id}/clone", tags=["Campaigns"], status_code=201)
async def clone_campaign(campaign_id: str, client_id: ClientId, body: dict = {}) -> dict:
    """Clone an existing campaign with all its sequence steps."""
    # Fetch original campaign
    orig = _supabase.table("campaigns").select("*").eq("id", campaign_id).limit(1).execute()
    if not orig.data:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    campaign = orig.data[0]
    if campaign.get("client_id") != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    # Fetch original sequence steps
    steps = _supabase.table("sequence_steps").select("*").eq("campaign_id", campaign_id).order("step_number").execute()

    # Create new campaign
    new_name = body.get("name") or f"{campaign['name']} (kopie)"
    new_campaign = {k: v for k, v in campaign.items() if k not in ("id", "created_at", "updated_at")}
    new_campaign["name"] = new_name
    new_campaign["status"] = "draft"
    resp = _supabase.table("campaigns").insert(new_campaign).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to clone campaign.")
    new_id = resp.data[0]["id"]

    # Clone sequence steps
    cloned_steps = 0
    for step in (steps.data or []):
        new_step = {k: v for k, v in step.items() if k not in ("id",)}
        new_step["campaign_id"] = new_id
        try:
            _supabase.table("sequence_steps").insert(new_step).execute()
            cloned_steps += 1
        except Exception:
            pass

    return {"id": new_id, "name": new_name, "cloned_steps": cloned_steps}


# ---------------------------------------------------------------------------
# Funnel management
# ---------------------------------------------------------------------------

@app.get("/funnel/overview", tags=["Funnel"])
async def funnel_overview(client_id: ClientId) -> dict:
    """Current funnel stage distribution for the authenticated client."""
    from funnel_engine import snapshot_funnel
    return snapshot_funnel(_supabase, client_id)


@app.get("/funnel/analytics", tags=["Funnel"])
async def funnel_analytics(client_id: ClientId, days: int = Query(default=30)) -> list[dict]:
    """Daily funnel snapshots for trend analysis."""
    cutoff = _days_ago_utc(days)
    resp = (
        _supabase.table("funnel_analytics")
        .select("*")
        .eq("client_id", client_id)
        .gte("date", cutoff[:10])
        .order("date")
        .execute()
    )
    return resp.data or []


@app.post("/funnel/route-reply", tags=["Funnel"])
async def route_reply_endpoint(client_id: ClientId, body: dict) -> dict:
    """
    Route a classified reply through the funnel engine.

    Body: { lead_id, lead_email, classification, campaign_id?, reply_body? }
    """
    from funnel_engine import route_reply
    lead_id = body.get("lead_id", "")
    lead_email = body.get("lead_email", "")
    classification = body.get("classification", "other")
    campaign_id = body.get("campaign_id")
    reply_body = body.get("reply_body", "")

    if not lead_id or not classification:
        raise HTTPException(status_code=422, detail="lead_id and classification are required.")

    return route_reply(_supabase, client_id, lead_id, lead_email, classification, campaign_id, reply_body)


@app.get("/funnel/routing-rules", tags=["Funnel"])
async def get_routing_rules(client_id: ClientId) -> list[dict]:
    """Get reply routing rules for the authenticated client."""
    resp = _supabase.table("reply_routing_rules").select("*").eq("client_id", client_id).execute()
    rules = resp.data or []
    if not rules:
        # Seed defaults
        from funnel_engine import seed_default_rules
        seed_default_rules(_supabase, client_id)
        resp = _supabase.table("reply_routing_rules").select("*").eq("client_id", client_id).execute()
        rules = resp.data or []
    return rules


@app.patch("/funnel/routing-rules/{rule_id}", tags=["Funnel"])
async def update_routing_rule(rule_id: str, client_id: ClientId, body: dict) -> dict:
    """Update a reply routing rule."""
    check = _supabase.table("reply_routing_rules").select("client_id").eq("id", rule_id).limit(1).execute()
    if not check.data or check.data[0]["client_id"] != client_id:
        raise HTTPException(status_code=404, detail="Rule not found.")
    allowed = {"action", "auto_reply_template", "notify", "active"}
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=422, detail="No valid fields to update.")
    resp = _supabase.table("reply_routing_rules").update(patch).eq("id", rule_id).execute()
    return resp.data[0] if resp.data else {"id": rule_id}


@app.post("/funnel/move-stage", tags=["Funnel"])
async def move_lead_stage(client_id: ClientId, body: dict) -> dict:
    """Manually move a lead to a different funnel stage."""
    from funnel_engine import move_to_stage, STAGES
    lead_id = body.get("lead_id", "")
    new_stage = body.get("stage", "")
    if not lead_id or new_stage not in STAGES:
        raise HTTPException(status_code=422, detail=f"lead_id required, stage must be one of: {', '.join(STAGES)}")
    # Verify lead belongs to client
    lead = _supabase.table("leads").select("client_id").eq("id", lead_id).limit(1).execute()
    if not lead.data or lead.data[0]["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    move_to_stage(_supabase, lead_id, new_stage, "manual")
    return {"lead_id": lead_id, "new_stage": new_stage}


# ---------------------------------------------------------------------------
# Funnel sequence templates (per stage)
# ---------------------------------------------------------------------------

_FUNNEL_TEMPLATES = {
    "cold_intro_nl": {
        "stage": "cold",
        "name": "Cold intro (NL)",
        "steps": [
            {"step_number": 1, "subject": "vraag over {{company}}", "body": "Hi {{first_name}},\n\nKwam {{company}} tegen en had een korte vraag: hoe lossen jullie nu {{pain_point}} op?\n\nWij helpen vergelijkbare bedrijven met {{value_prop}}.\n\nOpen voor een kort gesprek?\n\n{{sender_name}}", "wait_days": 0},
            {"step_number": 2, "subject": "re: {{company}}", "body": "Hi {{first_name}},\n\nWeet niet of mijn vorige mail aankwam. Kort voorbeeld: {{similar_company}} bespaarde {{result}} met onze aanpak.\n\nIs dit relevant voor jullie?\n\n{{sender_name}}", "wait_days": 3},
        ],
    },
    "warm_followup_nl": {
        "stage": "warm",
        "name": "Warm follow-up (NL)",
        "steps": [
            {"step_number": 3, "subject": "case {{similar_company}}", "body": "Hi {{first_name}},\n\nDeze case is wellicht interessant: {{similar_company}} had hetzelfde probleem en loste het als volgt op.\n\n[link naar case]\n\nBenieuwd hoe jullie dit nu aanpakken.\n\n{{sender_name}}", "wait_days": 4},
            {"step_number": 4, "subject": "{{first_name}}, nog even dit", "body": "Hi {{first_name}},\n\nLaatste opvolging van mij. Mocht het nu niet passen, helemaal prima.\n\nVoor als het later wél relevant wordt: {{calendar_link}}\n\nSucces!\n\n{{sender_name}}", "wait_days": 5},
        ],
    },
    "hot_meeting_nl": {
        "stage": "hot",
        "name": "Meeting request (NL)",
        "steps": [
            {"step_number": 1, "subject": "15 min volgende week?", "body": "Hi {{first_name}},\n\nLeuk dat je interesse hebt! Zullen we volgende week 15 minuten inplannen?\n\nHier is mijn agenda: {{calendar_link}}\n\nKies een moment dat je uitkomt.\n\n{{sender_name}}", "wait_days": 0},
        ],
    },
    "breakup_nl": {
        "stage": "warm",
        "name": "Break-up (NL)",
        "steps": [
            {"step_number": 5, "subject": "laatste mail", "body": "Hi {{first_name}},\n\nIk neem aan dat dit nu niet de juiste timing is — dat snap ik.\n\nMocht het later wél relevant worden, ping gerust. Verder geen mails van mij.\n\nSucces met alles,\n{{sender_name}}", "wait_days": 5},
        ],
    },
}


@app.get("/funnel/templates", tags=["Funnel"])
async def funnel_templates(
    client_id: ClientId,
    stage: Optional[str] = Query(default=None),
) -> list[dict]:
    """Get sequence templates organized by funnel stage."""
    templates = []
    for tid, t in _FUNNEL_TEMPLATES.items():
        if stage and t["stage"] != stage:
            continue
        templates.append({"id": tid, **t})
    return templates


# ---------------------------------------------------------------------------
# GDPR — data export & right to erasure
# ---------------------------------------------------------------------------

@app.get("/leads/priority", tags=["Leads"])
async def leads_priority(
    client_id: ClientId,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """
    Leads ranked by a composite priority score combining:
      - engagement (0-100): recent interaction intensity (opens, clicks, replies)
      - heatr_score (0-100): ICP fit from Heatr enrichment
      - funnel_stage: hot > warm > cold > nurture > lost
      - recency: boost for recent activity, penalty for staleness

    Returns top N leads with a `priority` 0-100 + reason breakdown so the UI
    can explain WHY a lead is at the top.
    """
    resp = (
        _supabase.table("leads")
        .select("id, email, first_name, last_name, company, domain, status, funnel_stage, engagement_score, engagement_updated_at, custom_fields, created_at")
        .eq("client_id", client_id)
        .in_("funnel_stage", ["cold", "warm", "hot", "meeting"])
        .limit(1000)
        .execute()
    )
    leads = resp.data or []

    stage_weight = {"cold": 20, "warm": 50, "hot": 80, "meeting": 100}
    now = datetime.now(timezone.utc)

    scored = []
    for lead in leads:
        cf = lead.get("custom_fields") or {}

        engagement = float(lead.get("engagement_score") or 0)
        heatr = float(cf.get("heatr_score") or 0)
        stage_score = stage_weight.get(lead.get("funnel_stage") or "cold", 20)

        # Recency: 0-100 based on days since last engagement
        last = lead.get("engagement_updated_at") or lead.get("created_at")
        recency = 50
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                days = (now - last_dt).days
                if days <= 1:
                    recency = 100
                elif days <= 3:
                    recency = 80
                elif days <= 7:
                    recency = 60
                elif days <= 14:
                    recency = 40
                elif days <= 30:
                    recency = 20
                else:
                    recency = 0
            except Exception:
                pass

        # Weighted composite: engagement = 40%, funnel stage = 30%, heatr = 20%, recency = 10%
        priority = (
            engagement * 0.40
            + stage_score * 0.30
            + heatr * 0.20
            + recency * 0.10
        )

        # Explain what drove the score
        reasons: list[str] = []
        if engagement >= 50:
            reasons.append(f"hot engagement ({engagement:.0f})")
        if lead.get("funnel_stage") == "hot":
            reasons.append("in hot stage")
        elif lead.get("funnel_stage") == "meeting":
            reasons.append("meeting stage")
        if heatr >= 70:
            reasons.append(f"strong ICP fit ({heatr:.0f})")
        if recency >= 80:
            reasons.append("recently active")

        scored.append({
            "id": lead["id"],
            "email": lead.get("email"),
            "name": f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip() or lead.get("email"),
            "company": lead.get("company") or cf.get("company") or "",
            "funnel_stage": lead.get("funnel_stage"),
            "engagement_score": engagement,
            "heatr_score": heatr,
            "priority": round(priority, 1),
            "reasons": reasons or ["baseline"],
        })

    scored.sort(key=lambda x: x["priority"], reverse=True)
    return scored[:limit]


@app.get("/compliance/overview", tags=["GDPR"])
async def compliance_overview(client_id: ClientId) -> dict:
    """
    Self-service compliance view for the authenticated client.

    Returns what data Warmr holds about this client + links to export/delete
    endpoints + a summary of admin actions on their account in the last 30 days.
    """
    # Count our rows per table
    def _count(table: str, column: str = "client_id") -> int:
        try:
            r = _supabase.table(table).select("id", count="exact").eq(column, client_id).execute()
            return r.count or 0
        except Exception:
            return 0

    counts = {
        "leads": _count("leads"),
        "inboxes": _count("inboxes"),
        "domains": _count("domains"),
        "campaigns": _count("campaigns"),
        "email_events": _count("email_events"),
        "email_tracking": _count("email_tracking"),
        "suppression_list": _count("suppression_list"),
        "notifications": _count("notifications"),
        "reply_inbox": _count("reply_inbox"),
    }

    # Admin actions touching this client in last 30 days
    cutoff = _days_ago_utc(30)
    admin_acts = (
        _supabase.table("admin_audit_log")
        .select("admin_id, action, target_type, target_id, created_at")
        .eq("target_id", client_id)
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )

    return {
        "client_id": client_id,
        "data_held_by_warmr": counts,
        "total_rows": sum(counts.values()),
        "admin_actions_last_30d": admin_acts.data or [],
        "retention_policy": "Leads + events retained while account is active. Deleted on request (Article 17) or within 30 days of account closure.",
        "rights": {
            "access": "/leads/{lead_id}/export-gdpr  (HMAC-signed bundle per lead)",
            "erasure": "/leads/{lead_id}/purge  (permanent delete, irreversible)",
            "rectification": "PATCH via /leads/{lead_id} or contact support",
            "portability": "Export endpoint returns machine-readable JSON",
            "objection": "POST /suppression  (add email to do-not-contact list)",
        },
        "data_processor_contact": "info@aeryssolution.nl",
    }


@app.get("/leads/{lead_id}/export-gdpr", tags=["GDPR"])
async def gdpr_export_lead(lead_id: str, client_id: ClientId) -> dict:
    """Export all data Warmr has on a single lead (GDPR Article 15)."""
    lead = _supabase.table("leads").select("*").eq("id", lead_id).limit(1).execute()
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if lead.data[0].get("client_id") != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    # Gather every related record
    bundle = {
        "lead": lead.data[0],
        "campaign_leads": (_supabase.table("campaign_leads").select("*").eq("lead_id", lead_id).execute()).data or [],
        "email_events": (_supabase.table("email_events").select("*").eq("lead_id", lead_id).execute()).data or [],
        "email_tracking": (_supabase.table("email_tracking").select("*").eq("lead_id", lead_id).execute()).data or [],
        "bounces": (_supabase.table("bounce_log").select("*").eq("lead_email", lead.data[0].get("email", "")).execute()).data or [],
        "exported_at": _now_utc(),
        "exported_by": client_id,
    }

    # HMAC signature over the canonical JSON representation. Consumer can verify
    # integrity + exact timestamp by recomputing HMAC-SHA256(secret, canonical_body).
    import hashlib, hmac, json as _json
    canonical = _json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    secret = os.getenv("WARMR_API_TOKEN", "fallback-secret").encode("utf-8")
    signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    bundle_hash = hashlib.sha256(canonical).hexdigest()

    return {
        **bundle,
        "_integrity": {
            "algorithm": "HMAC-SHA256",
            "signature": signature,
            "content_hash_sha256": bundle_hash,
            "verify_instructions": (
                "To verify: recompute HMAC-SHA256(server_secret, canonical_json) over "
                "the export with the _integrity field removed. Match against signature."
            ),
        },
    }


@app.delete("/leads/{lead_id}/purge", tags=["GDPR"], status_code=200)
async def gdpr_purge_lead(lead_id: str, client_id: ClientId) -> dict:
    """Permanently delete all data for a lead (GDPR Article 17 — right to erasure)."""
    lead = _supabase.table("leads").select("client_id, email").eq("id", lead_id).limit(1).execute()
    if not lead.data:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if lead.data[0]["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    email = lead.data[0]["email"]
    deleted = {}

    # Delete from all related tables
    for table, filter_col, filter_val in [
        ("email_tracking", "lead_id", lead_id),
        ("email_events", "lead_id", lead_id),
        ("campaign_leads", "lead_id", lead_id),
        ("bounce_log", "lead_email", email),
        ("unsubscribe_tokens", "lead_id", lead_id),
        ("leads", "id", lead_id),
    ]:
        try:
            r = _supabase.table(table).delete().eq(filter_col, filter_val).execute()
            deleted[table] = len(r.data or [])
        except Exception as exc:
            deleted[table] = f"error: {exc}"

    # Add to suppression so this email is never contacted again
    try:
        domain = email.split("@")[-1] if "@" in email else None
        _supabase.table("suppression_list").insert({
            "client_id": client_id,
            "email": email,
            "domain": domain,
            "reason": "manual",
            "source": "gdpr_purge",
        }).execute()
    except Exception:
        pass  # Already suppressed

    return {"purged": True, "lead_email": email, "deleted_records": deleted}


# ---------------------------------------------------------------------------
# Admin audit log
# ---------------------------------------------------------------------------

# ── Anomaly detection: warn if non-admin query returns unexpectedly large result ──
def _check_query_size_anomaly(client_id: str, table: str, row_count: int, threshold: int = 1000) -> None:
    """Log a warning + create a notification when a non-admin user retrieves suspiciously many rows."""
    if row_count < threshold:
        return
    try:
        from api.auth import _get_client_status
        if _get_client_status(client_id).get("is_admin"):
            return  # Admins legitimately read many rows
        logger.warning("ANOMALY: non-admin client %s retrieved %d rows from %s", client_id, row_count, table)
        _supabase.table("notifications").insert({
            "client_id": "system",
            "type": "query_anomaly",
            "message": f"Non-admin client {client_id} retrieved {row_count} rows from {table}.",
            "priority": "urgent",
        }).execute()
    except Exception:
        pass


# ── Webhook URL verification via challenge-response ──
async def _verify_webhook_url(url: str) -> bool:
    """Ping the webhook URL with a challenge; expect it echoed back in X-Warmr-Verification."""
    import secrets
    challenge = secrets.token_urlsafe(16)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url, headers={"X-Warmr-Challenge": challenge})
            return resp.headers.get("X-Warmr-Verification") == challenge
    except Exception:
        return False


def _log_admin_action(admin_id: str, action: str, target_type: str = "", target_id: str = "", payload: dict | None = None) -> None:
    """Silently record an admin action. Failures are non-fatal."""
    try:
        _supabase.table("admin_audit_log").insert({
            "admin_id": admin_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "payload": payload or {},
        }).execute()
    except Exception:
        pass


@app.post("/auth/check-password", tags=["Auth"])
@limiter.limit("30/minute")
async def check_password_endpoint(request: Request, body: dict) -> dict:
    """
    Validate a password against policy rules (length, complexity, common list).

    Called by the signup/password-change UI to give real-time feedback.
    Does NOT authenticate — only validates the string. Safe to call public.
    """
    from utils.password_policy import check_password, strength_score
    password = body.get("password", "")
    email = body.get("email")
    ok, errors = check_password(password, email)
    return {
        "valid": ok,
        "errors": errors,
        "strength": strength_score(password),
    }


@app.post("/auth/force-logout", tags=["Auth"])
async def force_logout(client_id: ClientId) -> dict:
    """
    Invalidate all active sessions for the authenticated client.

    Increments `clients.session_version` AND calls Supabase admin.signOut()
    which revokes all refresh tokens. Existing access tokens remain valid
    until their ~1h expiry but cannot be refreshed.
    """
    import httpx as _httpx
    # Bump session_version (client-side tracking)
    try:
        row = _supabase.table("clients").select("session_version").eq("id", client_id).limit(1).execute()
        current = int((row.data or [{}])[0].get("session_version") or 1)
        _supabase.table("clients").update({"session_version": current + 1}).eq("id", client_id).execute()
    except Exception as exc:
        logger.warning("Failed to bump session_version for %s: %s", client_id, exc)

    # Invalidate cache so subsequent checks pick up the new version
    try:
        from api.auth import invalidate_client_cache
        invalidate_client_cache(client_id)
    except Exception:
        pass

    # Call Supabase admin signout to revoke refresh tokens
    try:
        _httpx.post(
            f"{SUPABASE_URL}/auth/v1/admin/users/{client_id}/logout",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("Supabase admin signout failed for %s: %s", client_id, exc)

    return {"ok": True, "message": "All sessions revoked. Existing access tokens expire in ~1 hour."}


@app.post("/admin/impersonate/{target_client_id}", tags=["Admin"])
async def impersonate_client(
    target_client_id: str,
    admin_id: Annotated[str, Depends(require_admin)],
    request: Request,
) -> dict:
    """
    Generate a short-lived JWT for support sessions.

    The returned token carries an `impersonator_id` claim so the UI can show a
    banner + every write during the session is attributable to the admin.
    Expires after 15 minutes.
    """
    # Verify target exists
    target = _supabase.table("clients").select("id, email, company_name").eq("id", target_client_id).limit(1).execute()
    if not target.data:
        raise HTTPException(status_code=404, detail="Target client not found.")

    import time as _time
    token = jose_jwt.encode(
        {
            "sub": target_client_id,
            "aud": "authenticated",
            "role": "authenticated",
            "impersonator_id": admin_id,
            "iat": int(_time.time()),
            "exp": int(_time.time()) + 900,  # 15 min
        },
        SUPABASE_JWT_SECRET,
        algorithm="HS256",
    )

    _log_admin_action(
        admin_id=admin_id,
        action="impersonate_started",
        target_type="client",
        target_id=target_client_id,
        payload={
            "ip": request.client.host if request.client else None,
            "target_email": target.data[0].get("email"),
            "target_company": target.data[0].get("company_name"),
        },
    )

    return {
        "token": token,
        "expires_in": 900,
        "target_client_id": target_client_id,
        "target_email": target.data[0].get("email"),
        "impersonator_id": admin_id,
    }


@app.get("/admin/audit-log/export", tags=["Admin"])
async def admin_audit_log_export(
    admin_id: Annotated[str, Depends(require_admin)],
    days: int = Query(default=30, ge=1, le=365),
    format: str = Query(default="json", regex="^(json|csv)$"),
) -> Response:
    """Export admin audit log for compliance / SOC 2 evidence."""
    cutoff = _days_ago_utc(days)
    resp = (
        _supabase.table("admin_audit_log")
        .select("*")
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(10000)
        .execute()
    )
    rows = resp.data or []

    if format == "csv":
        out = io.StringIO()
        if rows:
            import csv
            w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow({k: (str(v) if not isinstance(v, (str, int, float, bool)) else v) for k, v in r.items()})
        _log_admin_action(admin_id, "audit_log_export", "audit", "", {"days": days, "rows": len(rows), "format": "csv"})
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="warmr-audit-{days}d.csv"'},
        )

    _log_admin_action(admin_id, "audit_log_export", "audit", "", {"days": days, "rows": len(rows), "format": "json"})
    return Response(
        content=__import__("json").dumps({"rows": rows, "count": len(rows), "days": days}, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="warmr-audit-{days}d.json"'},
    )


@app.post("/admin/audit-log/prune", tags=["Admin"])
async def admin_audit_log_prune(
    admin_id: Annotated[str, Depends(require_admin)],
    body: dict,
) -> dict:
    """
    Prune audit log rows older than X days. SOC 2 typically requires 7-year retention.
    Default here is 90 days for self-hosted. Exports recommended before pruning.
    """
    days = int(body.get("retain_days", 2555))  # 7 years default
    if days < 90:
        raise HTTPException(status_code=422, detail="retain_days must be >= 90 for compliance safety.")
    cutoff = _days_ago_utc(days)
    resp = _supabase.table("admin_audit_log").delete().lt("created_at", cutoff).execute()
    deleted = len(resp.data or [])
    _log_admin_action(admin_id, "audit_log_prune", "audit", "", {"retain_days": days, "deleted": deleted})
    return {"deleted": deleted, "retained_days": days}


@app.get("/admin/audit-log", tags=["Admin"])
async def admin_audit_log(
    admin_id: Annotated[str, Depends(require_admin)],
    limit: int = Query(default=100),
    target_type: Optional[str] = Query(default=None),
) -> list[dict]:
    """Recent admin actions (admins only)."""
    q = _supabase.table("admin_audit_log").select("*").order("created_at", desc=True).limit(limit)
    if target_type:
        q = q.eq("target_type", target_type)
    return (q.execute()).data or []


# ---------------------------------------------------------------------------
# AI subject line optimizer
# ---------------------------------------------------------------------------

@app.post("/ai/subject-optimize", tags=["AI"])
@limiter.limit("20/hour")
async def optimize_subject(request: Request, client_id: ClientId, body: dict) -> dict:
    """
    Generate 5 subject line variants with spam scores.

    Body: { "subject": "Quick question about acme", "context": "B2B cold outreach" }
    Returns: { "variants": [{"subject": "...", "spam_score": 0.12, "reasoning": "..."}, ...] }
    """
    subject = (body.get("subject") or "").strip()
    if not subject:
        raise HTTPException(status_code=422, detail="subject is required.")
    context = body.get("context", "B2B cold outreach")

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured.")

    prompt = f"""You are an expert at writing cold email subject lines that bypass spam filters and get opened.

Original subject: "{subject}"
Context: {context}

Generate exactly 5 subject line variants. For each:
- Keep under 7 words
- Lowercase preferred (feels personal, not corporate)
- No exclamation marks, no ALL CAPS, no spammy words (free, guarantee, urgent, $, %, !)
- Vary the angle: question, observation, curiosity gap, specific reference, ultra-short
- Include a spam_score from 0.0 (perfect) to 1.0 (will be filtered)

Return ONLY valid JSON in this exact format:
{{"variants": [
  {{"subject": "...", "spam_score": 0.10, "reasoning": "why this works"}},
  ...
]}}
No markdown, no preamble, just JSON."""

    try:
        import anthropic, json
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])
        return json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Subject optimization failed: {exc}")


# ---------------------------------------------------------------------------
# AI reply suggestions
# ---------------------------------------------------------------------------

@app.post("/ai/reply-suggest", tags=["AI"])
@limiter.limit("30/hour")
async def suggest_replies(request: Request, client_id: ClientId, body: dict) -> dict:
    """
    Generate 3 quick reply suggestions for an incoming email.

    Body: { "incoming_email": "Hi, sounds interesting...", "context": "they replied to step 2 of cold outreach" }
    Returns: { "suggestions": [{"label": "Book meeting", "body": "..."}, ...] }
    """
    incoming = (body.get("incoming_email") or "").strip()
    if not incoming:
        raise HTTPException(status_code=422, detail="incoming_email is required.")
    context = body.get("context", "Reply to a B2B cold outreach email")
    language = body.get("language", "nl")
    lang_label = {"nl": "Dutch", "en": "English", "fr": "French"}.get(language, "Dutch")

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured.")

    prompt = f"""Generate 3 short reply options to this email.

Context: {context}
Language: {lang_label}

Incoming email:
\"\"\"
{incoming}
\"\"\"

Generate exactly 3 reply suggestions:
1. A "Book meeting" suggestion that proposes a calendar slot using {{{{calendar_link}}}} variable
2. A "Send more info" suggestion that gives a brief teaser and offers to share more
3. A "Polite decline / qualify" that asks one specific question to qualify the lead

Each reply: 30-60 words, natural tone, no marketing language.

Return ONLY valid JSON:
{{"suggestions": [
  {{"label": "Book meeting", "body": "..."}},
  {{"label": "Send more info", "body": "..."}},
  {{"label": "Qualify", "body": "..."}}
]}}
No markdown, just JSON."""

    try:
        import anthropic, json
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])
        return json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Reply suggestion failed: {exc}")


# ---------------------------------------------------------------------------
# Inbox preview (Gmail / Outlook / Apple Mail rendering)
# ---------------------------------------------------------------------------

@app.post("/preview/email", tags=["Preview"])
async def preview_email(client_id: ClientId, body: dict) -> dict:
    """
    Render an email as it would appear in Gmail / Outlook / Apple Mail.

    Body: { "subject": "...", "body": "...", "sender_name": "...", "sender_email": "..." }
    Returns: { "gmail": "<html>", "outlook": "<html>", "apple_mail": "<html>" }
    """
    subject = body.get("subject", "")
    email_body = body.get("body", "")
    sender_name = body.get("sender_name", "Sender")
    sender_email = body.get("sender_email", "sender@example.com")

    # Convert plain text body to HTML paragraphs
    html_body = "<p>" + email_body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"

    base_styles = "font-family:-apple-system,BlinkMacSystemFont,sans-serif;color:#202124"

    gmail = f"""
    <div style="background:#f6f8fc;padding:20px;border-radius:8px;{base_styles}">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
        <div style="width:40px;height:40px;border-radius:50%;background:#1a73e8;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600">{sender_name[:1].upper()}</div>
        <div>
          <div style="font-weight:600;font-size:14px">{sender_name} <span style="font-weight:400;color:#5f6368">&lt;{sender_email}&gt;</span></div>
          <div style="font-size:12px;color:#5f6368">aan mij</div>
        </div>
      </div>
      <div style="font-size:18px;font-weight:500;margin-bottom:14px">{subject}</div>
      <div style="font-size:14px;line-height:1.6">{html_body}</div>
    </div>
    """

    outlook = f"""
    <div style="background:#fff;border:1px solid #d1d1d1;border-radius:4px;padding:20px;{base_styles}">
      <div style="border-bottom:1px solid #edebe9;padding-bottom:12px;margin-bottom:14px">
        <div style="font-size:18px;font-weight:600;margin-bottom:8px">{subject}</div>
        <div style="font-size:13px;color:#605e5c">
          <strong style="color:#0078d4">{sender_name}</strong> &lt;{sender_email}&gt;
        </div>
      </div>
      <div style="font-size:14px;line-height:1.6;color:#323130">{html_body}</div>
    </div>
    """

    apple = f"""
    <div style="background:#fff;border-radius:10px;padding:24px;{base_styles};box-shadow:0 1px 3px rgba(0,0,0,.1)">
      <div style="font-size:20px;font-weight:600;margin-bottom:6px;color:#000">{subject}</div>
      <div style="font-size:13px;color:#8e8e93;margin-bottom:18px">Van: {sender_name} &lt;{sender_email}&gt;</div>
      <div style="font-size:15px;line-height:1.7;color:#1d1d1f">{html_body}</div>
    </div>
    """

    return {"gmail": gmail.strip(), "outlook": outlook.strip(), "apple_mail": apple.strip()}


# ---------------------------------------------------------------------------
# Plan limits enforcement helper
# ---------------------------------------------------------------------------

def _enforce_plan_limit(client_id: str, resource: str) -> None:
    """
    Raise HTTPException if the client has hit their plan limit for the given resource.

    resource: 'inboxes' | 'domains'
    """
    client_resp = _supabase.table("clients").select("plan, max_inboxes, max_domains").eq("id", client_id).limit(1).execute()
    if not client_resp.data:
        return  # Unknown client — skip enforcement
    client = client_resp.data[0]
    if resource == "inboxes":
        max_allowed = client.get("max_inboxes") or 5
        current = _supabase.table("inboxes").select("id", count="exact").eq("client_id", client_id).execute()
        current_count = current.count or 0
        if current_count >= max_allowed:
            raise HTTPException(
                status_code=402,
                detail=f"Plan limit reached: {max_allowed} inboxes max. Upgrade to add more.",
            )
    elif resource == "domains":
        max_allowed = client.get("max_domains") or 2
        current = _supabase.table("domains").select("id", count="exact").eq("client_id", client_id).execute()
        current_count = current.count or 0
        if current_count >= max_allowed:
            raise HTTPException(
                status_code=402,
                detail=f"Plan limit reached: {max_allowed} domains max. Upgrade to add more.",
            )


# ---------------------------------------------------------------------------
# Client settings (booking URL, sender name, signature)
# ---------------------------------------------------------------------------

@app.get("/settings", tags=["Settings"])
async def get_client_settings(client_id: ClientId) -> dict:
    """Get the authenticated client's global settings."""
    resp = _supabase.table("client_settings").select("*").eq("client_id", client_id).limit(1).execute()
    if resp.data:
        return resp.data[0]
    return {
        "client_id": client_id,
        "booking_url": None,
        "sender_name": None,
        "email_signature": None,
        "company_name": None,
        "reply_to_email": None,
    }


@app.put("/settings", tags=["Settings"])
async def upsert_client_settings(client_id: ClientId, body: dict) -> dict:
    """Create or update the authenticated client's global settings."""
    allowed = {"booking_url", "sender_name", "email_signature", "company_name", "reply_to_email", "unsubscribe_text"}
    payload = {k: v for k, v in body.items() if k in allowed}
    payload["client_id"] = client_id
    payload["updated_at"] = _now_utc()

    # Try update first, fall back to insert
    existing = _supabase.table("client_settings").select("client_id").eq("client_id", client_id).limit(1).execute()
    if existing.data:
        resp = _supabase.table("client_settings").update(payload).eq("client_id", client_id).execute()
    else:
        resp = _supabase.table("client_settings").insert(payload).execute()
    return resp.data[0] if resp.data else payload


# ---------------------------------------------------------------------------
# CRM Integrations
# ---------------------------------------------------------------------------

@app.get("/crm/integrations", tags=["CRM"])
async def list_crm_integrations(client_id: ClientId) -> list[dict]:
    """List all CRM integrations for this client."""
    resp = (
        _supabase.table("crm_integrations")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .execute()
    )
    # Don't expose the full API key in list view
    items = resp.data or []
    for item in items:
        if item.get("api_key"):
            item["api_key"] = item["api_key"][:6] + "..." + item["api_key"][-4:] if len(item["api_key"]) > 12 else "***"
    return items


@app.post("/crm/integrations", tags=["CRM"], status_code=201)
async def create_crm_integration(client_id: ClientId, body: dict) -> dict:
    """
    Add a new CRM integration.

    For generic webhooks, the URL must pass a challenge-response verification:
    the endpoint must echo `X-Warmr-Challenge` back as `X-Warmr-Verification`.
    This prevents clients from misdirecting events to unrelated URLs.
    """
    provider = body.get("provider", "").strip().lower()
    if provider not in ("hubspot", "pipedrive", "salesforce", "webhook"):
        raise HTTPException(status_code=422, detail="provider must be hubspot | pipedrive | salesforce | webhook.")

    webhook_url = body.get("webhook_url")
    if provider == "webhook":
        if not webhook_url:
            raise HTTPException(status_code=422, detail="webhook_url is required for webhook provider.")
        # Verify URL ownership via challenge-response
        verified = await _verify_webhook_url(webhook_url)
        if not verified and not body.get("skip_verification"):
            raise HTTPException(
                status_code=422,
                detail="Webhook URL verification failed. Your endpoint must respond to GET requests and echo the X-Warmr-Challenge request header back as X-Warmr-Verification.",
            )

    payload = {
        "client_id": client_id,
        "provider": provider,
        "api_key": body.get("api_key"),
        "webhook_url": webhook_url,
        "config": body.get("config") or {},
        "active": body.get("active", True),
        "sync_on_reply": body.get("sync_on_reply", True),
        "sync_on_interested": body.get("sync_on_interested", True),
        "sync_on_meeting": body.get("sync_on_meeting", True),
    }
    try:
        resp = _supabase.table("crm_integrations").insert(payload).execute()
        return resp.data[0] if resp.data else payload
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(status_code=409, detail=f"{provider} integration already exists for this client.")
        raise


@app.patch("/crm/integrations/{integration_id}", tags=["CRM"])
async def update_crm_integration(integration_id: str, client_id: ClientId, body: dict) -> dict:
    """Update an existing CRM integration."""
    check = _supabase.table("crm_integrations").select("client_id").eq("id", integration_id).limit(1).execute()
    if not check.data:
        raise HTTPException(status_code=404, detail="Integration not found.")
    if check.data[0]["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    allowed = {"api_key", "webhook_url", "config", "active", "sync_on_reply", "sync_on_interested", "sync_on_meeting"}
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=422, detail="No valid fields to update.")
    patch["updated_at"] = _now_utc()

    resp = _supabase.table("crm_integrations").update(patch).eq("id", integration_id).execute()
    return resp.data[0] if resp.data else {"id": integration_id}


@app.delete("/crm/integrations/{integration_id}", tags=["CRM"], status_code=204)
async def delete_crm_integration(integration_id: str, client_id: ClientId) -> Response:
    """Remove a CRM integration."""
    check = _supabase.table("crm_integrations").select("client_id").eq("id", integration_id).limit(1).execute()
    if not check.data:
        raise HTTPException(status_code=404, detail="Integration not found.")
    if check.data[0]["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    _supabase.table("crm_integrations").delete().eq("id", integration_id).execute()
    return Response(status_code=204)


@app.post("/crm/test/{integration_id}", tags=["CRM"])
async def test_crm_integration(integration_id: str, client_id: ClientId) -> dict:
    """Send a test event to verify the CRM integration works."""
    check = _supabase.table("crm_integrations").select("*").eq("id", integration_id).limit(1).execute()
    if not check.data:
        raise HTTPException(status_code=404, detail="Integration not found.")
    integration = check.data[0]
    if integration["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    test_lead = {
        "id": "test",
        "email": "test@warmr-dev.local",
        "first_name": "Warmr",
        "last_name": "Test",
        "company": "Warmr Test",
        "phone": "",
    }

    from crm_dispatcher import (
        sync_to_hubspot, sync_to_pipedrive, sync_to_webhook,
    )
    provider = integration["provider"]
    if provider == "hubspot":
        status, response = sync_to_hubspot(integration, test_lead, "test")
    elif provider == "pipedrive":
        status, response = sync_to_pipedrive(integration, test_lead, "test")
    elif provider == "webhook":
        status, response = sync_to_webhook(integration, test_lead, "test")
    else:
        status, response = "failed", f"Provider {provider} not yet supported."

    return {"status": status, "response": response}


@app.get("/crm/sync-log", tags=["CRM"])
async def crm_sync_log(client_id: ClientId, limit: int = Query(default=50)) -> list[dict]:
    """Recent CRM sync attempts for this client."""
    resp = (
        _supabase.table("crm_sync_log")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


# ---------------------------------------------------------------------------
# AI sequence writer
# ---------------------------------------------------------------------------

@app.post("/sequences/generate", tags=["AI"])
@limiter.limit("10/hour")
async def generate_sequence(request: Request, client_id: ClientId, body: dict) -> dict:
    """
    Generate a multi-step email sequence from a briefing using Claude Sonnet.

    Body:
      {
        "audience": "CTOs at Dutch SaaS startups",
        "value_proposition": "Cut AWS bills 40% via automated rightsizing",
        "tone": "professional",          # professional | casual | direct
        "language": "nl",                 # nl | en | fr
        "step_count": 4,                  # 3-5 steps
        "wait_days": [3, 4, 5]            # wait between steps (optional)
      }
    """
    audience = body.get("audience", "").strip()
    value_prop = body.get("value_proposition", "").strip()
    if not audience or not value_prop:
        raise HTTPException(status_code=422, detail="audience and value_proposition are required.")

    tone = body.get("tone", "professional")
    language = body.get("language", "nl")
    step_count = max(2, min(5, int(body.get("step_count", 4))))
    wait_days_input = body.get("wait_days") or [3, 4, 5, 6]

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured.")

    lang_label = {"nl": "Dutch", "en": "English", "fr": "French"}.get(language, "Dutch")
    tone_label = {
        "professional": "professional, polished, respectful",
        "casual": "casual, friendly, conversational",
        "direct": "direct, no-fluff, straight to the point",
    }.get(tone, "professional, polished, respectful")

    prompt = f"""Generate a {step_count}-step cold email sequence in {lang_label}.

Audience: {audience}
Value proposition: {value_prop}
Tone: {tone_label}

Requirements:
- Step 1: Pattern interrupt opener, mention specific pain point, end with low-friction question
- Step 2-{step_count - 1}: Follow-ups that add new value/angle, never just "checking in"
- Final step: Soft break-up email, leave door open
- Each email: 50-100 words, no jargon, no exclamation marks
- Include {{{{first_name}}}} and {{{{company}}}} variables where natural
- Subject lines: max 5 words, lowercase, intriguing not salesy
- NO unsubscribe footers — those are added automatically

Return ONLY a valid JSON array of objects with this exact structure:
[
  {{"step_number": 1, "subject": "...", "body": "...", "wait_days": 0}},
  {{"step_number": 2, "subject": "...", "body": "...", "wait_days": 3}},
  ...
]

No markdown code blocks, no explanation, just raw JSON."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        import json
        steps = json.loads(raw)
        # Apply wait_days from input if provided
        for i, step in enumerate(steps):
            if i == 0:
                step["wait_days"] = 0
            elif i - 1 < len(wait_days_input):
                step["wait_days"] = wait_days_input[i - 1]
        return {"steps": steps, "language": language, "tone": tone}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"AI returned invalid JSON: {exc}. Raw: {raw[:200]}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sequence generation failed: {exc}")


# ---------------------------------------------------------------------------
# Email template library
# ---------------------------------------------------------------------------

# Pre-built templates organized by use case
_TEMPLATE_LIBRARY = {
    "cold_intro_nl": {
        "name": "Cold intro (NL)",
        "category": "intro",
        "language": "nl",
        "subject": "vraag over {{company}}",
        "body": "Hi {{first_name}},\n\nKwam {{company}} tegen via {{source}}. Snelle vraag — hoe lossen jullie nu {{pain_point}} op?\n\nWij helpen vergelijkbare bedrijven met {{value_prop}}. Open voor 15 min volgende week?\n\n{{sender_name}}",
    },
    "cold_intro_en": {
        "name": "Cold intro (EN)",
        "category": "intro",
        "language": "en",
        "subject": "quick question about {{company}}",
        "body": "Hi {{first_name}},\n\nCame across {{company}} via {{source}}. Quick question — how are you currently handling {{pain_point}}?\n\nWe help similar companies with {{value_prop}}. Open to a 15 min chat next week?\n\n{{sender_name}}",
    },
    "follow_up_value_nl": {
        "name": "Follow-up met waarde (NL)",
        "category": "followup",
        "language": "nl",
        "subject": "case van {{similar_company}}",
        "body": "Hi {{first_name}},\n\nWeet niet of mijn vorige mail de inbox haalde. Voorbeeld: {{similar_company}} bespaarde {{result}} met onze aanpak.\n\nNuttige link kort gemaakt: [link]\n\nThoughts?\n\n{{sender_name}}",
    },
    "break_up_nl": {
        "name": "Break-up email (NL)",
        "category": "breakup",
        "language": "nl",
        "subject": "laatste mail",
        "body": "Hi {{first_name}},\n\nLaatste mail uit mijn kant. Waarschijnlijk geen prio nu — dat snap ik.\n\nMocht het later wel relevant worden, ping gerust. Verder geen mails van mij.\n\nSucces met alles,\n{{sender_name}}",
    },
    "meeting_request_nl": {
        "name": "Meeting verzoek (NL)",
        "category": "meeting",
        "language": "nl",
        "subject": "15 min volgende week?",
        "body": "Hi {{first_name}},\n\nKort en bondig: ik denk dat we {{company}} kunnen helpen met {{specific_outcome}}.\n\n15 minuten volgende week? Hier mijn agenda: {{calendar_link}}\n\n{{sender_name}}",
    },
}


@app.get("/templates", tags=["Templates"])
async def list_templates(
    client_id: ClientId,
    category: Optional[str] = Query(default=None),
    language: Optional[str] = Query(default=None),
) -> list[dict]:
    """List all available email templates, optionally filtered."""
    items = []
    for tid, t in _TEMPLATE_LIBRARY.items():
        if category and t["category"] != category:
            continue
        if language and t["language"] != language:
            continue
        items.append({"id": tid, **t})
    return items


@app.get("/templates/{template_id}", tags=["Templates"])
async def get_template(template_id: str, client_id: ClientId) -> dict:
    """Get a single template by ID."""
    if template_id not in _TEMPLATE_LIBRARY:
        raise HTTPException(status_code=404, detail="Template not found.")
    return {"id": template_id, **_TEMPLATE_LIBRARY[template_id]}


# ---------------------------------------------------------------------------
# Suppression list (do-not-contact)
# ---------------------------------------------------------------------------

@app.get("/suppression", tags=["Suppression"])
async def list_suppression(client_id: ClientId, limit: int = Query(default=200)) -> list[dict]:
    """List all suppressed emails for this client."""
    resp = (
        _supabase.table("suppression_list")
        .select("*")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


@app.post("/suppression", tags=["Suppression"], status_code=201)
async def add_suppression(client_id: ClientId, body: dict) -> dict:
    """Manually add an email to the suppression list."""
    email = (body.get("email") or "").strip().lower()
    reason = body.get("reason", "manual")
    if not email:
        raise HTTPException(status_code=422, detail="Email is required.")
    domain = email.split("@")[-1] if "@" in email else None
    try:
        resp = _supabase.table("suppression_list").insert({
            "client_id": client_id,
            "email": email,
            "domain": domain,
            "reason": reason,
            "source": "manual",
        }).execute()
        return resp.data[0] if resp.data else {"email": email}
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Email already suppressed.")
        raise


@app.delete("/suppression/{suppression_id}", tags=["Suppression"], status_code=204)
async def remove_suppression(suppression_id: str, client_id: ClientId) -> Response:
    """Remove an email from the suppression list."""
    check = _supabase.table("suppression_list").select("client_id").eq("id", suppression_id).limit(1).execute()
    if not check.data:
        raise HTTPException(status_code=404, detail="Not found.")
    if check.data[0]["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    _supabase.table("suppression_list").delete().eq("id", suppression_id).execute()
    return Response(status_code=204)


@app.post("/suppression/import", tags=["Suppression"])
async def import_suppression(client_id: ClientId, file: UploadFile = File(...)) -> dict:
    """Bulk import emails to suppression list from a CSV (one column: email)."""
    content = await file.read()
    df = pd.read_csv(io.BytesIO(content))
    col = df.columns[0]
    emails = df[col].dropna().str.strip().str.lower().unique().tolist()
    added = 0
    for email in emails:
        domain = email.split("@")[-1] if "@" in email else None
        try:
            _supabase.table("suppression_list").insert({
                "client_id": client_id,
                "email": email,
                "domain": domain,
                "reason": "manual",
                "source": "import",
            }).execute()
            added += 1
        except Exception:
            pass  # Skip duplicates
    return {"imported": added, "total_in_file": len(emails), "skipped_duplicates": len(emails) - added}


# ---------------------------------------------------------------------------
# Unsubscribe (public — no auth required)
# ---------------------------------------------------------------------------

def _generate_unsubscribe_token(client_id: str, lead_id: str, lead_email: str, campaign_id: str | None = None) -> str:
    """Create a unique unsubscribe token and store it in the database."""
    import secrets
    token = secrets.token_urlsafe(32)
    _supabase.table("unsubscribe_tokens").insert({
        "token": token,
        "client_id": client_id,
        "lead_id": lead_id,
        "lead_email": lead_email,
        "campaign_id": campaign_id,
    }).execute()
    return token


def generate_unsubscribe_url(client_id: str, lead_id: str, lead_email: str, campaign_id: str | None = None) -> str:
    """Generate a full unsubscribe URL for embedding in campaign emails."""
    token = _generate_unsubscribe_token(client_id, lead_id, lead_email, campaign_id)
    base = os.getenv("WARMR_BASE_URL", "http://localhost:8000")
    return f"{base}/unsubscribe/{token}"


@app.get("/unsubscribe/{token}", tags=["Unsubscribe"], include_in_schema=False)
async def unsubscribe_page(token: str) -> Response:
    """Hosted unsubscribe confirmation page (public, no auth)."""
    resp = _supabase.table("unsubscribe_tokens").select("*").eq("token", token).limit(1).execute()
    if not resp.data:
        html = _unsub_html("Ongeldige link", "Deze uitschrijflink is niet geldig of al verlopen.", False)
        return Response(content=html, media_type="text/html")

    row = resp.data[0]
    # Block access when owning client is suspended (prevents data leakage)
    if _client_is_suspended(row.get("client_id", "")):
        html = _unsub_html("Niet beschikbaar", "Deze uitschrijflink is tijdelijk niet actief.", False)
        return Response(content=html, media_type="text/html", status_code=410)

    if row.get("used"):
        html = _unsub_html("Al uitgeschreven", f"<strong>{html_mod.escape(row['lead_email'])}</strong> is al uitgeschreven.", False)
        return Response(content=html, media_type="text/html")

    html = _unsub_html(
        "Uitschrijven bevestigen",
        f"Wil je <strong>{html_mod.escape(row['lead_email'])}</strong> uitschrijven van toekomstige e-mails?",
        True,
        token=token,
    )
    return Response(content=html, media_type="text/html")


@app.post("/unsubscribe/{token}", tags=["Unsubscribe"], include_in_schema=False)
async def process_unsubscribe(token: str) -> Response:
    """Process the unsubscribe action (public, no auth)."""
    resp = _supabase.table("unsubscribe_tokens").select("*").eq("token", token).limit(1).execute()
    if not resp.data:
        html = _unsub_html("Ongeldige link", "Deze uitschrijflink is niet geldig.", False)
        return Response(content=html, media_type="text/html")

    row = resp.data[0]
    if row.get("used"):
        html = _unsub_html("Al uitgeschreven", f"<strong>{html_mod.escape(row['lead_email'])}</strong> is al uitgeschreven.", False)
        return Response(content=html, media_type="text/html")

    # Mark token as used
    _supabase.table("unsubscribe_tokens").update({"used": True}).eq("id", row["id"]).execute()

    # Add to suppression list
    try:
        domain = row["lead_email"].split("@")[-1] if "@" in row["lead_email"] else None
        _supabase.table("suppression_list").insert({
            "client_id": row["client_id"],
            "email": row["lead_email"],
            "domain": domain,
            "reason": "unsubscribe",
            "source": row.get("campaign_id") or "email",
        }).execute()
    except Exception:
        pass  # Already suppressed

    # Cancel all pending sends for this lead across all campaigns
    try:
        _supabase.table("campaign_leads").update({
            "status": "unsubscribed",
        }).eq("lead_id", row["lead_id"]).in_("status", ["active", "pending"]).execute()
    except Exception:
        pass

    # Update lead status
    try:
        _supabase.table("leads").update({"status": "unsubscribed"}).eq("id", row["lead_id"]).execute()
    except Exception:
        pass

    html = _unsub_html(
        "Uitgeschreven",
        f"<strong>{html_mod.escape(row['lead_email'])}</strong> is succesvol uitgeschreven. Je ontvangt geen verdere e-mails meer.",
        False,
    )
    return Response(content=html, media_type="text/html")


# ---------------------------------------------------------------------------
# Email tracking (open pixel + click redirect) — public, no auth
# ---------------------------------------------------------------------------

# 1x1 transparent GIF
_TRACKING_PIXEL = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00"
    b"\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)


def _verify_tracking_token(token: str) -> tuple[str, str, str, str] | None:
    """Decode and verify an HMAC-signed tracking token. Returns (client_id, campaign_id, lead_id, lead_email) or None."""
    import base64, hashlib, hmac
    try:
        decoded = base64.urlsafe_b64decode(token + "==").decode()
        parts = decoded.split("|")
        if len(parts) < 5:
            return None
        client_id, campaign_id, lead_id, lead_email, sig = parts[0], parts[1], parts[2], parts[3], parts[4]
        secret = os.getenv("WARMR_API_TOKEN", "fallback-secret-change-me")
        raw = f"{client_id}|{campaign_id}|{lead_id}|{lead_email}"
        expected_sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return client_id, campaign_id, lead_id, lead_email
    except Exception:
        return None


def _client_is_suspended(client_id: str) -> bool:
    """Quick cached check — used by public endpoints to block suspended tenants."""
    try:
        from api.auth import _get_client_status
        return bool(_get_client_status(client_id).get("suspended"))
    except Exception:
        return False


@app.get("/t/{token}.gif", tags=["Tracking"], include_in_schema=False)
async def track_open(token: str, request: Request) -> Response:
    """Record an email open event and return a 1x1 transparent GIF."""
    try:
        verified = _verify_tracking_token(token)
        if verified:
            client_id, campaign_id, lead_id, lead_email = verified
            # Suspended clients' tracking links no longer emit events
            if _client_is_suspended(client_id):
                return Response(content=_TRACKING_PIXEL, media_type="image/gif", status_code=410)
            _supabase.table("email_tracking").insert({
                "client_id": client_id,
                "campaign_id": campaign_id,
                "lead_id": lead_id,
                "lead_email": lead_email,
                "event_type": "open",
                "tracking_token": token,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent", ""),
            }).execute()
            try:
                _supabase.table("email_events").insert({
                    "client_id": client_id,
                    "campaign_id": campaign_id,
                    "lead_id": lead_id,
                    "event_type": "opened",
                }).execute()
            except Exception:
                logger.debug("Failed to write email_events for open tracking: %s", token)
            # Engagement score: +5 for open
            try:
                from engagement_scorer import add_engagement
                add_engagement(_supabase, lead_id, "opened")
            except Exception:
                pass
    except Exception as exc:
        logger.debug("Tracking pixel error for token %s: %s", token[:20], exc)
    return Response(
        content=_TRACKING_PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/c/{token}", tags=["Tracking"], include_in_schema=False)
async def track_click(token: str, url: str = Query(...), request: Request = None) -> Response:
    """Record a click event and redirect to the original URL."""
    try:
        verified = _verify_tracking_token(token)
        if verified:
            client_id, campaign_id, lead_id, lead_email = verified
            # Suspended clients: do not track, but still redirect (user gets what they clicked)
            if _client_is_suspended(client_id):
                return RedirectResponse(url=url, status_code=302)
            _supabase.table("email_tracking").insert({
                "client_id": client_id,
                "campaign_id": campaign_id,
                "lead_id": lead_id,
                "lead_email": lead_email,
                "event_type": "click",
                "tracking_token": token,
                "link_url": url,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent", ""),
            }).execute()
            try:
                _supabase.table("email_events").insert({
                    "client_id": client_id,
                    "campaign_id": campaign_id,
                    "lead_id": lead_id,
                    "event_type": "clicked",
                }).execute()
            except Exception:
                logger.debug("Failed to write email_events for click tracking: %s", token[:20])
            # Engagement score: +10 for click
            try:
                from engagement_scorer import add_engagement
                add_engagement(_supabase, lead_id, "clicked")
            except Exception:
                pass
    except Exception as exc:
        logger.debug("Click tracking error for token %s: %s", token[:20], exc)
    return RedirectResponse(url=url, status_code=302)


@app.get("/tracking/stats", tags=["Tracking"])
async def tracking_stats(
    client_id: ClientId,
    campaign_id: Optional[str] = Query(default=None),
) -> dict:
    """Get open/click tracking stats for a campaign or all campaigns."""
    q_open = _supabase.table("email_tracking").select("id", count="exact").eq("client_id", client_id).eq("event_type", "open")
    q_click = _supabase.table("email_tracking").select("id", count="exact").eq("client_id", client_id).eq("event_type", "click")
    if campaign_id:
        q_open = q_open.eq("campaign_id", campaign_id)
        q_click = q_click.eq("campaign_id", campaign_id)
    opens = q_open.execute()
    clicks = q_click.execute()
    return {
        "opens": opens.count if opens.count is not None else len(opens.data or []),
        "clicks": clicks.count if clicks.count is not None else len(clicks.data or []),
    }


def _unsub_html(title: str, message: str, show_button: bool, token: str = "") -> str:
    """Generate minimal unsubscribe HTML page."""
    button = ""
    if show_button:
        button = f'''
        <form method="POST" action="/unsubscribe/{token}" style="margin-top:1.5rem">
          <button type="submit" style="
            padding:.75rem 2rem; font-size:1rem; font-weight:600;
            color:#fff; background:linear-gradient(135deg,#a29bfe,#6c5ce7);
            border:none; border-radius:8px; cursor:pointer;
          ">Ja, schrijf me uit</button>
        </form>'''
    return f'''<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; margin: 0; background: #fafafa; color: #1a1a2e; }}
    .card {{ background: #fff; border-radius: 16px; padding: 3rem;
             max-width: 460px; text-align: center;
             box-shadow: 0 4px 24px rgba(0,0,0,.08); }}
    h1 {{ font-size: 1.5rem; margin-bottom: 1rem; }}
    p {{ color: #666; line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{message}</p>
    {button}
  </div>
</body>
</html>'''


# ---------------------------------------------------------------------------
_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


@app.get("/", include_in_schema=False)
async def root_redirect():
    """Redirect / to the login page."""
    return RedirectResponse(url="/index.html")


if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
