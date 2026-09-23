"""Queue booking creation and lifecycle provider effects after local commit.

Revision ID: 0225_booking_delivery_operations
Revises: 0224_prospect_identity_followups
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0225_booking_delivery_operations"
down_revision = "0224_prospect_identity_followups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_delivery_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_type", sa.String(length=24), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "operation_type IN ('create','cancel','reschedule','update')",
            name="ck_booking_delivery_operation_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','action_required','superseded')",
            name="ck_booking_delivery_operation_status",
        ),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["dos_rep_appointments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["calendar_events.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_booking_delivery_operations_idempotency_key"
        ),
    )
    op.create_index(
        "ix_booking_delivery_operations_appointment_id",
        "booking_delivery_operations",
        ["appointment_id"],
    )
    op.create_index(
        "ix_booking_delivery_operations_next_attempt_at",
        "booking_delivery_operations",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_booking_delivery_operations_due",
        "booking_delivery_operations",
        ["status", "next_attempt_at"],
    )

    op.create_table(
        "booking_delivery_effects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effect_key", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=300), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending','processing','sent','failed','unavailable','skipped','action_required')",
            name="ck_booking_delivery_effect_status",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["booking_delivery_operations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            "effect_key",
            name="uq_booking_delivery_effect_operation_key",
        ),
    )
    op.create_index(
        "ix_booking_delivery_effects_operation_status",
        "booking_delivery_effects",
        ["operation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_booking_delivery_effects_operation_status",
        table_name="booking_delivery_effects",
    )
    op.drop_table("booking_delivery_effects")
    op.drop_index(
        "ix_booking_delivery_operations_due",
        table_name="booking_delivery_operations",
    )
    op.drop_index(
        "ix_booking_delivery_operations_next_attempt_at",
        table_name="booking_delivery_operations",
    )
    op.drop_index(
        "ix_booking_delivery_operations_appointment_id",
        table_name="booking_delivery_operations",
    )
    op.drop_table("booking_delivery_operations")
