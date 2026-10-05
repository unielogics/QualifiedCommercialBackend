"""Durable Dealer Prospect outreach, collateral, suppression, and replies.

The prospect/stage models live in ``dealer_prospect.py``.  This module is
deliberately separate: marketing delivery has a different retention and
concurrency lifecycle, and queued drafts must retain the exact collateral
bytes even after an administrator retires or replaces an asset.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

DRAFT_STATUSES = (
    "pending_review",
    "editing",
    "sending",
    "sent",
    "cancelled",
    "failed",
    "blocked",
)
COLLATERAL_STATUSES = ("pending_approval", "active", "retired")
OUTREACH_PROFILE_STATUSES = ("active", "retired")
SUPPRESSION_REASONS = (
    "unsubscribe",
    "bounce",
    "complaint",
    "bad_address",
    "administrative",
)


class DealerProspectEmailDraft(TimestampMixin, Base):
    """One immutable-at-dispatch email with a durable review deadline.

    ``sending`` is intentionally terminal for automatic processing.  A worker
    commits that state *before* calling SES.  If the process dies after SES
    accepts the message, a later scheduler tick will not send it twice; an
    operator must reconcile the ambiguous row from the provider ledger.
    """

    __tablename__ = "dealer_prospect_email_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review','editing','sending','sent','cancelled','failed','blocked')",
            name="ck_dealer_prospect_email_draft_status",
        ),
        CheckConstraint("version >= 1", name="ck_dealer_prospect_email_draft_version"),
        CheckConstraint(
            "delivery_mode IN ('attachments','secure_link')",
            name="ck_dealer_prospect_email_draft_delivery_mode",
        ),
        CheckConstraint(
            "compose_mode IN ('ai','manual')",
            name="ck_dealer_prospect_email_draft_compose_mode",
        ),
        CheckConstraint(
            "char_length(outreach_profile_hash) = 64",
            name="ck_dealer_prospect_email_draft_profile_hash",
        ),
        CheckConstraint(
            "outreach_profile_version >= 1",
            name="ck_dealer_prospect_email_draft_profile_version",
        ),
        CheckConstraint(
            "(collateral_bundle_id IS NULL AND collateral_bundle_version IS NULL) OR "
            "(collateral_bundle_id IS NOT NULL AND collateral_bundle_version >= 1)",
            name="ck_dealer_prospect_email_draft_bundle_identity",
        ),
        Index("ix_dealer_prospect_email_drafts_due", "status", "auto_send_at"),
        Index("ix_dealer_prospect_email_drafts_prospect_created", "prospect_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dealer_prospects.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    cc_emails: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    from_name: Mapped[str] = mapped_column(String(160), nullable=False)
    reply_to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    reply_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    unsubscribe_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    rfc_message_id: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)

    subject: Mapped[str] = mapped_column(String(240), nullable=False)
    # The UI may edit only this portion.  ``locked_footer_text`` is rebuilt
    # onto ``body_text`` after every edit so the website, signature, physical
    # address, disclosure, and unsubscribe link cannot be removed.
    editable_body: Mapped[str] = mapped_column(Text, nullable=False)
    locked_footer_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    compose_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ai", server_default="ai"
    )
    purpose: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        default="dealer_information",
        server_default="dealer_information",
    )
    # The source prospect remains mutable.  Delivery therefore relies on this
    # immutable audience/profile snapshot, and separately verifies that the
    # prospect has not changed audience after review began.
    lead_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="dealer", server_default="dealer"
    )
    funding_intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outreach_profile_key: Mapped[str] = mapped_column(
        String(32), nullable=False, default="dealer", server_default="dealer"
    )
    outreach_profile_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    outreach_profile_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    outreach_profile_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    collateral_bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketing_collateral_bundles.id", ondelete="SET NULL"),
        nullable=True,
    )
    collateral_bundle_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collateral_bundle_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    draft_source: Mapped[str] = mapped_column(String(16), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Only source identifiers and hashes are retained.  Decrypted reply text
    # is assembled ephemerally for a single model request and is not copied.
    ai_context_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending_review", server_default="pending_review"
    )
    auto_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    attachment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attachment_total_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    delivery_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="attachments", server_default="attachments"
    )
    secure_bundle_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    secure_bundle_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    secure_bundle_selected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    secure_bundle_selected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    secure_bundle_downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProspectOutreachProfile(TimestampMixin, Base):
    """Versioned writing/audience policy for one Field Desk lead type.

    Updates create a new active version and retire the previous row.  Drafts
    copy the complete public policy into ``outreach_profile_snapshot`` so a
    later admin edit cannot silently change reviewed copy or links.
    """

    __tablename__ = "prospect_outreach_profiles"
    __table_args__ = (
        UniqueConstraint(
            "lead_type", "version", name="uq_prospect_outreach_profile_version"
        ),
        CheckConstraint(
            "status IN ('active','retired')",
            name="ck_prospect_outreach_profile_status",
        ),
        CheckConstraint(
            "version >= 1", name="ck_prospect_outreach_profile_version"
        ),
        Index(
            "uq_prospect_outreach_profile_active",
            "lead_type",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    desk_name: Mapped[str] = mapped_column(String(80), nullable=False)
    audience_label: Mapped[str] = mapped_column(String(120), nullable=False)
    audience_plural: Mapped[str] = mapped_column(String(120), nullable=False)
    website_url: Mapped[str] = mapped_column(String(500), nullable=False)
    drafting_guidance: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    purpose_templates: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class MarketingCollateralAsset(TimestampMixin, Base):
    """Admin-approved, versioned PDF collateral.

    The mutable library row is never read by the dispatcher.  Draft creation
    copies active assets into ``DealerProspectEmailDraftAsset`` below.
    """

    __tablename__ = "marketing_collateral_assets"
    __table_args__ = (
        UniqueConstraint(
            "assignment",
            "lead_type",
            "logical_key",
            "version",
            name="uq_marketing_collateral_version",
        ),
        CheckConstraint(
            "status IN ('pending_approval','active','retired')",
            name="ck_marketing_collateral_status",
        ),
        CheckConstraint("size_bytes > 0", name="ck_marketing_collateral_size"),
        CheckConstraint("char_length(sha256) = 64", name="ck_marketing_collateral_sha"),
        Index(
            "ix_marketing_collateral_active_order",
            "assignment",
            "lead_type",
            "status",
            "sort_order",
        ),
        Index(
            "uq_marketing_collateral_one_active_version",
            "assignment",
            "lead_type",
            "logical_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assignment: Mapped[str] = mapped_column(
        String(48), nullable=False, default="dealer_outreach", server_default="dealer_outreach"
    )
    lead_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="dealer", server_default="dealer"
    )
    # A PDF may be offered for several composer purposes.  ``information`` is
    # the canonical form of the legacy ``dealer_information`` alias.
    purposes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: ["information"],
        server_default=text("'[\"information\"]'::jsonb"),
    )
    included_by_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    logical_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending_approval", server_default="pending_approval"
    )
    file_name: Mapped[str] = mapped_column(String(240), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(80), nullable=False, default="application/pdf", server_default="application/pdf"
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketingCollateralBundle(TimestampMixin, Base):
    """Versioned collateral publication for one audience and email purpose."""

    __tablename__ = "marketing_collateral_bundles"
    __table_args__ = (
        UniqueConstraint(
            "lead_type",
            "purpose",
            "version",
            name="uq_marketing_collateral_bundle_version",
        ),
        CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_marketing_collateral_bundle_status",
        ),
        CheckConstraint("version >= 1", name="ck_marketing_collateral_bundle_version"),
        CheckConstraint("revision >= 1", name="ck_marketing_collateral_bundle_revision"),
        Index(
            "uq_marketing_collateral_bundle_published",
            "lead_type",
            "purpose",
            unique=True,
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
        Index(
            "ix_marketing_collateral_bundle_lookup",
            "lead_type",
            "purpose",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_type: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class MarketingCollateralBundleItem(Base):
    """Ordered asset identity frozen into a bundle publication version."""

    __tablename__ = "marketing_collateral_bundle_items"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id", "asset_id", name="uq_marketing_collateral_bundle_item_asset"
        ),
        UniqueConstraint(
            "bundle_id", "sort_order", name="uq_marketing_collateral_bundle_item_order"
        ),
        CheckConstraint(
            "inclusion_mode IN ('default','optional')",
            name="ck_marketing_collateral_bundle_item_mode",
        ),
        CheckConstraint("sort_order >= 0", name="ck_marketing_collateral_bundle_item_order"),
        CheckConstraint("size_bytes > 0", name="ck_marketing_collateral_bundle_item_size"),
        CheckConstraint(
            "char_length(sha256) = 64", name="ck_marketing_collateral_bundle_item_sha"
        ),
        Index("ix_marketing_collateral_bundle_items_order", "bundle_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bundle_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketing_collateral_bundles.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketing_collateral_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inclusion_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_name: Mapped[str] = mapped_column(String(180), nullable=False)
    asset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(String(240), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class MarketingCollateralAssetEvent(Base):
    """Append-only administrative history for one collateral version."""

    __tablename__ = "marketing_collateral_asset_events"
    __table_args__ = (
        Index("ix_marketing_collateral_events_asset_created", "asset_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketing_collateral_assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class DealerProspectEmailDraftAsset(Base):
    """Exact PDF snapshot attached to one queued prospect draft."""

    __tablename__ = "dealer_prospect_email_draft_assets"
    __table_args__ = (
        UniqueConstraint("draft_id", "asset_id", name="uq_prospect_draft_asset"),
        CheckConstraint("size_bytes > 0", name="ck_prospect_draft_asset_size"),
        CheckConstraint("char_length(sha256) = 64", name="ck_prospect_draft_asset_sha"),
        Index("ix_prospect_draft_assets_order", "draft_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dealer_prospect_email_drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketing_collateral_assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    asset_name: Mapped[str] = mapped_column(String(180), nullable=False)
    asset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(String(240), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    document_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class EmailSuppression(TimestampMixin, Base):
    """Global email suppression shared by every prospect send path."""

    __tablename__ = "email_suppressions"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('unsubscribe','bounce','complaint','bad_address','administrative')",
            name="ck_email_suppression_reason",
        ),
        Index("ix_email_suppressions_active", "active", "email_normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DealerProspectInboundReply(TimestampMixin, Base):
    """Encrypted-at-rest copy of a reply correlated to a prospect alias."""

    __tablename__ = "dealer_prospect_inbound_replies"
    __table_args__ = (
        UniqueConstraint("provider", "provider_message_id", name="uq_prospect_reply_provider_id"),
        Index("ix_prospect_replies_prospect_received", "prospect_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dealer_prospects.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dealer_prospect_email_drafts.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(320), nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    to_emails: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    body_text_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    encryption_provider: Mapped[str] = mapped_column(
        String(24), nullable=False, default="fernet", server_default="fernet"
    )
    in_reply_to: Mapped[str | None] = mapped_column(String(500), nullable=True)
    references: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
