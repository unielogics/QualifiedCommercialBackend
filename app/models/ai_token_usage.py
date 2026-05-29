"""AITokenUsage — append-only ledger of every LLM call's token cost.

One row per Anthropic completion, written best-effort from the
orchestrator's `resp.usage`. Powers the super-admin token-usage
report: spend per file, per AI agent, per activity, per broker, per
model, over time.

Dimension columns are plain UUIDs (no FK) on purpose — this is an
analytics log, so deleting a loan/agent must NOT wipe its historical
spend, and we never need referential integrity here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AITokenUsage(Base):
    __tablename__ = "ai_token_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    model: Mapped[str] = mapped_column(String(64), nullable=False)
    activity: Mapped[str] = mapped_column(
        String(48), nullable=False, default="other", server_default="other", index=True
    )
    """Call-site label: loan_chat | deal_chat | airail_chat |
    ai_agent_compose | playbook_synthesis | showing_guide | training |
    summarizer | client_summary | lender_extract | reengagement |
    doc_scan | knowledge_classify | other."""

    # Dimension tags (plain UUIDs — no FK; ledger survives deletions).
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    ai_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )
    broker_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, index=True
    )

    # Token buckets straight from Anthropic usage.
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=0, server_default="0"
    )
