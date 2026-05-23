from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.enums import ClientStage
from app.schemas.common import ORMModel


_LeadSource = Literal[
    "manual_entry",
    "open_house",
    "referral",
    "listing_inquiry",
    "buyer_consultation",
    "existing_database",
    "other",
]
_LeadTemperature = Literal["new", "hot", "warm", "cold", "nurture"]
_FinancingSupportNeeded = Literal["yes", "maybe", "no", "unknown"]
_ContactPermission = Literal[
    "send_invite_now",
    "save_lead_only",
    "agent_will_introduce_first",
]
_RelationshipContext = Literal[
    "new_lead",
    "existing_client",
    "past_client",
    "referral_from_other",
    "other",
]
_LeadPromotionStatus = Literal[
    "not_ready",
    "agent_requested_review",
    "funding_reviewing",
    "promoted_to_intake",
    "declined",
]


class ClientCreate(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    referral_source: str | None = None
    broker_id: UUID | None = None
    # Lead routing / ownership / attribution (alembic 0029).
    # Captured by the AgentLeadModal; persists on the Client row so the
    # pipeline view can filter / scope and the funding team has context
    # at promotion time.
    lead_source: _LeadSource | None = None
    lead_temperature: _LeadTemperature | None = None
    financing_support_needed: _FinancingSupportNeeded | None = None
    contact_permission: _ContactPermission | None = None
    relationship_context: _RelationshipContext | None = None
    originating_agent_id: UUID | None = None
    current_agent_id: UUID | None = None
    source_channel: str | None = None
    # Preferred language — the AI composer uses this to write outbound
    # messages in the client's language when set.
    language: str | None = None
    # Lead-funnel fields (alembic 0024). Defaults to 'lead' when
    # not specified — agents creating clients via the
    # LeadsPipelineView "+ Add Lead" button leave these to default.
    # `client_type` (buyer/seller) optional at creation but used
    # by the doc-checklist resolver downstream.
    stage: ClientStage = ClientStage.LEAD
    client_type: Literal["buyer", "seller"] | None = None
    # Per-lead overrides (alembic 0025). Captured by the AddLeadWizard:
    # `lead_intake` carries property/financial context, `checklist_overrides`
    # disables firm items + appends agent extras for THIS lead, and
    # `ai_cadence_override` lets the agent dial nudge frequency per lead.
    # All free-shape JSONB on the backend; the wizard owns the schema.
    lead_intake: dict[str, Any] | None = None
    checklist_overrides: dict[str, Any] | None = None
    ai_cadence_override: dict[str, Any] | None = None
    # Experience mode (alembic 0026). Dashboard-created clients pass
    # "guided"; self-signups leave this NULL (UI derives self_directed).
    client_experience_mode: Literal["guided", "self_directed", "hybrid"] | None = None
    client_experience_mode_reason: str | None = None
    client_experience_mode_locked_by: Literal["agent", "client", "firm"] | None = None


class ClientUpdate(BaseModel):
    """Partial update — every field optional. None means 'no change'."""
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    referral_source: str | None = None
    broker_id: UUID | None = None
    tier: str | None = None
    fico: int | None = None
    avatar_color: str | None = None
    properties: str | None = None
    experience: str | None = None
    # Lead-funnel transitions — agents flip stage via PATCH (e.g.
    # lead → contacted on first outreach). Backend stamps
    # `contacted_at = now()` automatically when stage flips into
    # CONTACTED for the first time (handled in the router).
    stage: ClientStage | None = None
    client_type: Literal["buyer", "seller"] | None = None
    contacted_at: datetime | None = None
    # Per-lead overrides (alembic 0025) — same shape as ClientCreate.
    # Send `null` to clear an override and fall back to broker defaults.
    lead_intake: dict[str, Any] | None = None
    checklist_overrides: dict[str, Any] | None = None
    ai_cadence_override: dict[str, Any] | None = None
    # Experience mode (alembic 0026). PATCH path so the desktop's
    # client-detail toggle persists.
    client_experience_mode: Literal["guided", "self_directed", "hybrid"] | None = None
    client_experience_mode_reason: str | None = None
    client_experience_mode_locked_by: Literal["agent", "client", "firm"] | None = None
    # Lead routing fields are PATCH-able too (e.g. agent reassigns,
    # funding team flips promotion status).
    lead_source: _LeadSource | None = None
    lead_temperature: _LeadTemperature | None = None
    financing_support_needed: _FinancingSupportNeeded | None = None
    contact_permission: _ContactPermission | None = None
    relationship_context: _RelationshipContext | None = None
    lead_promotion_status: _LeadPromotionStatus | None = None
    originating_agent_id: UUID | None = None
    current_agent_id: UUID | None = None
    source_channel: str | None = None
    language: str | None = None


class ClientSelfUpdate(BaseModel):
    """The fields a CLIENT-role user is allowed to change on themselves
    via PATCH /clients/me. Excludes everything underwriting-sensitive
    (tier, fico, broker_id, funded totals)."""
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    properties: str | None = None
    experience: str | None = None


class ClientRead(ORMModel):
    id: UUID
    user_id: UUID | None
    broker_id: UUID | None
    # Owner display name for the pipeline / clients list header.
    # Populated by list endpoints via a join on brokers.display_name.
    broker_name: str | None = None
    name: str
    email: str | None
    phone: str | None
    address: str | None = None
    city: str | None
    since: date | None
    tier: str
    fico: int | None
    avatar_color: str | None
    funded_total: float
    funded_count: int
    properties: str | None = None
    experience: str | None = None
    # Lead-funnel state (alembic 0024).
    stage: ClientStage = ClientStage.LEAD
    client_type: Literal["buyer", "seller"] | None = None
    contacted_at: datetime | None = None
    intake_started_at: datetime | None = None
    intake_completed_at: datetime | None = None
    # Per-lead overrides (alembic 0025).
    lead_intake: dict[str, Any] | None = None
    checklist_overrides: dict[str, Any] | None = None
    ai_cadence_override: dict[str, Any] | None = None
    # Experience mode (alembic 0026). NULL = derive from broker_id.
    client_experience_mode: Literal["guided", "self_directed", "hybrid"] | None = None
    client_experience_mode_reason: str | None = None
    client_experience_mode_locked_by: Literal["agent", "client", "firm"] | None = None
    # Lead routing / ownership / attribution (alembic 0029).
    lead_source: _LeadSource | None = None
    lead_temperature: _LeadTemperature | None = None
    financing_support_needed: _FinancingSupportNeeded | None = None
    contact_permission: _ContactPermission | None = None
    relationship_context: _RelationshipContext | None = None
    lead_promotion_status: _LeadPromotionStatus = "not_ready"
    originating_agent_id: UUID | None = None
    current_agent_id: UUID | None = None
    language: str | None = None
    source_channel: str | None = None
    # Realtor Client Intelligence Profile (alembic 0030). Free-shape
    # JSONB written by the Realtor AI. Surfaced here so the Client
    # Readiness Card on /clients/[id] can render without an extra
    # round-trip. Never set on Create — populated by the AI as the
    # conversation evolves.
    realtor_profile: dict[str, Any] | None = None
    # Presence (alembic 0046) — joined from the linked User row. NULL if
    # the client hasn't signed into the borrower app yet. Drives the
    # online dot in the loan detail header.
    last_seen_at: datetime | None = None
