"""Freeze proposed collateral on each saved client terms version.

Revision ID: 0275_terms_collateral_snapshot
Revises: 0274_investor_collateral
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0275_terms_collateral_snapshot"
down_revision = "0274_investor_collateral"
branch_labels = None
depends_on = None


def upgrade():
    # Existing terms have no truthful historic collateral snapshot to backfill.
    op.add_column("application_term_sheets", sa.Column("collateral_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("application_term_sheets", sa.Column("collateral_revision", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("application_term_sheets", "collateral_revision")
    op.drop_column("application_term_sheets", "collateral_snapshot")
