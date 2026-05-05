from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import FeedbackOutputType, FeedbackRating
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class AIFeedback(TimestampMixin, Base):
    """One operator's vote (and optional comment) on a single AI output.

    Polymorphic by (output_type, output_id). `loan_id` is denormalized so
    the prompt-assembly pass can quickly pull "recent negative feedback for
    this loan" without joining through ai_tasks/email_drafts/etc.

    Unique on (output_type, output_id, created_by) so a second vote from
    the same user replaces (upserts) the prior row.
    """

    __tablename__ = "ai_feedback"
    __table_args__ = (
        UniqueConstraint("output_type", "output_id", "created_by", name="uq_ai_feedback_actor"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    output_type: Mapped[FeedbackOutputType] = mapped_column(String(32), nullable=False, index=True)
    output_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=True, index=True
    )
    rating: Mapped[FeedbackRating] = mapped_column(String(8), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
