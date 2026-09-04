"""When a booking's delivery failure actually happened.

`booking_notifications.last_error` had no timestamp, so the appointment panel
dated it with `updated_at`. That is a different fact: every later write to the
booking moves `updated_at`, including a reminder that goes out successfully. A
failure from 31 Aug therefore rendered as "Recorded today, 11:30 AM" — the time
of a text that had just been delivered.

Existing rows are left null rather than backfilled from `updated_at`, which
would encode the same wrong answer permanently. A null reads as "not recorded",
and the panel omits the date instead of inventing one.

Revision ID: 0187_booking_delivery_error_at
Revises: 0186_booking_video_library
"""

import sqlalchemy as sa

from alembic import op

revision = "0187_booking_delivery_error_at"
down_revision = "0186_booking_video_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_notifications",
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("booking_notifications", "last_error_at")
