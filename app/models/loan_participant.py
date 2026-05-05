"""Per-loan thread participant.

This is the table the operator manages from the Loan Detail UI to control
who is in the email loop for a given deal. It powers:

- Fintech Orchestrator privacy: Lender PII is scrubbed from anything sent to
  participants whose role is BROKER or CLIENT.
- Outbound BCC: every SUPER_ADMIN row in this table is added to the BCC
  line on outbound mail for that loan (audit trail).
- Notifications: the broker is CC'd, the client gets simplified versions.

Frontend-managed — never read from .env.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import ParticipantRole
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan


class LoanParticipant(TimestampMixin, Base):
    __tablename__ = "loan_participants"
    __table_args__ = (
        # Same email shouldn't appear twice on the same loan. Distinct loans
        # may share a participant (e.g. an admin BCC'd on every deal).
        UniqueConstraint("loan_id", "email", name="uq_loan_participants_loan_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    role: Mapped[ParticipantRole] = mapped_column(String(32), nullable=False)
    company: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # Outbound knobs — set per row from the UI:
    cc_outbound: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Visibly CC on outbound mail. Other recipients see this address.
    Default True for BROKER and CLIENT rows."""

    bcc_outbound: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Silently BCC on every outbound mail. Other recipients do not see this
    address. Default True for SUPER_ADMIN rows (audit trail)."""

    # Identity protection — for Lender rows, default True. Anyone toggling this
    # off should be a deliberate operator choice. Never exposed to non-admins.
    hide_identity: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """If True, this person's name/email is replaced with 'The Lender' or
    'The Lead Underwriter' in any text shown to BROKER/CLIENT participants."""

    loan: Mapped[Loan] = relationship(back_populates="participants")
