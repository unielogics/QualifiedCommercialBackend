"""Per-viewer read cursor for the dealer-lead communication channel (migration 0107).

DealerLeadChannelSeen — when a given user (the lead's dealer partner, or an
internal teammate) last opened the Messages channel on a dealer AI lead. The
channel itself is the shared BucketNote(visibility="admin") thread on the
lead's bucket; this table is the read/unread state it never had, so both sides
get accurate unread counts on the Messages tab and the global inbox.

One row per (intake_id, user_id). Symmetric to AdminActivitySeen but scoped to
the message channel rather than the whole what's-new feed, and it applies to
dealer partners too (AdminActivitySeen is super-admin-only).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DealerLeadChannelSeen(Base):
    __tablename__ = "dealer_lead_channel_seen"
    __table_args__ = (UniqueConstraint("intake_id", "user_id", name="uq_dealer_lead_channel_seen_intake_user"),)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    intake_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
