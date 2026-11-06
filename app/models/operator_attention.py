"""Shared desk-only queue dispositions and their append-only action history."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OperatorAttentionControl(Base):
    __tablename__ = "operator_attention_controls"
    __table_args__ = (
        CheckConstraint("status IN ('active','archived','dismissed','duplicate')", name="ck_attention_control_status"),
        CheckConstraint("revision >= 0", name="ck_attention_control_revision"),
        CheckConstraint("(status = 'duplicate' AND duplicate_of_key IS NOT NULL AND duplicate_of_key <> item_key AND duplicate_snapshot IS NOT NULL) OR (status <> 'duplicate' AND duplicate_of_key IS NULL)", name="ck_attention_control_duplicate"),
        Index("ix_operator_attention_controls_status", "status"),
    )

    item_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    duplicate_of_key: Mapped[str | None] = mapped_column(String(120), ForeignKey("operator_attention_controls.item_key", ondelete="RESTRICT"))
    duplicate_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String(1000))
    actor_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    actor_name: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OperatorAttentionEvent(Base):
    __tablename__ = "operator_attention_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operator_attention_event_retry"),
        UniqueConstraint("item_key", "revision", name="uq_operator_attention_event_revision"),
        CheckConstraint("action IN ('archive','dismiss','duplicate','restore')", name="ck_attention_event_action"),
        CheckConstraint("revision > 0", name="ck_attention_event_revision"),
        Index("ix_operator_attention_events_item_time", "item_key", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(120), ForeignKey("operator_attention_controls.item_key", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1000))
    duplicate_of_key: Mapped[str | None] = mapped_column(String(120))
    duplicate_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
