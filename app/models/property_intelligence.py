from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class PropertyIntelligenceSnapshot(TimestampMixin, Base):
    __tablename__ = "property_intelligence_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    normalized_address: Mapped[str] = mapped_column(Text, nullable=False)
    address_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_status: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    address: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    google_place: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rentcast_property: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rentcast_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rentcast_rent: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    rentcast_market: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fema_flood: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
