from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import Role
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.broker import Broker
    from app.models.client import Client


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: an invited team member exists in our DB before they complete
    # Clerk sign-up. The row gets bound to a real clerk_id on first JIT
    # auto-provision (deps.get_current_user matches by email).
    clerk_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    role: Mapped[Role] = mapped_column(String(32), nullable=False, default=Role.CLIENT)
    # Soft-delete: super-admin revoke sets this rather than physically deleting,
    # so historical FK references (loans.broker_id → brokers.user_id, etc.) survive.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optional links to broker/client profiles (one-to-one)
    broker: Mapped[Broker | None] = relationship(back_populates="user", uselist=False)
    client: Mapped[Client | None] = relationship(back_populates="user", uselist=False)
