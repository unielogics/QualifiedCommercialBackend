"""Persisted AI Underwriter conversation threads.

The standalone "Intelligent Underwriter" chat (mobile FAB / desktop
topbar icon) used to live entirely in component state — close the
panel and history was gone. This module holds the thread + message
tables that back a real conversation history per user.

Distinct from `loan_chat_messages` (per-deal Co-pilot rail, scoped
to a loan) and from `messages` (email / SMS to borrowers). This is
the user's personal AI conversation surface, not tied to any one
loan.

Cascade: deleting a User removes their threads; deleting a thread
removes its messages.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan
    from app.models.user import User


class AIChatThread(TimestampMixin, Base):
    __tablename__ = "ai_chat_threads"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Loan-scoped thread when set; account-wide ("Account questions")
    # when null. Partial unique idx on (user_id, loan_id) WHERE
    # loan_id IS NOT NULL gives us one canonical thread per
    # (user, loan) pair while leaving account threads unconstrained.
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="New conversation")
    last_message_preview: Mapped[str | None] = mapped_column(String(240), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped to now() whenever the borrower opens the thread (via
    # POST /threads/{id}/seen). NULL = never opened — UI treats
    # every assistant message as unread until first view.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    loan: Mapped["Loan | None"] = relationship(foreign_keys=[loan_id])

    messages: Mapped[list["AIChatMessage"]] = relationship(
        "AIChatMessage",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="AIChatMessage.created_at.asc()",
    )


class AIChatMessage(TimestampMixin, Base):
    __tablename__ = "ai_chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_chat_threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 'user' or 'assistant' — same shape Anthropic expects when we
    # replay history into the model.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Structured CTAs the frontend renders as buttons under the
    # bubble. Shape: list[ChatAction] (see app/routers/ai.py).
    # Always None for user messages.
    actions: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    # Files attached to this message. Shape:
    # [{document_id, name, content_type, status, suggested_checklist_key}].
    # User messages carry uploads from the chat composer; assistant
    # messages may reference docs the AI scanned/produced.
    attachments: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)

    thread: Mapped["AIChatThread"] = relationship(
        "AIChatThread", back_populates="messages"
    )
