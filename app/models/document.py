from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import DocStatus
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan


class Document(TimestampMixin, Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE")
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Legacy free-form vault tab — "experience" / "active_asset" / etc.
    # Distinct from `checklist_key`; both can be set on the same row.
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[DocStatus] = mapped_column(String(32), default=DocStatus.PENDING)

    requested_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Per-loan override of the computed due date (alembic 0022).
    # NULL = the cron computes `requested_on + due_offset_days` using
    # the per-loan-type checklist defaults. Non-NULL wins — set via
    # PATCH /documents/{id} from the loan detail's Workflow tab.
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 'ai' | user_id

    # ── Categorization + AI scanning (alembic 0017) ─────────────────────
    #
    # `checklist_key` matches `DocChecklistItem.name` for the parent
    # loan's product. Natural key — checklist items don't have ids.
    # Set on `kickoff_loan` rows + on borrower uploads that pick a
    # checklist item. Null on legacy / "Other" rows.
    checklist_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # True when the borrower deliberately picked "Other / not in checklist".
    # Distinct from `checklist_key=None` which means legacy / unclassified.
    is_other: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # AI vision scan state. `ai_scan_status` is independent of
    # `status` (the workflow state) so we don't conflate them.
    #   'unscanned' — never been scanned (default for new rows)
    #   'queued'    — flagged scan_dirty=True, waiting for the drain
    #   'scanning'  — picked up by the drain, mid-flight
    #   'scanned'   — completed; ai_notes + confidence populated
    #   'failed'    — exception during scan; manual retry needed
    ai_scan_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unscanned", server_default="unscanned"
    )
    ai_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_scan_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    # Flipped True on upload-complete; the scheduler's
    # job_document_scan_drain picks up scan_dirty=True rows.
    scan_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    loan: Mapped[Loan] = relationship(back_populates="documents")
