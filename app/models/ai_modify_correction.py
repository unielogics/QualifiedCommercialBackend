from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class AIModifyCorrection(TimestampMixin, Base):
    """Super-admin correction attached to a specific AI turn in the Deal
    Workspace chat ("you should have asked X instead").

    Surfaced back into the AI's system prompt (when the audience is
    super_admin) so future replies on the same loan reflect the correction.
    """

    __tablename__ = "ai_modify_corrections"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loan_chat_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    correction: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
