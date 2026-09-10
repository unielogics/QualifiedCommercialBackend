"""A seat on a file.

Every file (`ApplicationProfile`) has one agent seat — the rep or broker who
brought it — and any number of underwriter seats. The agent seat is derived
from the ownership pointers the system already keeps (a dealer partner on an
intake, a field rep on a dealer file, the client's agent, the loan's broker),
and `derived_from` says which one produced it; the desk sets underwriter
seats by hand. Seats decide who is told about the file's timeline and who may
read it. They do not widen what a person may open: file access stays with the
ownership rules that already exist.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
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
        Index("uq_file_team_one_agent", "profile_id", unique=True, postgresql_where=text("seat = 'agent'")),
    )
