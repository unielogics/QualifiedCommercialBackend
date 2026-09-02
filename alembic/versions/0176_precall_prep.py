"""Pre-call prep: draft file on every booking, room PIN ownership, configurable sequence.

Revision ID: 0176_precall_prep
Revises: 0175_client_room_pin_recovery
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0176_precall_prep"
down_revision = "0175_client_room_pin_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The booking now owns a draft dealer file and the pre-call sequence state.
    op.add_column(
        "booking_notifications",
        sa.Column(
            "precall_dealer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("booking_notifications", sa.Column("precall_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("booking_notifications", sa.Column("precall_stopped_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("booking_notifications", sa.Column("precall_stop_reason", sa.String(32), nullable=True))
    op.add_column("booking_notifications", sa.Column("precall_pin_delivered_via", sa.String(12), nullable=True))
    op.create_index("ix_booking_notifications_precall_dealer", "booking_notifications", ["precall_dealer_id"])

    # Reminder rows gain a kind so booking-anchored pre-call steps can share the
    # dispatcher without being re-timed like call-anchored reminders.
    op.add_column(
        "booking_notification_reminders",
        sa.Column("kind", sa.String(12), nullable=False, server_default="reminder"),
    )
    op.add_column("booking_notification_reminders", sa.Column("step_key", sa.String(24), nullable=True))
    op.add_column("booking_notification_reminders", sa.Column("rendered_body", sa.Text(), nullable=True))

    # Host-authored message templates for both channels.
    op.add_column(
        "booking_settings",
        sa.Column("precall_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    for column in ("precall_messages", "reminder_email_messages", "confirmation_messages"):
        op.add_column(
            "booking_settings",
            sa.Column(column, JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        )

    # The client can choose their own room PIN on first entry.
    op.add_column(
        "bucket_upload_links",
        sa.Column("passcode_set_by_client_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Where a draft came from, so booking drafts can be told apart from
    # product-finder drafts in lists and cleanup.
    op.add_column("dos_dealers", sa.Column("draft_source", sa.String(24), nullable=True))


def downgrade() -> None:
    op.drop_column("dos_dealers", "draft_source")
    op.drop_column("bucket_upload_links", "passcode_set_by_client_at")
    for column in ("confirmation_messages", "reminder_email_messages", "precall_messages", "precall_enabled"):
        op.drop_column("booking_settings", column)
    op.drop_column("booking_notification_reminders", "rendered_body")
    op.drop_column("booking_notification_reminders", "step_key")
    op.drop_column("booking_notification_reminders", "kind")
    op.drop_index("ix_booking_notifications_precall_dealer", table_name="booking_notifications")
    for column in (
        "precall_pin_delivered_via",
        "precall_stop_reason",
        "precall_stopped_at",
        "precall_completed_at",
        "precall_dealer_id",
    ):
        op.drop_column("booking_notifications", column)
