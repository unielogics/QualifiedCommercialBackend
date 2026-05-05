from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel

from app.schemas.common import ORMModel


class ClientCreate(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    referral_source: str | None = None
    broker_id: UUID | None = None


class ClientUpdate(BaseModel):
    """Partial update — every field optional. None means 'no change'."""
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    referral_source: str | None = None
    broker_id: UUID | None = None
    tier: str | None = None
    fico: int | None = None
    avatar_color: str | None = None


class ClientRead(ORMModel):
    id: UUID
    user_id: UUID | None
    broker_id: UUID | None
    name: str
    email: str | None
    phone: str | None
    city: str | None
    since: date | None
    tier: str
    fico: int | None
    avatar_color: str | None
    funded_total: float
    funded_count: int
