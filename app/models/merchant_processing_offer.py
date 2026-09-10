"""A merchant-processing offer: the partner's terms, read by the system,
answered by the client, and told to the partner.

The processing partner prepares a pricing proposal for a client — what they
pay their card processor today, what they would pay with the partner. The
desk drops that PDF on the file; it becomes an ordinary `BucketFile` marked
as an offer document, the analysis pipeline reads it with a dedicated
prompt, and the numbers land here. The client sees the offer in their room
and accepts or declines; the answer is stamped on this row with the same
evidence a signature carries (time, IP, browser), and the partner is emailed
at once, with the delivery outcome stored beside the answer so a failed send
can never undo what the client said.

`terms` is what the client may see. `desk_terms` — the partner's residual,
commission or bonus lines — is never serialised to a client surface; the
client view is built from an allowlist, not by subtraction.

One current offer per file: the partial unique index below refuses a second
row that is neither superseded nor withdrawn.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class MerchantProcessingOffer(TimestampMixin, Base):
    __tablename__ = "merchant_processing_offers"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The partner's terms PDF, an ordinary bucket file carrying the offer marker.
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL"), nullable=True
    )
    #: The processing partner — a lender whose products include merchant_processing.
    lender_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("lenders.id", ondelete="SET NULL"), nullable=True
    )

    #: uploaded → extracted | unreadable → sent → accepted | declined;
    #: withdrawn (desk) and superseded (a newer upload) close a row.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="uploaded", server_default="uploaded")

    terms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    desk_terms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    #: Bumped on every desk edit; the client's answer names the version it saw.
    terms_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    estimated_monthly_savings: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    estimated_annual_savings: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    #: fees_diff | rate_x_volume | stated | manual
    savings_basis: Mapped[str | None] = mapped_column(String(24), nullable=True)
    extraction_confidence: Mapped[str | None] = mapped_column(String(12), nullable=True)
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: accepted | declined, with signature-grade evidence.
    client_response: Mapped[str | None] = mapped_column(String(16), nullable=True)
    client_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_response_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_response_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    client_response_ip: Mapped[str | None] = mapped_column(String(80), nullable=True)
    client_response_user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    disclaimer_version: Mapped[str | None] = mapped_column(String(24), nullable=True)

    #: sent | failed | skipped | blocked — the outcome of telling the partner.
    partner_email_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    partner_email_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    partner_email_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    partner_email_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_merchant_processing_offers_profile", "profile_id"),
        Index("ix_merchant_processing_offers_source_file", "source_file_id"),
        Index(
            "uq_merchant_processing_offers_current",
            "profile_id",
            unique=True,
            postgresql_where=text("status NOT IN ('superseded', 'withdrawn')"),
        ),
    )
