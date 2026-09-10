"""A link that opens one financial form and nothing else.

The document room already reaches a borrower, but it is a whole workspace —
tabs, uploads, agreements. Asking someone for a personal financial statement
should land them on the statement, not on a room they have to navigate.

**Only the hash of the token is stored.** The link carries no access code, by
decision, which makes the URL itself the entire credential: anyone holding it
can read and write a balance sheet. Hashing costs nothing and means a database
read — a backup, a support query, a leaked dump — cannot hand somebody a working
link. The dealer intake room already stores its token this way; the bucket room
stores its in plaintext and relies on a PIN instead, which is the trade this one
is not making.

`expires_at` and `revoked_at` exist for the same reason. An open link with no
end date is a permanent credential to someone's finances sitting in whatever
inbox it was forwarded to.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class FinancialFormLink(TimestampMixin, Base):
    __tablename__ = "financial_form_links"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: pfs — a personal financial statement, per person.
    #: debt_schedule — the business debt schedule, one per file.
    #: p_and_l — the business profit and loss statement, one per file.
    #: balance_sheet — the business balance sheet, one per file.
    #: worksheet — the grid over all four, scoped by `link_sheets`.
    #: The forms packet is not a kind: it is four links of these kinds whose
    #: tokens derive from one base and which share a `packet_id`.
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Set on the four children of one packet, so the desk can list a packet
    #: and close all four in one action. Null on a link minted on its own.
    packet_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    #: The statement this link edits. Set when a link is minted for an existing
    #: draft so a borrower resumes rather than starting a second sheet.
    statement_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("financial_statements.id", ondelete="CASCADE")
    )
    #: SHA-256 of the token. The token itself is shown once, at mint time.
    token_hash: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(120))
    invitee_email: Mapped[str | None] = mapped_column(String(320))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the borrower pressed Save. The page shows its thank-you state from
    #: this, so a reload returns to it rather than to an empty form.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the holder may do. 'edit' is the default because it is exactly what
    #: every link minted before this column existed already allowed — a form
    #: link has always been a write credential — so defaulting it changes no
    #: live link's behaviour. A view link is a deliberate narrowing, enforced
    #: on the server on every write and never by a disabled input.
    permission: Mapped[str] = mapped_column(
        String(8), nullable=False, default="edit", server_default="edit"
    )
    #: The worksheet a `kind='worksheet'` link opens. Links are joined to each
    #: other on this, rather than by deriving one token from another: the
    #: packet's `{base}.{kind}` scheme means any child token yields the base
    #: and the base yields all four children, so a link advertised as "P&L
    #: only" would be a lie. An independent token per link is what makes the
    #: per-sheet scope true.
    worksheet_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_worksheets.id", ondelete="SET NULL"),
        index=True,
    )

    #: The second factor on a worksheet link, hashed like the client room's
    #: passcode. A form link has always been a bare bearer credential, and for a
    #: form somebody opens once that was a defensible trade. A worksheet changes
    #: the duration and the reach, not the data: it is meant to be bookmarked,
    #: kept for weeks, forwarded from the accountant to their bookkeeper, and it
    #: is continuously writable. Null on every link minted before this existed,
    #: which is exactly today's behaviour for those links.
    pin_hash: Mapped[str | None] = mapped_column(String(160))
    #: When the PIN was last set. The unlock session is bound to this, so
    #: rotating the PIN ends every open tab without a revocation list.
    pin_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Wrong attempts are counted **on the row**, not in process memory: this
    #: instance restarts on every deploy, and a lockout a deploy forgets is not
    #: a lockout.
    pin_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: How many times the link has been opened. `last_used_at` answers "is this
    #: still in use"; the count answers "was this forwarded", which is the
    #: question asked after something leaks.
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "kind in ('pfs','debt_schedule','p_and_l','balance_sheet','worksheet')",
            name="ck_financial_form_links_kind",
        ),
        CheckConstraint(
            "permission in ('edit','view')", name="ck_financial_form_links_permission"
        ),
        Index("ix_financial_form_links_profile_kind", "profile_id", "kind"),
    )

    @property
    def is_open(self) -> bool:
        """Whether this link still works. Expiry and revocation are separate
        facts — one is a deadline, the other is someone deciding — and both
        close it."""
        from datetime import UTC
        from datetime import datetime as _dt

        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= _dt.now(UTC):
            return False
        return True
