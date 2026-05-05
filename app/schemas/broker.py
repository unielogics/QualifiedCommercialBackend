from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel

from app.enums import BrokerTier
from app.schemas.common import ORMModel


class BrokerRead(ORMModel):
    id: UUID
    user_id: UUID
    display_name: str
    tier: BrokerTier
    joined: date | None
    lifetime_points: int
    redeemed_points: int
    balance_points: int
    funded_total: float
    funded_count: int


class LeaderboardEntry(BaseModel):
    rank: int
    broker_id: UUID
    display_name: str
    tier: BrokerTier
    lifetime_points: int
    funded_total: float
    funded_count: int
