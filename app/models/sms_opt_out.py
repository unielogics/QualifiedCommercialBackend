"""A number that must never be texted, whatever the reason it was reachable.

Distinct from `DealerSmsConsent` on purpose, and the distinction matters.

`DealerSmsConsent` records the lifecycle of a GRANT: someone opted in, under a
named disclosure, and may later revoke. `sms_consent.revoke()` marks those
grants revoked — but its WHERE clause only matches rows that are `granted` and
not yet revoked, so a STOP from a number that never granted anything matches
nothing and leaves no trace at all. That number stays textable forever.

Which is exactly the situation on the `Client` side of the codebase: clients
have a `contact_permission` string and no consent rows whatsoever, so revocation
had nothing to bite on.

This table is the other half: a plain suppression list, keyed on the number,
written whenever anyone says stop through any channel, and consulted before
every outbound message regardless of which subsystem is sending. It needs no
prior grant to exist, which is the whole point.

The invariant it enforces is the one `consent_for` already states for dealer
files, extended to everything:

    a number that opted out anywhere is unreachable everywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class SmsOptOut(TimestampMixin, Base):
    """One row per phone number that has asked not to be texted.

    Rows are not deleted on re-opt-in. Clearing an opt-out sets `cleared_at`,
    so the history of "they said stop, then later said start" survives — which
    is what you need if a carrier or a regulator asks why a number that once
    opted out is being messaged again.
    """

    __tablename__ = "sms_opt_out"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    #: E.164, normalized before write. Unique: one suppression row per number.
    phone_e164: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    #: The literal inbound text that triggered it ("STOP", "unsubscribe"), or a
    #: short description when set by an operator. Kept for proof.
    reason: Mapped[str] = mapped_column(String(120), default="STOP")

    #: Where it came from: "sms_reply", "operator", "carrier", "import".
    source: Mapped[str] = mapped_column(String(32), default="sms_reply")

    #: Free-form context — the relay record, the operator's note.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when the number opts back in. NULL means the suppression is live.
    cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        state = "cleared" if self.cleared_at else "active"
        return f"<SmsOptOut {self.phone_e164} {state}>"
