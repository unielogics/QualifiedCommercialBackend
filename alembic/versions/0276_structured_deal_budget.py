"""Audited deal costs and immutable terms cost snapshots.

Revision ID: 0276_structured_deal_budget
Revises: 0275_terms_collateral_snapshot
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0276_structured_deal_budget"
down_revision = "0275_terms_collateral_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("application_profiles", sa.Column("deal_budget", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    for name in ("deal_budget_snapshot", "deal_budget_calculation"):
        op.add_column("application_term_sheets", sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("application_term_sheets", sa.Column("deal_budget_revision", sa.Integer(), nullable=True))


def downgrade():
    for name in ("deal_budget_revision", "deal_budget_calculation", "deal_budget_snapshot"):
        op.drop_column("application_term_sheets", name)
    op.drop_column("application_profiles", "deal_budget")
