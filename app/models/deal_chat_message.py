from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import DealChatRole
from app.models._mixins import TimestampMixin


class DealChatMessage(TimestampMixin, Base):
    """Multi-party (A) thread per Deal — broker + client + AI share it.

    Mirrors LoanChatMessage but keyed on deal_id. Lives pre-promotion;
    after promotion the chat persists for ongoing agent nurture while
    the funding-team conversation moves to loan_chat_messages (L).
    """

    __tablename__ = "deal_chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("deals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_role: Mapped[DealChatRole] = mapped_column(String(32), nullable=False)
    from_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    client_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
