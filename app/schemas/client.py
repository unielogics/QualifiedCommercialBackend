from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.enums import ClientStage
from app.schemas.common import ORMModel


class ClientCreate(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    referral_source: str | None = None
    broker_id: UUID | None = None
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
