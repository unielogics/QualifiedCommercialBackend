"""Store audited file-scoped proposed real estate collateral.

Revision ID: 0274_investor_collateral
Revises: 0273_terms_provenance_text
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0274_investor_collateral"
down_revision = "0273_terms_provenance_text"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("application_profiles", sa.Column("investor_collateral", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column("application_profiles", "investor_collateral")
