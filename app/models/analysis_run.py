from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class AnalysisRun(TimestampMixin, Base):
    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    property_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("property_intelligence_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    prequal_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prequal_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    tool_source: Mapped[str] = mapped_column(String(32), nullable=False, default="deal_analyzer")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", index=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False, default="Analysis run")
    target_property_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    calculator_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ai_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sanitized_client_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    report_version: Mapped[int] = mapped_column(default=1, nullable=False)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shared_by_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
