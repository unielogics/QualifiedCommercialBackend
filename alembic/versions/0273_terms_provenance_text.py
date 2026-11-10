"""Preserve full earnings/plan provenance when saving client terms.

Revision ID: 0273_terms_provenance_text
Revises: 0272_financial_snapshot_objects
"""

import sqlalchemy as sa

from alembic import op

revision = "0273_terms_provenance_text"
down_revision = "0272_financial_snapshot_objects"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("application_term_sheets", "cash_flow_label", existing_type=sa.String(80), type_=sa.Text(), existing_nullable=False)
    op.alter_column("application_term_sheets", "dscr_source", existing_type=sa.String(200), type_=sa.Text(), existing_nullable=False)


def downgrade():
    # PostgreSQL checks the original limits; an over-length historical source
    # prevents downgrade rather than being silently truncated.
    op.alter_column("application_term_sheets", "dscr_source", existing_type=sa.Text(), type_=sa.String(200), existing_nullable=False)
    op.alter_column("application_term_sheets", "cash_flow_label", existing_type=sa.Text(), type_=sa.String(80), existing_nullable=False)
