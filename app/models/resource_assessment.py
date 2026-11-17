"""A consented public tool result; its capability never authorizes an application."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ResourceAssessment(Base):
    __tablename__ = "resource_assessments"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("clients.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    goal: Mapped[str] = mapped_column(String(40), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(80), nullable=False)
    contact: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attribution: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    email_marketing_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_version: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    application_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nurture_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    application_intake_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intakes.id"), nullable=True)
    resource_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
