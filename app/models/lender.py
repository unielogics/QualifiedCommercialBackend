from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.loan import Loan


class Lender(TimestampMixin, Base):
    """A lending counter-party tracked by Qualified Commercial.

    The model is intentionally small — submission routing + a contact
    person + the products this lender funds is enough to drive the
    Connect-Lender flow and the Gateway Rule (One-Way Mirror)
    machinery in `app/services/email/`. Anything richer (rate sheets,
    matrices) lives in adjacent models.
    """

    __tablename__ = "lenders"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)

    # Loan-submission inbox — what we send packaged docs to.
    submission_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # Primary point of contact (a human at the lender). Distinct from
    # submission_email which is typically a shared inbox.
    contact_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_title: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Loan products this lender services. List of `LoanType` string
    # values (e.g. ["dscr", "fix_and_flip"]). Drives the
    # Connect-Lender dropdown filter.
    products: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    # Phase-2 hook: domain-based fallback for inbound matching when
    # the lender emails from a colleague's address that isn't in the
    # explicit participant list yet.
    email_domain: Mapped[str | None] = mapped_column(String(120), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft-delete: lets us hide a lender from new connections without
    # orphaning historical participant rows / activity log entries.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # Pre-existing JSONB blob — left untouched. Reserved for rate
    # matrices / product caps; out of scope for the v1 admin UI.
    matrix: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    loans: Mapped[list["Loan"]] = relationship(back_populates="lender")
