"""Add accepted amount and split QC earnings components.

Revision ID: 0232_accepted_deal_economics
Revises: 0231_marketing_business_types
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0232_accepted_deal_economics"
down_revision = "0231_marketing_business_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("underwriting_accepted_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "application_profiles",
        sa.Column("forecast_consulting_fee", sa.Numeric(14, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_application_profiles_underwriting_accepted_amount",
        "application_profiles",
        "underwriting_accepted_amount IS NULL OR underwriting_accepted_amount >= 0",
    )
    op.create_check_constraint(
        "ck_application_profiles_forecast_consulting_fee",
        "application_profiles",
        "forecast_consulting_fee IS NULL OR forecast_consulting_fee >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_application_profiles_forecast_consulting_fee",
        "application_profiles",
        type_="check",
    )
    op.drop_constraint(
        "ck_application_profiles_underwriting_accepted_amount",
        "application_profiles",
        type_="check",
    )
    op.drop_column("application_profiles", "forecast_consulting_fee")
    op.drop_column("application_profiles", "underwriting_accepted_amount")
