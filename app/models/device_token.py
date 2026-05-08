"""Per-user push tokens (Expo).

The mobile app registers its Expo push token via POST /devices/push-tokens
after the borrower grants notification permission. `post_ai_message`
fans these out via Expo's HTTP push API on every system-initiated AI
message — kickoff opener, doc-reminder tier-1, scanner reaction,
anchor narration.

`(user_id, token)` is unique — a device that re-registers (Expo
sometimes rotates tokens) just upserts the timestamp. Tokens are
removed when the user logs out / uninstalls (mobile sends DELETE).
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
    # Expo push token — `ExponentPushToken[xxx]` shape. Tokens are
    # platform-agnostic at this layer; Expo's backend routes to FCM
    # / APNS as needed.
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    # Always 'expo' for now; reserved so we can add 'web' (FCM)
    # without a migration.
    platform: Mapped[str] = mapped_column(String(16), nullable=False, default="expo")
