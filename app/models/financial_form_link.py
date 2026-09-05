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

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
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
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
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

    __table_args__ = (
        CheckConstraint("kind in ('pfs','debt_schedule')", name="ck_financial_form_links_kind"),
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
