"""Calendar V2 outcomes, file links, and booking controls.

Revision ID: 0167_calendar_v2_crm_workspace
Revises: 0166_calendar_appointment_crm
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision = "0167_calendar_v2_crm_workspace"
down_revision = "0166_calendar_appointment_crm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "appointment_outcome_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("normalized_name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("color", sa.String(20), nullable=False, server_default="blue"),
        sa.Column("target_crm_status", sa.String(24), nullable=False),
        sa.Column(
            "effects",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "target_crm_status IN ('scheduled','confirmed','completed','follow_up','no_show','not_qualified','converted','cancelled')",
            name="ck_appointment_outcome_definition_status",
        ),
    )
    op.create_index(
        "uq_appointment_outcome_owner_name",
        "appointment_outcome_definitions",
        ["owner_user_id", "normalized_name"],
        unique=True,
    )
    op.create_index(
        "ix_appointment_outcome_owner_active",
        "appointment_outcome_definitions",
        ["owner_user_id", "active", "sort_order"],
    )

    op.add_column(
        "dos_rep_appointments",
        sa.Column("meeting_mode", sa.String(16), nullable=False, server_default="video"),
    )
    op.add_column("dos_rep_appointments", sa.Column("location", sa.String(500)))
    op.add_column(
        "dos_rep_appointments",
        sa.Column("linked_loan_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("workflow_outcome_definition_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column("dos_rep_appointments", sa.Column("workflow_outcome_label", sa.String(120)))
    op.add_column("dos_rep_appointments", sa.Column("workflow_outcome_effects", postgresql.JSONB()))
    op.add_column("dos_rep_appointments", sa.Column("workflow_outcome_results", postgresql.JSONB()))
    op.add_column(
        "dos_rep_appointments",
        sa.Column("workflow_outcome_applied_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("workflow_outcome_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "dos_rep_appointments",
        sa.Column("workflow_outcome_idempotency_key", sa.String(80)),
    )
    op.create_foreign_key(
        "fk_dos_rep_appointment_linked_loan",
        "dos_rep_appointments",
        "loans",
        ["linked_loan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_dos_rep_appointment_workflow_outcome",
        "dos_rep_appointments",
        "appointment_outcome_definitions",
        ["workflow_outcome_definition_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_dos_rep_appointment_workflow_actor",
        "dos_rep_appointments",
        "users",
        ["workflow_outcome_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_dos_rep_appointments_linked_loan",
        "dos_rep_appointments",
        ["linked_loan_id"],
    )
    op.create_index(
        "uq_dos_rep_appointments_outcome_idempotency",
        "dos_rep_appointments",
        ["workflow_outcome_idempotency_key"],
        unique=True,
    )

    op.add_column(
        "booking_settings",
        sa.Column(
            "booking_questions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(
                "'{\"business_name\": true, \"phone\": true, \"requested_amount\": true, \"bank_statement\": false}'::jsonb"
            ),
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column("no_show_follow_up_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "booking_settings",
        sa.Column("morning_digest_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "booking_settings",
        sa.Column("missing_outcome_reminder_hours", sa.Integer(), nullable=False, server_default="48"),
    )


def downgrade() -> None:
    op.drop_column("booking_settings", "missing_outcome_reminder_hours")
    op.drop_column("booking_settings", "morning_digest_enabled")
    op.drop_column("booking_settings", "no_show_follow_up_enabled")
    op.drop_column("booking_settings", "booking_questions")

    op.drop_index(
        "uq_dos_rep_appointments_outcome_idempotency",
        table_name="dos_rep_appointments",
    )
    op.drop_index("ix_dos_rep_appointments_linked_loan", table_name="dos_rep_appointments")
    op.drop_constraint(
        "fk_dos_rep_appointment_workflow_actor",
        "dos_rep_appointments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_dos_rep_appointment_workflow_outcome",
        "dos_rep_appointments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_dos_rep_appointment_linked_loan",
        "dos_rep_appointments",
        type_="foreignkey",
    )
    op.drop_column("dos_rep_appointments", "workflow_outcome_idempotency_key")
    op.drop_column("dos_rep_appointments", "workflow_outcome_by_user_id")
    op.drop_column("dos_rep_appointments", "workflow_outcome_applied_at")
    op.drop_column("dos_rep_appointments", "workflow_outcome_results")
    op.drop_column("dos_rep_appointments", "workflow_outcome_effects")
    op.drop_column("dos_rep_appointments", "workflow_outcome_label")
    op.drop_column("dos_rep_appointments", "workflow_outcome_definition_id")
    op.drop_column("dos_rep_appointments", "linked_loan_id")
    op.drop_column("dos_rep_appointments", "location")
    op.drop_column("dos_rep_appointments", "meeting_mode")

    op.drop_index(
        "ix_appointment_outcome_owner_active",
        table_name="appointment_outcome_definitions",
    )
    op.drop_index(
        "uq_appointment_outcome_owner_name",
        table_name="appointment_outcome_definitions",
    )
    op.drop_table("appointment_outcome_definitions")
