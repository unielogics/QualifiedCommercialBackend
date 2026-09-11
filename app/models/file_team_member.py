"""A team seat on an application file.

Files may have multiple agents and multiple underwriters. One agent can still
be marked as ownership-derived through `derived_from`; additional agents are
assigned by the desk. Seats decide who receives file timeline updates and may
read the team roster. They do not broaden access to the underlying file.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

SEAT_AGENT = "agent"
SEAT_UNDERWRITER = "underwriter"
SEATS = (SEAT_AGENT, SEAT_UNDERWRITER)


class FileTeamMember(TimestampMixin, Base):
    __tablename__ = "file_team_members"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    seat: Mapped[str] = mapped_column(String(24), nullable=False)
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Which ownership pointer produced a derived agent seat; null when the
    #: seat was set by hand.
    derived_from: Mapped[str | None] = mapped_column(String(48), nullable=True)

    __table_args__ = (
        UniqueConstraint("profile_id", "user_id", "seat", name="uq_file_team_members_profile_user_seat"),
        Index("ix_file_team_members_profile", "profile_id"),
        Index("ix_file_team_members_user", "user_id"),
    )
