"""A single managed catalog, with durable delivery history and worker claims."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JSON_DATA = JSON().with_variant(JSONB, "postgresql")


class ChatGPTFeedConfig(Base):
    __tablename__ = "chatgpt_feed_config"
    __table_args__ = (CheckConstraint("id = 1", name="ck_chatgpt_feed_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data: Mapped[dict] = mapped_column(JSON_DATA, nullable=False, default=dict)
    public_key: Mapped[str | None] = mapped_column(Text)
    connection_signature: Mapped[str | None] = mapped_column(String(64))
    delivery_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_hash: Mapped[str | None] = mapped_column(String(64))
    last_source_hash: Mapped[str | None] = mapped_column(String(64))
    last_exported_rows: Mapped[list] = mapped_column(JSON_DATA, nullable=False, default=list)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatGPTFeedRun(Base):
    __tablename__ = "chatgpt_feed_runs"
    __table_args__ = (
        CheckConstraint("catalog_id = 1", name="ck_chatgpt_feed_run_catalog"),
        Index("uq_chatgpt_feed_one_active", "catalog_id", unique=True,
              postgresql_where=text("status IN ('queued','running')"),
              sqlite_where=text("status IN ('queued','running')")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    catalog_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    trigger: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    source_hash: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_verified")
