"""Per-user push tokens (FCM).

The mobile app registers its FCM device token via POST /devices/push-tokens
after the borrower grants notification permission. `post_ai_message`
fans these out via Firebase Cloud Messaging on every system-initiated
AI message — kickoff opener, doc-reminder tier-1, scanner reaction,
anchor narration.

`(user_id, token)` is unique — a device that re-registers (FCM
sometimes rotates tokens after app updates / cache clears) just
upserts the row. Tokens are removed when the user logs out /
uninstalls (mobile sends DELETE), or pruned by app/services/push.py
when FCM reports `UNREGISTERED` / `INVALID_ARGUMENT`.

History: this used to go through Expo Push Service; switched to
direct FCM so the mobile can use raw FCM tokens without an Expo
project as an intermediary.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class DeviceToken(TimestampMixin, Base):
    __tablename__ = "device_tokens"
    __table_args__ = (UniqueConstraint("user_id", "token", name="uq_device_tokens_user_token"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FCM device token — opaque ~150-char string from
    # `getDevicePushTokenAsync()` on the mobile side.
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    # "device" = raw FCM token (current Android path). Reserved
    # values: "expo" (legacy Expo Push token), "apns" (iOS direct,
    # not yet implemented). Mobile sends the value it generated.
    platform: Mapped[str] = mapped_column(String(16), nullable=False, default="device")
