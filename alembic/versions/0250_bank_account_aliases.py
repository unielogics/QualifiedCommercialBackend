"""Accounts the desk declared to be one and the same.

The statement reader prints an account's digits differently from statement
to statement, or reads another account's digits off a multi-account
statement, and one checking account turns into several on the cockpit. The
desk groups them here: a map from an account key to the key it belongs to.

Revision ID: 0250_bank_account_aliases
Revises: 0249_financial_plan
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0250_bank_account_aliases"
down_revision = "0249_financial_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("bank_account_aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("application_profiles", "bank_account_aliases")
