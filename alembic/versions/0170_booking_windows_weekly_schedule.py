"""Add rolling booking windows and per-day availability schedules.

Revision ID: 0170_booking_windows_weekly_schedule
Revises: 0169_sms_messages
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0170_booking_windows_weekly_schedule"
down_revision = "0169_sms_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_settings",
        sa.Column(
            "weekly_schedule",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column(
            "advance_booking_window_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column("minimum_notice_days", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "booking_settings",
        sa.Column("maximum_advance_days", sa.Integer(), nullable=False, server_default="5"),
    )
    op.create_check_constraint(
        "ck_booking_settings_notice_days",
        "booking_settings",
        "minimum_notice_days >= 0 AND minimum_notice_days <= 365",
    )
    op.create_check_constraint(
        "ck_booking_settings_advance_days",
        "booking_settings",
        "maximum_advance_days >= 1 AND maximum_advance_days <= 365 "
        "AND maximum_advance_days >= minimum_notice_days",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_booking_settings_advance_days",
        "booking_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_booking_settings_notice_days",
        "booking_settings",
        type_="check",
    )
    op.drop_column("booking_settings", "maximum_advance_days")
    op.drop_column("booking_settings", "minimum_notice_days")
    op.drop_column("booking_settings", "advance_booking_window_enabled")
    op.drop_column("booking_settings", "weekly_schedule")
