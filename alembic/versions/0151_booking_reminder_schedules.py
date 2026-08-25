"""repeatable booking reminder schedules

Revision ID: 0151_booking_reminder_schedules
Revises: 0150_shared_calendar_reminders
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0151_booking_reminder_schedules"
down_revision = "0150_shared_calendar_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_settings",
        sa.Column(
            "reminder_email_minutes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[1440]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column(
            "reminder_sms_minutes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[120]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE booking_settings
        SET reminder_email_minutes = jsonb_build_array(reminder_email_minutes_before),
            reminder_sms_minutes = jsonb_build_array(reminder_sms_minutes_before)
        """
    )

    op.create_table(
        "booking_notification_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booking_notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=12), nullable=False),
        sa.Column("minutes_before", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=300), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["booking_notification_id"], ["booking_notifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "booking_notification_id",
            "channel",
            "minutes_before",
            name="uq_booking_notification_reminder_schedule",
        ),
    )
    op.create_index(
        "ix_booking_notification_reminders_due",
        "booking_notification_reminders",
        ["status", "due_at"],
    )

    op.execute(
        """
        INSERT INTO booking_notification_reminders (
            id, booking_notification_id, channel, minutes_before, due_at,
            status, sent_at, error, created_at, updated_at
        )
        SELECT gen_random_uuid(), bn.id, 'email',
               GREATEST(15, ROUND(EXTRACT(EPOCH FROM (ce.starts_at - bn.email_reminder_due_at)) / 60)::integer),
               bn.email_reminder_due_at, bn.email_reminder_status,
               bn.email_reminder_sent_at, bn.last_error, now(), now()
        FROM booking_notifications bn
        JOIN calendar_events ce ON ce.id = bn.event_id
        WHERE bn.email_reminder_due_at IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO booking_notification_reminders (
            id, booking_notification_id, channel, minutes_before, due_at,
            status, sent_at, error, created_at, updated_at
        )
        SELECT gen_random_uuid(), bn.id, 'sms',
               GREATEST(15, ROUND(EXTRACT(EPOCH FROM (ce.starts_at - bn.sms_reminder_due_at)) / 60)::integer),
               bn.sms_reminder_due_at, bn.sms_reminder_status,
               bn.sms_reminder_sent_at, bn.last_error, now(), now()
        FROM booking_notifications bn
        JOIN calendar_events ce ON ce.id = bn.event_id
        WHERE bn.sms_reminder_due_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_booking_notification_reminders_due", table_name="booking_notification_reminders")
    op.drop_table("booking_notification_reminders")
    op.drop_column("booking_settings", "reminder_sms_minutes")
    op.drop_column("booking_settings", "reminder_email_minutes")
