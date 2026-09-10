"""Who changed which cell on a shared worksheet, and what it said before.

Today the public form path writes nothing at all. `save_public_financial_form_draft`
takes a body from a no-login link and saves it with no `log_profile_action`, no
`file_events.emit` and not even a `last_used_at` touch — the only trace a
borrower ever edited their balance sheet is that the numbers are different. That
was survivable for a form somebody opens once and submits. A worksheet is the
opposite: it is designed to be kept open for weeks, forwarded to an accountant
and edited continuously, and the figures on it are the ones underwriting decides
on. **Audit is what makes handing that link out defensible.**

One row per accepted batch of cells, in the same `{address: {before, after}}`
diff shape `production_packages.apply_changes` already writes, with the address
flattened to a string — `"p_and_l.gross_revenue"`, `"debt_schedule.<row>.balance"` —
so the log is greppable without knowing the address union type.

A dedicated table rather than `log_profile_action` because of volume: an hour of
grid editing is hundreds of writes, and `log_profile_action` feeds the activity
timeline the desk reads as a *human* account of the file. Four hundred cell
edits would bury the document upload that mattered.

**Proved and claimed never share a field.** `link_id`, `ip`, `user_agent` and
`actor_user_id` are what the request demonstrated. `claimed_name` is what
somebody typed into a name prompt on a page with no login — useful for reading
the log, worth nothing as identity — so it is named for what it is and stored
apart from the rest.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: How the edit arrived. 'operator' is a signed-in desk user; 'share_link' is
#: somebody holding a no-login token. Kept as stored text rather than derived
#: from `actor_user_id is None`, so a future third door has somewhere to say so.
VIA_OPERATOR = "operator"
VIA_SHARE_LINK = "share_link"


class FinancialWorksheetEdit(Base):
    __tablename__ = "financial_worksheet_edits"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    worksheet_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_worksheets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The workbook clock reading this batch produced. Two rows with the same
    #: revision are the same accepted batch; the range between two rows is what
    #: one session changed.
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sheet_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: {"p_and_l.gross_revenue": {"before": "1,000", "after": "1,250"}}
    changes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Null for a guest. A PIN is not a person.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: SET NULL rather than CASCADE: revoking a link must not erase the record
    #: of what was done with it. That is the moment the record matters most.
    link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_form_links.id", ondelete="SET NULL"),
        index=True,
    )
    claimed_name: Mapped[str | None] = mapped_column(String(120))
    via: Mapped[str] = mapped_column(String(16), nullable=False, default=VIA_SHARE_LINK)
    ip: Mapped[str | None] = mapped_column(String(80))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_financial_worksheet_edits_worksheet_created", "worksheet_id", "created_at"),
    )
