"""Auto-drafted outbound email awaiting broker approval.

Populated by the Fintech Orchestrator when an inbound Lender request is
detected and the AI generates a Broker/Client-facing summary. The broker
sees these in a "Pending review" inbox and clicks Approve to send.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import EmailDraftStatus
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan


class EmailDraft(TimestampMixin, Base):
    __tablename__ = "email_drafts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Where the draft is being sent.
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    cc_emails: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    bcc_emails: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    # Subject (already prefixed with [QC-{deal_id}] by the relay).
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[EmailDraftStatus] = mapped_column(String(32), default=EmailDraftStatus.PENDING, nullable=False)

    # Provenance — link back to the inbound message that triggered this draft.
    triggered_by_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """e.g. 'inbound_lender_request', 'doc_received', 'auto_followup'"""
    triggered_by_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # When the broker actions it, store who + when.
    actioned_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sent_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    """Gmail message id, populated after send."""

    loan: Mapped[Loan] = relationship(back_populates="email_drafts")
