from __future__ import annotations

import uuid

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class AgreementCounterparty(TimestampMixin, Base):
    """A legal party that signs a public agreement without becoming a user
    or referral partner company."""

    __tablename__ = "agreement_counterparties"
    __table_args__ = (
        UniqueConstraint(
            "normalized_legal_name",
            "normalized_state_of_formation",
            name="uq_agreement_counterparty_name_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_legal_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    state_of_formation: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_state_of_formation: Mapped[str] = mapped_column(String(80), nullable=False)
    principal_business_address: Mapped[str] = mapped_column(String(512), nullable=False)
    signer_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
