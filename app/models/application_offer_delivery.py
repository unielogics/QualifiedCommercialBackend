"""Immutable client offer-package deliveries.

The source term sheets remain the desk's editable/versioned records.  These
rows are the exact client-facing evidence: the email, the bytes attached to
it, the deadline, and any answer against that exact hash/version.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class ApplicationOfferDelivery(TimestampMixin, Base):
    __tablename__ = "application_offer_deliveries"
    __table_args__ = (
        Index("ix_application_offer_deliveries_profile_sent", "profile_id", "sent_at"),
        CheckConstraint(
            "status IN ('sending','sent','partially_decided','completed','expired','superseded','failed')",
            name="ck_application_offer_delivery_status",
        ),
        CheckConstraint(
            "reconciliation_outcome IS NULL OR reconciliation_outcome IN ('provider_accepted','confirmed_not_sent')",
            name="ck_application_offer_delivery_reconciliation_outcome",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    room_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_upload_links.id", ondelete="SET NULL")
    )
    room_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    access_passcode_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dos_rep_inbox_threads.id", ondelete="SET NULL"),
    )
    message_send_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("message_sends.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="sending", server_default="sending"
    )
    to_contact_id: Mapped[str] = mapped_column(String(80), nullable=False)
    cc_contact_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    recipient_emails: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    cc_emails: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    personal_message: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    draft_fingerprint: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(24))
    provider_correlation_id: Mapped[str] = mapped_column(
        String(320), nullable=False, unique=True, index=True
    )
    provider_handoff_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(320))
    provider_detail: Mapped[str | None] = mapped_column(Text)
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effects_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reconciliation_outcome: Mapped[str | None] = mapped_column(String(24))
    reconciliation_attestation: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[ApplicationOfferDeliveryItem]] = relationship(
        back_populates="delivery",
        cascade="all, delete-orphan",
        order_by="ApplicationOfferDeliveryItem.created_at",
    )


class ApplicationOfferDeliveryItem(TimestampMixin, Base):
    __tablename__ = "application_offer_delivery_items"
    __table_args__ = (
        UniqueConstraint("delivery_id", "item_key", name="uq_application_offer_delivery_item_key"),
        Index("ix_application_offer_delivery_items_source", "kind", "source_id", "source_version"),
        CheckConstraint(
            "kind IN ('merchant_offer','production_term_sheet','application_term_sheet','evidence_file')",
            name="ck_application_offer_delivery_item_kind",
        ),
        CheckConstraint(
            "decision_status IN ('pending','accepted','declined','expired','superseded','not_applicable')",
            name="ck_application_offer_delivery_item_decision",
        ),
        CheckConstraint("char_length(sha256) = 64", name="ck_application_offer_delivery_item_sha"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_offer_deliveries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_key: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_version: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(180), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    canonical_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    file_name: Mapped[str] = mapped_column(String(240), nullable=False)
    content_type: Mapped[str] = mapped_column(String(160), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(700))
    # Durable fail-safe for installations without object storage.  This is the
    # same encrypted-at-rest database used by issued application term sheets.
    document_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_name: Mapped[str | None] = mapped_column(String(180))
    response_reason: Mapped[str | None] = mapped_column(Text)
    response_channel: Mapped[str | None] = mapped_column(String(24))
    response_ip: Mapped[str | None] = mapped_column(String(80))
    response_user_agent: Mapped[str | None] = mapped_column(String(500))
    response_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    response_attestation: Mapped[str | None] = mapped_column(Text)

    delivery: Mapped[ApplicationOfferDelivery] = relationship(back_populates="items")
