"""Shared model mixins."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """created_at / updated_at, both stamped by the database.

    `updated_at` uses a server-side onupdate, so SQLAlchemy expires the
    attribute after every UPDATE flush and reloads it on next access. In an
    async session that lazy reload raises MissingGreenlet, which surfaces as a
    500 on any route that commits and then serialises the same row. If you read
    `updated_at` after a write, refresh it first — see `serialize()` in
    app/services/production_packages.py for the guarded form.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
