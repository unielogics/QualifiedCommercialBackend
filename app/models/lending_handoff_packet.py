"""Lending Handoff Packet — bridge object created when the agent
fires "Ready for Lending."

The packet is the connective tissue between the Realtor AI's
relationship work (Phase 1) and the Lending AI's loan intelligence
work (Phase 2). It carries:

  realtor_summary           the agent's intent_summary + relationship
                            stage at handoff time
  extracted_facts           structured facts the realtor AI captured
                            during conversation (mirror of the
                            client.realtor_profile at handoff time,
                            plus any chat-derived facts)
  missing_lending_items     what the Lending AI needs to collect
                            (e.g. borrower_entity_type, credit auth,
                            liquidity docs, property address)
  uploaded_document_refs    documents already collected, with a
                            relevant_to_lending flag per row
  recommended_lending_path  loan_type_guess + urgency + suggested
                            first_lending_question
  visibility_rules          per-fact visibility — internal_only |
                            agent_visible | client_visible | bank_visible.
                            Used by the client-facing AI (when wired)
                            to filter what's safe to surface.

The packet links both threads (realtor + lending) so we can navigate
back to the source conversation when needed for audit / context.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.ai_chat_thread import AIChatThread
    from app.models.client import Client
    from app.models.loan import Loan
    from app.models.prequal_request import PrequalRequest
    from app.models.user import User


class LendingHandoffPacket(TimestampMixin, Base):
    __tablename__ = "lending_handoff_packets"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    realtor_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
        nullable=True,
    )
    lending_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
        nullable=True,
    )
    prequal_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("prequal_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="SET NULL"),
        nullable=True,
    )

    handoff_reason: Mapped[str] = mapped_column(
        String(64), nullable=False,
        default="agent_marked_ready_for_lending",
        server_default="agent_marked_ready_for_lending",
    )
    handoff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    realtor_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    extracted_facts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    missing_lending_items: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    uploaded_document_refs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    recommended_lending_path: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    first_lending_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility_rules: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Relationships — disambiguated FKs because we have two AIChatThread
    # FKs (realtor + lending). Each relationship pins to one column.
    client: Mapped["Client"] = relationship(foreign_keys=[client_id])
    agent: Mapped["User | None"] = relationship(foreign_keys=[agent_id])
    realtor_thread: Mapped["AIChatThread | None"] = relationship(foreign_keys=[realtor_thread_id])
    lending_thread: Mapped["AIChatThread | None"] = relationship(foreign_keys=[lending_thread_id])
    prequal_request: Mapped["PrequalRequest | None"] = relationship(foreign_keys=[prequal_request_id])
    loan: Mapped["Loan | None"] = relationship(foreign_keys=[loan_id])
