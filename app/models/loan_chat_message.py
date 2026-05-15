from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import DealChatRole
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class LoanChatMessage(TimestampMixin, Base):
    """Persisted per-loan AI conversation in the Deal Workspace.

    Distinct from `messages` (which is the email/SMS conversation thread).
    `client_visible=false` is used for broker-internal Q&A turns so brokers
    can ask the AI things about a deal without polluting the client-facing
    chat. `from_role` follows DealChatRole.
    """

    __tablename__ = "loan_chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_role: Mapped[DealChatRole] = mapped_column(String(32), nullable=False)
    from_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    client_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional file attachment (alembic 0056). Lets a client send a
    # note + document in one chat turn.
    attachment_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
