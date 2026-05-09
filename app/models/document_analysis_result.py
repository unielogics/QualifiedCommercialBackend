"""DocumentAnalysisResult — persisted vision-scan extraction.

Today the vision scan runs synchronously during chat upload but only
writes results into the chat context. Persisting the structured
extraction here unlocks contradiction detection across the deal
lifetime — when a new analysis lands, the contradiction detector
compares its `extracted_facts` against prior facts (other docs +
realtor profile + chat-captured) and surfaces conflicts as
ChatActions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


from app.db import Base


class DocumentAnalysisResult(Base):
    __tablename__ = "document_analysis_results"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    detected_document_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)

    extracted_facts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Structured key/value extraction. e.g. {"buyer_name": "Marcus",
    "purchase_price": 875000}."""

    issues: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    """Contradictions / quality issues. List of
    {type, field, chat_value, document_value, severity}."""

    recommended_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """confirm_update_purchase_price | reject | request_correct_doc."""

    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    analyzer_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="v1", server_default="v1",
    )
