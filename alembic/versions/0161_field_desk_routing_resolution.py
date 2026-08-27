"""Field Desk routing resolution and financial provenance.

Revision ID: 0161_field_desk_routing_resolution
Revises: 0160_contract_packages_envelopes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0161_field_desk_routing_resolution"
down_revision = "0160_contract_packages_envelopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("client_requested_program", sa.String(80)))
    op.add_column(
        "dos_application_profiles",
        sa.Column(
            "field_provenance",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "dos_application_profiles",
        sa.Column(
            "field_confirmations",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("dos_debts", sa.Column("collateral", sa.Text()))
    op.execute(
        "UPDATE dos_dealers SET client_requested_amount = funding_goal "
        "WHERE client_requested_amount IS NULL AND funding_goal IS NOT NULL"
    )
    op.execute(
        "UPDATE dos_dealers AS d SET client_requested_program = p.selected_program "
        "FROM dos_application_profiles AS p "
        "WHERE p.dealer_id = d.id AND d.client_requested_program IS NULL "
        "AND p.selected_program IS NOT NULL"
    )

    op.create_table(
        "dos_application_recommendations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rules_version", sa.String(48), nullable=False),
        sa.Column("current_amount", sa.Numeric(14, 2)),
        sa.Column("current_program", sa.String(80)),
        sa.Column("recommended_amount", sa.Numeric(14, 2)),
        sa.Column("recommended_program", sa.String(80)),
        sa.Column("supported_min", sa.Numeric(14, 2)),
        sa.Column("supported_max", sa.Numeric(14, 2)),
        sa.Column("reasons", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("response_amount", sa.Numeric(14, 2)),
        sa.Column("response_program", sa.String(80)),
        sa.Column("response_note", sa.Text()),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("responded_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["dealer_id"], ["dos_dealers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["responded_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_dos_application_recommendations_dealer",
        "dos_application_recommendations",
        ["dealer_id", "created_at"],
    )
    op.create_index(
        "ix_dos_application_recommendations_status",
        "dos_application_recommendations",
        ["status"],
    )

    op.create_table(
        "dos_program_rule_resolutions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("program_key", sa.String(80), nullable=False),
        sa.Column("rule_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source", sa.String(160)),
        sa.Column("current_value", postgresql.JSONB()),
        sa.Column("recommended_action", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column("rep_note", sa.Text()),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("requested_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["dealer_id"], ["dos_dealers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_dos_program_rule_resolutions_dealer",
        "dos_program_rule_resolutions",
        ["dealer_id", "status"],
    )
    op.create_index(
        "ix_dos_program_rule_resolutions_rule",
        "dos_program_rule_resolutions",
        ["program_key", "rule_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_dos_program_rule_resolutions_rule", table_name="dos_program_rule_resolutions")
    op.drop_index("ix_dos_program_rule_resolutions_dealer", table_name="dos_program_rule_resolutions")
    op.drop_table("dos_program_rule_resolutions")
    op.drop_index("ix_dos_application_recommendations_status", table_name="dos_application_recommendations")
    op.drop_index("ix_dos_application_recommendations_dealer", table_name="dos_application_recommendations")
    op.drop_table("dos_application_recommendations")
    op.drop_column("dos_debts", "collateral")
    op.drop_column("dos_application_profiles", "field_confirmations")
    op.drop_column("dos_application_profiles", "field_provenance")
    op.drop_column("dos_dealers", "client_requested_program")
