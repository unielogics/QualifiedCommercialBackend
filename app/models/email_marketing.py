"""Address-level subscription, consent provenance, campaigns and delivery claims."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MarketingSubscriber(Base):
    __tablename__ = "email_marketing_subscribers"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    locale: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketingEnrollment(Base):
    __tablename__ = "email_marketing_enrollments"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    subscriber_id: Mapped[UUID] = mapped_column(ForeignKey("email_marketing_subscribers.id"), index=True)
    source: Mapped[str] = mapped_column(String(120))
    program: Mapped[str | None] = mapped_column(String(120))
    outcome: Mapped[str | None] = mapped_column(String(120))
    consent_version: Mapped[str] = mapped_column(String(48))
    client_id: Mapped[UUID | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    intake_id: Mapped[UUID | None] = mapped_column(ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MarketingCampaign(Base):
    __tablename__ = "email_marketing_campaigns"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    name: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[dict] = mapped_column(JSON)
    selection: Mapped[dict | None] = mapped_column(JSON)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketingDelivery(Base):
    __tablename__ = "email_marketing_deliveries"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "purpose_key", name="uq_email_marketing_delivery"),
        Index("ix_email_marketing_due", "status", "due_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    subscriber_id: Mapped[UUID] = mapped_column(ForeignKey("email_marketing_subscribers.id"))
    campaign_id: Mapped[UUID | None] = mapped_column(ForeignKey("email_marketing_campaigns.id"), index=True)
    purpose_key: Mapped[str] = mapped_column(String(48))
    step: Mapped[int | None] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    message_send_id: Mapped[UUID | None] = mapped_column(ForeignKey("message_sends.id", ondelete="SET NULL"))


class MarketingPermissionBatch(Base):
    __tablename__ = "email_marketing_permission_batches"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fingerprint: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(20))
    task_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_count: Mapped[int] = mapped_column(Integer, default=0)


class MarketingPermissionRequest(Base):
    __tablename__ = "email_marketing_permission_requests"
    __table_args__ = (Index("ix_marketing_permission_due", "status", "due_at"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    locale: Mapped[str] = mapped_column(String(2))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("email_marketing_permission_batches.id"))
    invite_batch_id: Mapped[UUID | None] = mapped_column(ForeignKey("email_marketing_permission_batches.id"))
    status: Mapped[str] = mapped_column(String(16))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_send_id: Mapped[UUID | None] = mapped_column(ForeignKey("message_sends.id", ondelete="SET NULL"))
    task_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("calendar_events.id", ondelete="SET NULL"))
