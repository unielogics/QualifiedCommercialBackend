"""Signatures on file — one live signature per subject, placed on program
agreements on that subject's behalf.

Subjects: a team member / relationship manager (``user``), a sponsor company
(``company`` = referral_partner_companies.id) and Qualified Commercial itself
(``qc``, subject_id NULL). The dealer never has one: the client signs fresh
every time.

Rows are append-only evidence. Re-adopting revokes the previous live row
first (partial unique on the live pair); revoking never touches documents a
signature was already placed on — those placements carry the stored
signature id and its adoption date on their certificate.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

STORED_SIGNATURE_SUBJECT_TYPES: tuple[str, ...] = ("user", "company", "qc")
STORED_SIGNATURE_SOURCES: tuple[str, ...] = ("self_adopted", "agreement", "admin_recorded", "letterhead")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_ref() -> Mapped[uuid.UUID | None]:
    return mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


class StoredSignature(TimestampMixin, Base):
    __tablename__ = "stored_signatures"
    __table_args__ = (
        Index(
            "uq_stored_signatures_live",
            "subject_type",
            "subject_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # user | company | qc
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # users.id | referral_partner_companies.id | NULL for qc
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    typed_name: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str | None] = mapped_column(String(120))
    signature_s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    signature_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # self_adopted | agreement | admin_recorded | letterhead
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    source_agreement_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_agreements.id", ondelete="SET NULL")
    )
    adoption_consent_version: Mapped[str | None] = mapped_column(String(32))
    adopted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    adopted_by_user_id: Mapped[uuid.UUID | None] = _user_ref()
    adopted_ip: Mapped[str | None] = mapped_column(String(64))
    adopted_user_agent: Mapped[str | None] = mapped_column(String(400))
    # Admin attestation for company signatures (why this signature may be placed).
    authorization_note: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = _user_ref()

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None
