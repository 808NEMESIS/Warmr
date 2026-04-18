"""
api/models.py

Pydantic request and response models for the Warmr API.
All models use snake_case field names matching the Supabase column names.
"""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TokenVerifyResponse(BaseModel):
    """Response from POST /auth/verify."""
    client_id: str
    valid: bool


# ---------------------------------------------------------------------------
# Inboxes
# ---------------------------------------------------------------------------

class InboxCreate(BaseModel):
    """Payload for POST /inboxes."""
    email: EmailStr
    domain: str
    provider: str = Field(default="google", pattern="^(google|microsoft|other)$")
    warmup_start_date: Optional[date] = None
    notes: Optional[str] = None


class InboxPatch(BaseModel):
    """Payload for PATCH /inboxes/{id}/pause — toggles warmup_active."""
    warmup_active: bool


class InboxResponse(BaseModel):
    """Full inbox row as returned to the frontend."""
    id: UUID
    email: str
    domain: str
    provider: Optional[str]
    warmup_active: bool
    warmup_start_date: Optional[date]
    daily_warmup_target: int
    daily_campaign_target: int
    daily_sent: int
    reputation_score: float
    open_rate: float
    reply_rate: float
    spam_rescues: int
    spam_complaints: int
    last_spam_incident: Optional[datetime]
    status: str
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

class DomainCreate(BaseModel):
    """Payload for POST /domains."""
    domain: str = Field(..., min_length=3)
    registrar: Optional[str] = None
    tld: Optional[str] = None

    @field_validator("domain")
    @classmethod
    def normalise_domain(cls, v: str) -> str:
        return v.lower().strip().lstrip("www.")


class DomainResponse(BaseModel):
    """Full domain row as returned to the frontend."""
    id: UUID
    domain: str
    registrar: Optional[str]
    tld: Optional[str]
    spf_configured: bool
    dkim_configured: bool
    dmarc_phase: str
    blacklisted: bool
    last_blacklist_check: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class DNSCheckResponse(BaseModel):
    """Live DNS check result for GET /domains/{id}/dns-check."""
    domain: str
    spf: dict
    dkim: dict
    dmarc: dict
    mx: dict
    overall_healthy: bool
    errors: list[str]


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------

class WarmupStats(BaseModel):
    """Aggregated warmup stats for the authenticated client."""
    total_inboxes: int
    active_inboxes: int
    ready_inboxes: int        # reputation_score >= 70 and status == 'ready'
    total_sent_today: int
    total_sent_alltime: int
    avg_reputation_score: float
    total_spam_rescues: int
    total_spam_complaints: int
    sends_last_7_days: int
    errors_last_7_days: int


class WarmupTriggerResponse(BaseModel):
    """Response from POST /warmup/trigger."""
    triggered: bool
    message: str


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

class CampaignCreate(BaseModel):
    """Payload for POST /campaigns."""
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    inbox_ids: list[UUID] = Field(default_factory=list)


class CampaignResponse(BaseModel):
    """Campaign row response."""
    id: UUID
    name: str
    description: Optional[str]
    status: str
    total_leads: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CampaignStats(BaseModel):
    """Per-campaign performance stats for GET /campaigns/{id}/stats."""
    campaign_id: str
    name: str
    total_leads: int
    sent: int
    pending: int
    bounced: int
    replied: int
    unsubscribed: int
    reply_rate: float       # replied / sent
    bounce_rate: float      # bounced / sent


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

class LeadResponse(BaseModel):
    """Lead row returned to the frontend."""
    id: UUID
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company_name: Optional[str]
    domain: Optional[str]
    status: str
    campaign_id: Optional[str]
    notes: Optional[str]
    imported_at: datetime

    model_config = {"from_attributes": True}


class LeadImportResult(BaseModel):
    """Summary returned after POST /leads/import."""
    total_rows: int
    imported: int
    duplicates: int
    errors: int
    error_details: list[str]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotificationItem(BaseModel):
    """One notification item returned by GET /notifications."""
    id: str
    type: str        # error | spam_rescue | low_reputation | high_bounce | complaint
    inbox_email: Optional[str]
    message: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# Security-hardened request bodies (replaces raw body: dict on key endpoints)
# ---------------------------------------------------------------------------

class AdminClientPatch(BaseModel):
    """Payload for PATCH /admin/clients/{id}."""
    company_name: Optional[str] = Field(None, max_length=200)
    plan: Optional[str] = Field(None, pattern="^(trial|starter|pro|agency)$")
    max_inboxes: Optional[int] = Field(None, ge=0, le=500)
    max_domains: Optional[int] = Field(None, ge=0, le=200)
    suspended: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=1000)


class ContentScoreRequest(BaseModel):
    """Payload for POST /deliverability/score-content and /score-content/deep."""
    subject: str = Field("", max_length=998)     # RFC 5322 subject limit
    body: str = Field("", max_length=100_000)
    html_body: Optional[str] = Field(None, max_length=500_000)
    campaign_id: Optional[UUID] = None
    sequence_step_id: Optional[UUID] = None


class PlacementTestRequest(BaseModel):
    """Payload for POST /deliverability/placement-test."""
    inbox_id: UUID
    subject: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = Field(None, max_length=5000)


class BlacklistRecoveryStepPatch(BaseModel):
    """Payload for PATCH /domains/{id}/blacklist-recovery/steps/{step}."""
    recovery_id: UUID
    completed: bool = True


class WebhookPatch(BaseModel):
    """Payload for PATCH /webhooks/{id}."""
    url: Optional[str] = Field(None, max_length=2048)
    events: Optional[list[str]] = None
    active: Optional[bool] = None
    read: bool = False
