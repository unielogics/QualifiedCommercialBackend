"""Client terms carry their structure.

A loan is priced as simple interest under three years and amortizing from
three years by default, with any number of interest-only months first, or
interest-only to maturity; the client terms store which, so the plan, the
PDF and the package print the same schedule.

Revision ID: 0255_client_terms_structure
Revises: 0254_plaid_statements_default_on
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0255_client_terms_structure"
down_revision = "0254_plaid_statements_default_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_term_sheets", sa.Column("loan_structure", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "application_term_sheets", sa.Column("interest_only_months", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("application_term_sheets", "interest_only_months")
    op.drop_column("application_term_sheets", "loan_structure")
