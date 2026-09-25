"""Add canonical pipeline economics and estimated closing fields.

Revision ID: 0228_pipeline_economics
Revises: 0227_prospect_outcome_cc_voiding
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0228_pipeline_economics"
down_revision = "0227_prospect_outcome_cc_voiding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_funded_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("forecast_fee_points", sa.Numeric(7, 4), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("estimated_close_date", sa.Date(), nullable=True),
    )
    op.create_check_constraint(
        "ck_application_profiles_forecast_fee_points",
        "application_profiles",
        "forecast_fee_points IS NULL OR "
        "(forecast_fee_points >= 0 AND forecast_fee_points <= 100)",
    )
    op.create_check_constraint(
        "ck_application_profiles_underwriting_funded_amount",
        "application_profiles",
        "underwriting_funded_amount IS NULL OR underwriting_funded_amount >= 0",
    )
    op.create_index(
        "ix_application_profiles_estimated_close_date",
        "application_profiles",
        ["estimated_close_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_application_profiles_estimated_close_date",
        table_name="application_profiles",
    )
    op.drop_constraint(
        "ck_application_profiles_forecast_fee_points",
        "application_profiles",
        type_="check",
    )
    op.drop_constraint(
        "ck_application_profiles_underwriting_funded_amount",
        "application_profiles",
        type_="check",
    )
    op.drop_column("application_profiles", "estimated_close_date")
    op.drop_column("application_profiles", "forecast_fee_points")
    op.drop_column("application_profiles", "underwriting_funded_amount")
