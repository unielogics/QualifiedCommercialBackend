"""A profit-and-loss statement or a balance sheet typed into our form.

Its own table, deliberately. A `kind` column on `financial_statements` would
have let `_statement_or_404` and the by-id PFS routes accept a balance-sheet
id, and `FinancialFormLink.statement_id` points only at the PFS table. The two
business statements carry no statement id on their links: a link of either
kind resolves the latest row of that kind on the file.

One live row per (profile, kind): a save updates the latest row of that kind
or creates one; status moves draft → submitted on submit and stays submitted
on later edits, the same rule the PFS follows; a re-submit files a fresh PDF.
The derived columns are written from `business_statement_schema.totals()` on
every save, so the figures the panel and the extractors read never depend on
walking JSONB.

Anchored to `ApplicationProfile` for the same reason the PFS is: it is the one
key that spans intake, deal, loan, client and dealer files.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class BusinessFinancialStatement(TimestampMixin, Base):
    __tablename__ = "business_financial_statements"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: p_and_l — a profit and loss statement for a period.
    #: balance_sheet — assets, liabilities and equity as of one date.
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Which field set `body` was written against (qc_pl.v1 / qc_bs.v1).
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The period a P&L covers; blank on a balance sheet.
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    #: The date a balance sheet is stated as of; blank on a P&L.
    as_of_date: Mapped[date | None] = mapped_column(Date)
    #: The form itself: header, sections of typed strings, notes.
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: Derived on every save from `totals()`. A P&L fills the first three, a
    #: balance sheet the last three; the rest stay null.
    gross_revenue: Mapped[float | None] = mapped_column(Numeric(16, 2))
    net_income: Mapped[float | None] = mapped_column(Numeric(16, 2))
    ebitda: Mapped[float | None] = mapped_column(Numeric(16, 2))
    total_assets: Mapped[float | None] = mapped_column(Numeric(16, 2))
    total_liabilities: Mapped[float | None] = mapped_column(Numeric(16, 2))
    total_equity: Mapped[float | None] = mapped_column(Numeric(16, 2))

    #: draft — saved, can be resumed. submitted — a PDF has been filed on the
    #: checklist; later edits keep the status.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Null when the borrower submitted it through their own link; set when
    #: staff completed it on their behalf.
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: The PDF filed at the last submit.
    bucket_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "kind in ('p_and_l','balance_sheet')", name="ck_business_financial_statements_kind"
        ),
        CheckConstraint(
            "status in ('draft','submitted')", name="ck_business_financial_statements_status"
        ),
        Index(
            "ix_business_financial_statements_profile_kind_status",
            "profile_id",
            "kind",
            "status",
        ),
    )
