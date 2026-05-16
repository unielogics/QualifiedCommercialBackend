from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class FixFlipScenario(TimestampMixin, Base):
    """A saved Fix & Flip Deal Analyzer run.

    Standalone by design — all of client/deal/loan are optional so the
    analyzer works before any pipeline record exists. `created_by` is
    the durable owner for scoping. `payload` holds the full
    FixFlipScenario JSON (inputs + computed result). `status` tracks
    lifecycle so the "Request Funding Terms" handoff can advance it.
    """

    __tablename__ = "fix_flip_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"),
        nullable=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"),
        nullable=True,
    )
    # draft | saved | funding_requested | converted_to_prequal
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    deal_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deal_grade: Mapped[str | None] = mapped_column(String(16), nullable=True)
