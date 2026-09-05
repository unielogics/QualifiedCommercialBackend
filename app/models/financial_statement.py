"""A Personal Financial Statement that survives being submitted.

Until now the numbers a borrower typed into the on-screen PFS were rendered
into a PDF and thrown away — deliberately, as data minimisation. That choice
cost more than it saved: a form could not be reopened, corrected or resumed,
staff could not finish one on a client's behalf, and the statement was tied to a
person only by a free-text name, so it could never be linked to an applicant at
all, let alone to two.

Keeping the rows is what makes all of that possible. The privacy reasoning is
answered rather than discarded: this still collects no SSN (see
`app.services.pfs_schema`), and the body holds what a lender's Form 413 asks
for and nothing beyond it.

Anchored to `ApplicationProfile` because that is the only key that spans intake,
deal, loan, client and dealer files — a statement attached to a bucket or a
dealer would be invisible on the other four.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class FinancialStatement(TimestampMixin, Base):
    __tablename__ = "financial_statements"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    statement_date: Mapped[date | None] = mapped_column(Date)
    #: Which field set `body` was written against, so an old statement can still
    #: be rendered after the form grows.
    schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sba413.v1", server_default="sba413.v1"
    )
    #: The form itself. JSONB rather than a table per section: Form 413 is seven
    #: schedules of ragged rows, and the numbers anything else reads are the
    #: derived columns below, not the individual lines.
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: Derived on every save. Stored rather than computed on read because these
    #: feed `key_facts`, and an underwriting metric should not depend on walking
    #: a JSON document.
    total_assets: Mapped[float | None] = mapped_column(Numeric(16, 2))
    total_liabilities: Mapped[float | None] = mapped_column(Numeric(16, 2))
    net_worth: Mapped[float | None] = mapped_column(Numeric(16, 2))
    liquid_assets: Mapped[float | None] = mapped_column(Numeric(16, 2))

    #: draft — saved but not submitted, so it can be resumed or finished by
    #: staff. submitted — the PDF has been generated and the checklist slot is
    #: satisfied.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Null when the borrower submitted it themselves through a link. Set when a
    #: staff member filled it in on their behalf, which the audit trail should
    #: be able to tell apart.
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: The generated PDF, so the staff view can show the artifact beside the
    #: data it came from.
    bucket_file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    owners: Mapped[list[FinancialStatementOwner]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("status in ('draft','submitted')", name="ck_financial_statements_status"),
        Index("ix_financial_statements_profile_status", "profile_id", "status"),
    )


class FinancialStatementOwner(Base):
    """Which applicants a statement speaks for.

    A joint statement — a married couple filing one sheet — is one statement
    linked to two people, so this is a list rather than a column on the
    statement.

    Two nullable owner columns, exactly one set, because owners genuinely live
    in two tables: `application_owners` for profile-backed files and `dos_owners`
    for dealer-backed ones, reconciled at read time by
    `app.services.application_profiles.owner_rows`. A single polymorphic
    `owner_id` would be simpler to write and would have no foreign key behind
    it, so a deleted owner would leave a statement pointing at nothing. This
    mirrors how `ApplicationProfile` already carries several mutually exclusive
    source FKs.
    """

    __tablename__ = "financial_statement_owners"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    statement_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_statements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    application_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_owners.id", ondelete="CASCADE")
    )
    dealer_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("dos_owners.id", ondelete="CASCADE")
    )

    statement: Mapped[FinancialStatement] = relationship(back_populates="owners")

    __table_args__ = (
        CheckConstraint(
            "(application_owner_id is not null)::int + (dealer_owner_id is not null)::int = 1",
            name="ck_financial_statement_owners_exactly_one",
        ),
        UniqueConstraint(
            "statement_id", "application_owner_id", name="uq_statement_application_owner"
        ),
        UniqueConstraint("statement_id", "dealer_owner_id", name="uq_statement_dealer_owner"),
    )
