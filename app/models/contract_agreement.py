from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ContractAgreement(TimestampMixin, Base):
    """A single e-signed instance of one of the platform's real contract
    templates (see app/services/contract_templates.py) — supersedes and
    replaces the interim BrokerNdaAcceptance model. Mirrors that model's
    (and BucketDocumentSignature's) evidentiary shape: typed name, canvas
    signature, rendered certificate, document hash/version, server-captured
    IP/user-agent, signed_at.

    `subject_type`/`subject_id` is a polymorphic reference (mirrors the
    target_type/target_id string-discriminator pattern already used by this
    codebase's activity-log helper `_log(...)`) rather than two nullable FK
    columns, since a contract can be signed by either an individual User
    (Platform Access Agreement) or a ReferralPartnerCompany (Referral
    Protection Agreement) — no single FK column fits both, and a contract
    type is only ever signed by exactly one kind of subject, so there is no
    ambiguity in practice.

    One row per signing event. Whether a given subject currently "has" a
    valid agreement of a given type on file is always a query against this
    table (ORDER BY created_at DESC LIMIT 1), never a denormalized status
    column — consistent with how credit-authorization/broker-NDA status is
    already derived elsewhere in this codebase."""

    __tablename__ = "contract_agreements"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    contract_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    # The filled-in blanks (client legal name, requested amount, fee rates,
    # notice addresses, etc.) submitted at signing time — the same values
    # render_contract_document() used to produce the text that was hashed
    # and shown to the signer. Kept alongside the hash so a dispute can
    # reconstruct exactly what was filled in, not just that something was.
    field_values: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    document_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    typed_name: Mapped[str] = mapped_column(String(160), nullable=False)
    esign_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    signature_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signature_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    certificate_s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    certificate_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email_delivery_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email_delivery_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
