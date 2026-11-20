"""Full identifiers live outside the application answers and document models."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SecureSsn(Base):
    __tablename__ = "secure_ssns"

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("application_profiles.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    person_name: Mapped[str] = mapped_column(String(120))
    last_four: Mapped[str] = mapped_column(String(4))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SecureSsnAccess(Base):
    """Written in the transaction that authorizes a single plaintext response."""
    __tablename__ = "secure_ssn_access"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("application_profiles.id", ondelete="CASCADE"), index=True)
    entry_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
