"""ClientAIPlan — the AI's resolved active plan for one client/deal.

This is the rolled-up view the UI renders + every AI turn consumes.
Recomputed by `app.services.ai.plan_builder.rebuild()` on chat turns,
document uploads, and cadence-engine passes. The individual-item
source of truth is `client_requirement_status`; this row is the
playbook-resolved + override-applied snapshot.

Pinned `active_playbook_versions` mean an in-flight deal is insulated
from playbook edits — if the funding team publishes DSCR Purchase v2,
existing deals stay on v1 until manually re-pinned. New deals pick
up the latest published version.

Partial unique indexes (created in alembic 0032) prevent duplicates:
one realtor-phase row per client (loan_id IS NULL) and one row per
(client_id, loan_id) pair.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ClientAIPlan(TimestampMixin, Base):
    __tablename__ = "client_ai_plan"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    current_phase: Mapped[str] = mapped_column(String(16), nullable=False)
    """realtor | lending."""

    active_playbook_versions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    """Pinned playbooks for this deal: [{playbook_id, version}, ...]."""

    custom_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Agent's free-text per-client guidance (e.g. "don't ask for
    buyer agreement yet — first collect property criteria")."""

    required_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    """The resolved active list — what the AI is currently chasing.
    Each entry: {requirement_key, label, category, required_level,
    status, source, blocks_stage, visibility}."""

    waived_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    """Items the agent or underwriter explicitly waived/marked NA."""

    ai_suggested_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    """Items the AI proposed adding (low-confidence, awaiting approval)."""

    next_best_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_best_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    readiness_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    # ── Phase 6 (alembic 0033): Deal Intelligence extensions ──────
    source_realtor_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
        nullable=True,
    )
    lending_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
        nullable=True,
    )
    handoff_packet_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("lending_handoff_packets.id", ondelete="SET NULL"),
        nullable=True,
    )
    verified_facts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    unverified_facts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    document_conflicts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    agent_visibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    borrower_visibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    funding_visibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
