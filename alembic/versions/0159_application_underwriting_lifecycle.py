"""application underwriting lifecycle

Revision ID: 0159_application_underwriting_lifecycle
Revises: 0158_financial_start_balance
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0159_application_underwriting_lifecycle"
down_revision = "0158_financial_start_balance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column(
            "underwriting_status",
            sa.String(length=32),
            server_default="submitted",
            nullable=False,
        ),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_approved_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_term_sheet_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_current_dscr", sa.Numeric(8, 4), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_target_dscr", sa.Numeric(8, 4), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_approved_dscr", sa.Numeric(8, 4), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_close_outcome", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_application_profiles_underwriting_updated_by",
        "application_profiles",
        "users",
        ["underwriting_updated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_application_profiles_underwriting_status",
        "application_profiles",
        ["underwriting_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_application_profiles_underwriting_status", table_name="application_profiles")
    op.drop_constraint(
        "fk_application_profiles_underwriting_updated_by",
        "application_profiles",
        type_="foreignkey",
    )
    op.drop_column("application_profiles", "underwriting_updated_at")
    op.drop_column("application_profiles", "underwriting_updated_by_user_id")
    op.drop_column("application_profiles", "underwriting_notes")
    op.drop_column("application_profiles", "underwriting_close_outcome")
    op.drop_column("application_profiles", "underwriting_approved_dscr")
    op.drop_column("application_profiles", "underwriting_target_dscr")
    op.drop_column("application_profiles", "underwriting_current_dscr")
    op.drop_column("application_profiles", "underwriting_term_sheet_amount")
    op.drop_column("application_profiles", "underwriting_approved_amount")
    op.drop_column("application_profiles", "underwriting_status")
