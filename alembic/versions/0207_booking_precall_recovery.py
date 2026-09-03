"""Recover booking pre-call automation for AI Intake targets.

Revision ID: 0207_booking_precall_recovery
Revises: 0206_form_pdf_refresh_queue
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0207_booking_precall_recovery"
down_revision = "0206_form_pdf_refresh_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_settings",
        sa.Column(
            "precall_default_variant",
            sa.String(length=24),
            nullable=False,
            server_default="main_street",
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column(
            "precall_allowed_variants",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"dealer\", \"real_estate\", \"main_street\", \"mca_refinance\"]'::jsonb"),
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column(
            "precall_allow_vertical_choice",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column(
            "inherit_firm_policy",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "booking_settings",
        sa.Column(
            "firm_policy_overrides",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_booking_settings_precall_default_variant",
        "booking_settings",
        "precall_default_variant IN ('dealer','real_estate','main_street','mca_refinance')",
    )
    op.execute(
        sa.text(
            """
            UPDATE booking_settings AS settings
               SET inherit_firm_policy = true
              FROM users
             WHERE settings.user_id = users.id
               AND users.role IN ('field_rep', 'broker')
            """
        )
    )

    for table in ("booking_notifications", "dos_rep_appointments"):
        op.add_column(
            table,
            sa.Column(
                "precall_intake_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL"),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "precall_application_data",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
        op.create_index(f"ix_{table}_precall_intake", table, ["precall_intake_id"])

    op.create_check_constraint(
        "ck_booking_notification_single_precall_target",
        "booking_notifications",
        "NOT (precall_dealer_id IS NOT NULL AND precall_intake_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_booking_notification_single_precall_target",
        "booking_notifications",
        type_="check",
    )
    for table in ("dos_rep_appointments", "booking_notifications"):
        op.drop_index(f"ix_{table}_precall_intake", table_name=table)
        op.drop_column(table, "precall_application_data")
        op.drop_column(table, "precall_intake_id")
    op.drop_constraint(
        "ck_booking_settings_precall_default_variant",
        "booking_settings",
        type_="check",
    )
    op.drop_column("booking_settings", "firm_policy_overrides")
    op.drop_column("booking_settings", "inherit_firm_policy")
    op.drop_column("booking_settings", "precall_allow_vertical_choice")
    op.drop_column("booking_settings", "precall_allowed_variants")
    op.drop_column("booking_settings", "precall_default_variant")
