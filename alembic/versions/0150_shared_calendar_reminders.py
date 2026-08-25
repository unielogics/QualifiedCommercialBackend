"""shared team calendar buffers and reminder deliveries

Revision ID: 0150_shared_calendar_reminders
Revises: 0149_product_booklet_prescreen
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0150_shared_calendar_reminders"
down_revision = "0149_product_booklet_prescreen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_settings", sa.Column("buffer_before_min", sa.Integer(), server_default="5", nullable=False))
    op.add_column("booking_settings", sa.Column("buffer_after_min", sa.Integer(), server_default="5", nullable=False))
    op.add_column("booking_settings", sa.Column("confirmation_email_enabled", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("booking_settings", sa.Column("confirmation_sms_enabled", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("booking_settings", sa.Column("reminder_email_enabled", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("booking_settings", sa.Column("reminder_email_minutes_before", sa.Integer(), server_default="1440", nullable=False))
    op.add_column("booking_settings", sa.Column("reminder_sms_enabled", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("booking_settings", sa.Column("reminder_sms_minutes_before", sa.Integer(), server_default="120", nullable=False))
    op.add_column("booking_settings", sa.Column("google_meet_enabled", sa.Boolean(), server_default=sa.true(), nullable=False))

    op.create_table(
        "booking_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("booked_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invitee_name", sa.String(length=160), nullable=False),
        sa.Column("invitee_email", sa.String(length=320), nullable=True),
        sa.Column("invitee_phone", sa.String(length=40), nullable=True),
        sa.Column("sms_consent", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sms_consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sms_consent_method", sa.String(length=32), nullable=True),
        sa.Column("sms_disclosure_version", sa.String(length=40), nullable=True),
        sa.Column("sms_disclosure_text", sa.Text(), nullable=True),
        sa.Column("sms_consent_ip", sa.String(length=64), nullable=True),
        sa.Column("sms_consent_user_agent", sa.String(length=400), nullable=True),
        sa.Column("program_name", sa.String(length=180), nullable=True),
        sa.Column("requested_amount", sa.String(length=40), nullable=True),
        sa.Column("full_address", sa.String(length=500), nullable=True),
        sa.Column("join_url", sa.String(length=500), nullable=True),
        sa.Column("confirmation_email_status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("confirmation_sms_status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("email_reminder_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sms_reminder_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sms_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_reminder_status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("sms_reminder_status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["booked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["event_id"], ["calendar_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("ix_booking_notifications_email_due", "booking_notifications", ["email_reminder_due_at", "email_reminder_sent_at"])
    op.create_index("ix_booking_notifications_sms_due", "booking_notifications", ["sms_reminder_due_at", "sms_reminder_sent_at"])

    # The primary super admin's booking policy is the common Field Desk calendar.
    op.execute(
        """
        UPDATE booking_settings bs
        SET enabled = true,
            duration_min = 20,
            buffer_before_min = 5,
            buffer_after_min = 5,
            confirmation_email_enabled = true,
            confirmation_sms_enabled = true,
            reminder_email_enabled = true,
            reminder_email_minutes_before = 1440,
            reminder_sms_enabled = true,
            reminder_sms_minutes_before = 120,
            google_meet_enabled = true
        FROM users u
        WHERE bs.user_id = u.id
          AND lower(u.email) = 'franco@qualifiedcommercial.com'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_booking_notifications_sms_due", table_name="booking_notifications")
    op.drop_index("ix_booking_notifications_email_due", table_name="booking_notifications")
    op.drop_table("booking_notifications")
    for name in (
        "google_meet_enabled",
        "reminder_sms_minutes_before",
        "reminder_sms_enabled",
        "reminder_email_minutes_before",
        "reminder_email_enabled",
        "confirmation_sms_enabled",
        "confirmation_email_enabled",
        "buffer_after_min",
        "buffer_before_min",
    ):
        op.drop_column("booking_settings", name)
