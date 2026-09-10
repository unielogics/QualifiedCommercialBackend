"""The four financial forms as one live worksheet, and what a link opens of it.

The owner asked for "a google sheet type of interface" over the four forms —
profit and loss, balance sheet, business debt schedule, personal financial
statement — editable by the desk and shareable with an outsider (their example
was the accountant) who has no login.

**A worksheet stores no cells.** The figures stay where they already live:
`business_financial_statements.body`, `financial_statements.body`, and
`dos_debts` rows. A second copy would be a second truth, and the whole point of
typing into the grid is that underwriting reads the same number the accountant
typed. What this table adds is the one thing four separate documents cannot
have on their own: **a clock**.

`revision` is that clock — a single monotonic counter for the whole workbook,
bumped once per accepted batch of cell edits under `SELECT … FOR UPDATE` on
this row. It does two jobs at once:

- The lock serialises concurrent writers, which is what makes read-modify-write
  on a JSONB body safe. Two saves landing at once on `business_statements.save`
  today silently lose one; the grid, where a save is a keystroke, must not
  inherit that.
- The counter orders events. Every sheet payload carries the clock reading at
  which it last changed (`sheet_rev` on the two statement tables), so a client
  can tell a stale echo from news, and a client that has fallen a long way
  behind can be told to reload rather than fed a thousand deltas.

The debt schedule has no statement row of its own — its rows are `dos_debts`,
owned by the file, not by this worksheet — so it reads the clock directly.
That is why `revision` lives here and not four times over.

`FinancialFormLinkSheet` is the other half: which of the four sheets one shared
link opens. A child table rather than a JSONB column because "which live links
open the personal financial statement?" is a question somebody will ask during
an incident, and it should be a query, not a JSON scan.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

#: The four sheets a worksheet spans, and the only values a link scope may
#: name. Kept here rather than imported from `sheet_layout` so the model layer
#: does not depend on the service layer; `test_sheets.py` pins them equal.
SHEET_KINDS: tuple[str, ...] = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")

#: What a link may allow. Stored, not computed: "what did this link permit on
#: 3 March?" is a question a recomputed capability cannot answer.
PERMISSIONS: tuple[str, ...] = ("edit", "view")


class FinancialWorksheet(TimestampMixin, Base):
    __tablename__ = "financial_worksheets"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The forms packet this worksheet belongs beside, when it was opened from
    #: one. Carried so the desk's existing packet listing keeps working; a
    #: worksheet minted on its own leaves it null.
    packet_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    #: The workbook clock. Bumped once per accepted batch, under FOR UPDATE.
    #: Never reset: a client's `base_rev` is only meaningful against a counter
    #: that only goes up.
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_financial_worksheets_profile_id_created_at", "profile_id", "created_at"),
    )


class FinancialFormLinkSheet(Base):
    """One row per sheet a link opens. No rows means the link opens nothing —
    which is why minting refuses an empty selection rather than storing it."""

    __tablename__ = "financial_form_link_sheets"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    link_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_form_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sheet_kind: Mapped[str] = mapped_column(String(24), nullable=False)

    __table_args__ = (
        UniqueConstraint("link_id", "sheet_kind", name="uq_financial_form_link_sheets_link_kind"),
        CheckConstraint(
            "sheet_kind in ('pfs','debt_schedule','p_and_l','balance_sheet')",
            name="ck_financial_form_link_sheets_kind",
        ),
    )
