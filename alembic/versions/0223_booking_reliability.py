"""Make Marketing bookings idempotent and allow minute booking notice.

Revision ID: 0223_booking_reliability
Revises: 0222_marketing_conversion
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0223_booking_reliability"
down_revision = "0222_marketing_conversion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_slug_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_booking_slug_aliases_slug"),
    )
    op.create_index(
        "ix_booking_slug_aliases_user_id",
        "booking_slug_aliases",
        ["user_id"],
    )
    # Preserve the previously published Field Desk link while the primary
    # shared-calendar page uses its shorter canonical slug.
    op.execute(
        "INSERT INTO booking_slug_aliases (id, user_id, slug) "
        "SELECT gen_random_uuid(), user_id, 'jonathan-franco' "
        "FROM booking_settings WHERE slug = 'franco' "
        "ON CONFLICT (slug) DO NOTHING"
    )
    op.add_column(
        "booking_settings",
        sa.Column(
            "minimum_notice_minutes",
            sa.Integer(),
            nullable=False,
            server_default="2880",
        ),
    )
    op.execute(
        "UPDATE booking_settings "
        "SET minimum_notice_minutes = GREATEST(0, minimum_notice_days) * 1440"
    )
    op.create_check_constraint(
        "ck_booking_settings_minimum_notice_minutes",
        "booking_settings",
        "minimum_notice_minutes >= 0 AND minimum_notice_minutes <= 525600",
    )

    op.add_column(
        "booking_notifications",
        sa.Column(
            "delivery_attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "booking_notifications",
        sa.Column("delivery_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "booking_notifications",
        sa.Column("delivery_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "booking_notifications",
        sa.Column("delivery_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE booking_notifications SET delivery_next_attempt_at = CURRENT_TIMESTAMP "
        "WHERE confirmation_email_status = 'pending' "
        "OR confirmation_sms_status = 'pending'"
    )
    op.create_index(
        "ix_booking_notifications_delivery_next_attempt_at",
        "booking_notifications",
        ["delivery_next_attempt_at"],
    )

    op.add_column(
        "dos_rep_appointments",
        sa.Column("prospect_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("creation_idempotency_key", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("return_stage_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dos_rep_appointments_prospect",
        "dos_rep_appointments",
        "dealer_prospects",
        ["prospect_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_dos_rep_appointments_return_stage",
        "dos_rep_appointments",
        "dealer_prospect_stage_definitions",
        ["return_stage_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_dos_rep_appointments_prospect",
        "dos_rep_appointments",
        ["prospect_id", "starts_at"],
    )
    op.create_unique_constraint(
        "uq_dos_rep_appointments_creation_idempotency",
        "dos_rep_appointments",
        ["creation_idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_dos_rep_appointments_creation_idempotency",
        "dos_rep_appointments",
        type_="unique",
    )
    op.drop_index("ix_dos_rep_appointments_prospect", table_name="dos_rep_appointments")
    op.drop_constraint(
        "fk_dos_rep_appointments_return_stage",
        "dos_rep_appointments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_dos_rep_appointments_prospect",
        "dos_rep_appointments",
        type_="foreignkey",
    )
    op.drop_column("dos_rep_appointments", "return_stage_id")
    op.drop_column("dos_rep_appointments", "creation_idempotency_key")
    op.drop_column("dos_rep_appointments", "prospect_id")

    op.drop_index(
        "ix_booking_notifications_delivery_next_attempt_at",
        table_name="booking_notifications",
    )
    op.drop_column("booking_notifications", "delivery_completed_at")
    op.drop_column("booking_notifications", "delivery_next_attempt_at")
    op.drop_column("booking_notifications", "delivery_last_attempt_at")
    op.drop_column("booking_notifications", "delivery_attempt_count")

    op.drop_constraint(
        "ck_booking_settings_minimum_notice_minutes",
        "booking_settings",
        type_="check",
    )
    op.drop_column("booking_settings", "minimum_notice_minutes")
    op.drop_index("ix_booking_slug_aliases_user_id", table_name="booking_slug_aliases")
    op.drop_table("booking_slug_aliases")
