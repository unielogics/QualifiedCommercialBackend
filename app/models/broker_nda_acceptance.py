from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class BrokerNdaAcceptance(TimestampMixin, Base):
    """A dealer-partner's e-signed non-disclosure / non-solicitation
    agreement, captured at signup and hard-blocking platform access until
    signed (see `_require_nda_signed` in dealer_ai_intake.py). Mirrors
    BucketDocumentSignature's typed-name + canvas-signature + rendered-
    certificate evidentiary pattern, but is user-scoped rather than
    bucket/requested-document-scoped since a team-invite signature has no
    client file to attach to.

    One row per signing event (mirrors LegalAcceptance's "one row per
    acceptance event, duplicates are cheap" philosophy). `users.nda_signed_at`
    is denormalized from the latest row here so every hot-path check (the
    AppShell gate, every broker endpoint) is a single-column read."""

    __tablename__ = "broker_nda_acceptances"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    document_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    typed_name: Mapped[str] = mapped_column(String(160), nullable=False)
    esign_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    signature_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signature_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    certificate_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    certificate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The broker's own free-text carve-out of prior/pre-existing relationships
    # (lenders, dealers, etc.) they want excluded from the NDA's non-solicit
    # scope. Captured as-is, part of the signed certificate — no admin
    # approval gate (business decision: accepted at face value, disputed
    # later like any other contract term if needed).
    prior_relationships_disclosure: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
