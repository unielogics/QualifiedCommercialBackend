"""Calendar appointment CRM state and immutable activity.

Revision ID: 0163_calendar_appointment_crm
Revises: 0162_unified_client_access
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0163_calendar_appointment_crm"
down_revision = "0162_unified_client_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_settings",
        sa.Column(
            "blocked_intervals",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("crm_status", sa.String(24), nullable=False, server_default="scheduled"),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("follow_up_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("crm_updated_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("crm_updated_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_dos_rep_appointment_crm_actor",
        "dos_rep_appointments",
        "users",
        ["crm_updated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_dos_rep_appointment_crm_status",
        "dos_rep_appointments",
        "crm_status IN ('scheduled','confirmed','completed','follow_up','no_show','not_qualified','converted','cancelled')",
    )
    op.create_index(
        "ix_dos_rep_appointments_crm_status",
        "dos_rep_appointments",
        ["crm_status", "follow_up_at"],
    )

    op.create_table(
        "dos_rep_appointment_activity",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_name", sa.String(160), nullable=False),
        sa.Column("before", postgresql.JSONB()),
        sa.Column("after", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["dos_rep_appointments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_dos_rep_appointment_activity_created",
        "dos_rep_appointment_activity",
        ["appointment_id", "created_at"],
    )

    op.execute(
        """
        UPDATE dos_rep_appointments
        SET crm_status = CASE
            WHEN archived_at IS NOT NULL OR status = 'cancelled' THEN 'cancelled'
            WHEN outcome = 'converted' OR converted_intake_id IS NOT NULL OR converted_dealer_id IS NOT NULL THEN 'converted'
            WHEN outcome = 'did_not_show' THEN 'no_show'
            WHEN outcome = 'not_converted' THEN 'not_qualified'
            WHEN status = 'done' THEN 'completed'
            WHEN client_rsvp_status = 'accepted' OR status = 'confirmed' THEN 'confirmed'
            ELSE 'scheduled'
        END,
        crm_updated_at = COALESCE(outcome_at, archived_at, updated_at, created_at)
        """
    )
    op.execute(
        """
        INSERT INTO dos_rep_appointment_activity (
            id, appointment_id, event_type, body, actor_user_id, actor_name,
            before, after, created_at
        )
        SELECT
            gen_random_uuid(), id, 'appointment_created', title,
            booked_by_user_id, 'System', NULL,
            jsonb_build_object('crm_status', crm_status), created_at
        FROM dos_rep_appointments
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dos_rep_appointment_activity_created",
        table_name="dos_rep_appointment_activity",
    )
    op.drop_table("dos_rep_appointment_activity")
    op.drop_index("ix_dos_rep_appointments_crm_status", table_name="dos_rep_appointments")
    op.drop_constraint(
        "ck_dos_rep_appointment_crm_status",
        "dos_rep_appointments",
        type_="check",
    )
    op.drop_constraint(
        "fk_dos_rep_appointment_crm_actor",
        "dos_rep_appointments",
        type_="foreignkey",
    )
    op.drop_column("dos_rep_appointments", "crm_updated_by_user_id")
    op.drop_column("dos_rep_appointments", "crm_updated_at")
    op.drop_column("dos_rep_appointments", "follow_up_at")
    op.drop_column("dos_rep_appointments", "crm_status")
    op.drop_column("booking_settings", "blocked_intervals")
