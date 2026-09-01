"""Shared appointment outcomes and privileged Calendar CRM controls.

Revision ID: 0171_shared_calendar_outcomes
Revises: 0170_booking_windows_weekly_schedule
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0171_shared_calendar_outcomes"
down_revision = "0170_booking_windows_weekly_schedule"
branch_labels = None
depends_on = None


DEFAULT_OUTCOMES = (
    (
        "Qualified",
        "Create or update the client file after a reviewed conversion.",
        "green",
        "converted",
        ["log_activity", "file_action"],
    ),
    (
        "Follow up",
        "Schedule the next client touch and keep the opportunity open.",
        "blue",
        "follow_up",
        ["log_activity", "schedule_follow_up"],
    ),
    (
        "Documents requested",
        "Record the request and keep the appointment in follow-up.",
        "amber",
        "follow_up",
        ["log_activity", "request_documents"],
    ),
    (
        "No show",
        "Mark the missed appointment and offer a path to rebook.",
        "red",
        "no_show",
        ["log_activity", "send_no_show_rebooking"],
    ),
    (
        "Not a fit",
        "Close the enquiry while retaining the reason and history.",
        "gray",
        "not_qualified",
        ["log_activity", "close_enquiry"],
    ),
)


def upgrade() -> None:
    op.add_column(
        "appointment_outcome_definitions",
        sa.Column("scope", sa.String(16), nullable=False, server_default="personal"),
    )
    op.alter_column(
        "appointment_outcome_definitions",
        "owner_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.drop_index(
        "uq_appointment_outcome_owner_name",
        table_name="appointment_outcome_definitions",
    )
    op.drop_index(
        "ix_appointment_outcome_owner_active",
        table_name="appointment_outcome_definitions",
    )
    op.create_check_constraint(
        "ck_appointment_outcome_definition_scope",
        "appointment_outcome_definitions",
        "scope IN ('personal','shared')",
    )
    op.create_check_constraint(
        "ck_appointment_outcome_definition_owner_scope",
        "appointment_outcome_definitions",
        "(scope = 'personal' AND owner_user_id IS NOT NULL) OR "
        "(scope = 'shared' AND owner_user_id IS NULL)",
    )
    op.create_index(
        "uq_appointment_outcome_personal_name",
        "appointment_outcome_definitions",
        ["owner_user_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("scope = 'personal'"),
    )
    op.create_index(
        "uq_appointment_outcome_shared_name",
        "appointment_outcome_definitions",
        ["normalized_name"],
        unique=True,
        postgresql_where=sa.text("scope = 'shared' AND active = true"),
    )
    op.create_index(
        "ix_appointment_outcome_scope_active",
        "appointment_outcome_definitions",
        ["scope", "owner_user_id", "active", "sort_order"],
    )

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    for index, (name, description, color, target_status, effects) in enumerate(
        DEFAULT_OUTCOMES
    ):
        op.execute(
            sa.text(
                "INSERT INTO appointment_outcome_definitions "
                "(id, owner_user_id, scope, name, normalized_name, description, "
                "color, target_crm_status, effects, active, sort_order) VALUES ("
                f"'{uuid.uuid4()}'::uuid, NULL, 'shared', {quote(name)}, "
                f"{quote(name.casefold())}, {quote(description)}, {quote(color)}, "
                f"{quote(target_status)}, {quote(json.dumps(effects))}::jsonb, TRUE, {index})"
            )
        )


def downgrade() -> None:
    op.execute("DELETE FROM appointment_outcome_definitions WHERE scope = 'shared'")
    op.drop_index(
        "ix_appointment_outcome_scope_active",
        table_name="appointment_outcome_definitions",
    )
    op.drop_index(
        "uq_appointment_outcome_shared_name",
        table_name="appointment_outcome_definitions",
    )
    op.drop_index(
        "uq_appointment_outcome_personal_name",
        table_name="appointment_outcome_definitions",
    )
    op.drop_constraint(
        "ck_appointment_outcome_definition_owner_scope",
        "appointment_outcome_definitions",
        type_="check",
    )
    op.drop_constraint(
        "ck_appointment_outcome_definition_scope",
        "appointment_outcome_definitions",
        type_="check",
    )
    op.alter_column(
        "appointment_outcome_definitions",
        "owner_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column("appointment_outcome_definitions", "scope")
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
