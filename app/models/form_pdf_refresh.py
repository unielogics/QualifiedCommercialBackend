"""One pending redraw per form, held back until the typing stops.

Every save on the four financial forms used to render a PDF and put it to S3
on the spot. On a form that is a page of boxes that was one document per Save;
on the live worksheet, where a save is a cell, it is one document per keystroke.

The owner's instruction was to wait: *"we will wait 120 seconds before we
generate PDF with updates. This way we prevent excessive mistakes from happening
and documents being updated for no reason."* A document rewritten on every
keystroke is noise, and a half-typed figure briefly filed as fact is worse than
a document two minutes behind.

This table is the whole of that hold. One row per (profile, form) — enforced,
not hoped for, by the unique constraint — carrying the moment the redraw comes
due. **A save does not add a row; it pushes `due_at` out.** That is the
difference between a debounce and a rate limit: a rate limit fires on the first
edit of a burst and drops the rest, so the last thing typed never reaches the
PDF. Pushing the deadline forward on every save means the render happens 120
seconds after the *last* edit, and what it renders is the finished figure.

The row is a deadline, not a payload. Nothing about the form's contents is
stored here: the drain reads the body back off the file at the moment it
renders, so a row that waited through six more edits still draws today's
numbers. `actor_name`/`actor_email` are the one exception, and only so the
refreshed document carries the name of whoever last touched it.

A queue row is not a promise the document will ever exist: no checklist row,
nothing typed yet, no document room, and the drain quietly drops it. It only
ever means "this form's PDF may be behind what the file says".

Deleting the profile deletes its pending redraws — CASCADE, because a redraw of
a file nobody has is work with no reader.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class FormPdfRefresh(TimestampMixin, Base):
    """A form whose PDF is owed an update, and the moment it is owed."""

    __tablename__ = "form_pdf_refresh_queue"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("application_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: One of `drafted_forms._KINDS` — p_and_l, balance_sheet, debt_schedule,
    #: pfs. The same vocabulary the save routes and `sheet_layout` use, so a
    #: queue row names the form the way everything else names it.
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: When the redraw comes due. Moved *forward* by every further save, which
    #: is what makes this settle on the last edit rather than the first.
    #: Indexed because the drain's only question is "what is due now".
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    #: Who last typed, so the document the drain files is attributed to them
    #: rather than to the cron actor that happened to draw it.
    actor_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    __table_args__ = (
        # The debounce itself. Without this a burst of saves is a pile of rows
        # and the render happens as many times as somebody pressed a key.
        UniqueConstraint("profile_id", "kind", name="uq_form_pdf_refresh_queue_profile_kind"),
    )
