"""Per-admin activity tracking (migration 0106).

AdminActivitySeen — when a super admin last looked at a lead (intake_id set)
or at the global what's-new feed (intake_id NULL, one row per user). Drives
the NEW badges / unseen counts on /admin/ai-underwriter-leads.

AdminDigestState — singleton cursor for the client/broker activity email
digest (services/admin_activity.py). Advances only after a successful SES
send so nothing is dropped while SES is unprovisioned.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AdminActivitySeen(Base):
    __tablename__ = "admin_activity_seen"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    intake_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"), nullable=True
    )
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdminDigestState(Base):
    __tablename__ = "admin_digest_state"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    singleton: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, unique=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
