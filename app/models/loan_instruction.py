from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class LoanInstruction(TimestampMixin, Base):
    """Persistent loan-scoped directive ("always cc legal").

    Authored by super-admin or broker from the Deal Workspace chat in
    'instruct' mode. Active rows are injected into every AI prompt assembled
    for this loan via services/ai/context.assemble_loan_context().
    """

    __tablename__ = "loan_instructions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
