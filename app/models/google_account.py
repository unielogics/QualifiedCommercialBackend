"""Per-user Google OAuth grant.

One row per user holds the encrypted refresh token + granted scopes for that
user's connected Google account. Every Google call (Gmail send-as-user, Calendar
two-way sync, Drive attach/ingest) resolves live credentials from this row via
`app.services.google.google_oauth_client.credentials_for_user`.

Refresh tokens are encrypted at rest with the SAME Fernet/KMS path used by
ProviderSecret (see app/services/provider_secrets.py); the `encryption_provider`
+ `kms_key_id` columns make decryption self-describing, mirroring ProviderSecret.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class GoogleAccount(TimestampMixin, Base):
    __tablename__ = "google_accounts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # The connected Google identity (may differ from User.email).
    google_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Stable Google account id from the id_token `sub` — survives an email change.
    google_sub: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Encrypted refresh token + self-describing encryption metadata (mirrors
    # ProviderSecret so decryption knows which path to take).
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encryption_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="fernet")
    kms_key_id: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Optional short-lived access-token cache to avoid a refresh on every call.
    access_token_cache: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Granted OAuth scopes (source of truth for "what can this account do").
    scopes: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Denormalized capability flags (derived from scopes) for cheap status reads.
    gmail_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    calendar_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    drive_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Per-user automation opt-ins (Phase 4). Reserved now to avoid a later migration.
    # e.g. {"re_agent_auto_emails": true, "merged_updates_auto": true, "status_change_emails": true}
    automation_settings: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Two-way calendar sync bookkeeping (Phase 2). Reserved now.
    calendar_sync_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    calendar_channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    calendar_resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    calendar_watch_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # "active" | "revoked" | "expired"
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
