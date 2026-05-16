"""Fix & Flip Deal Analyzer scenario schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.schemas.common import ORMModel


class FixFlipScenarioCreate(BaseModel):
    client_id: UUID | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    status: str = "saved"
    payload: dict[str, Any]
    deal_score: int | None = None
    deal_grade: str | None = None


class FixFlipScenarioUpdate(BaseModel):
    client_id: UUID | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    status: str | None = None
    payload: dict[str, Any] | None = None
    deal_score: int | None = None
    deal_grade: str | None = None


class FixFlipScenarioRead(ORMModel):
    id: UUID
    created_by: UUID | None
    client_id: UUID | None
    deal_id: UUID | None
    loan_id: UUID | None
    status: str
    payload: dict[str, Any] | None
    deal_score: int | None
    deal_grade: str | None
    created_at: datetime
    updated_at: datetime
    # Enrichment for the operator system-wide runs table (joined from
    # users on created_by). Null for orphaned / deleted creators.
    created_by_name: str | None = None
    created_by_email: str | None = None
