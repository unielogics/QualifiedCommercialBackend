"""Statement PDFs on for every file.

The owner: "pdf files should be enabled for every file". A bank-produced
statement is the evidence a lender reads, so a file no longer has to be
switched on one at a time by a super admin. The flag is a preference: the
deployment allowlist (DEALER_OS_PLAID_PRODUCTS) decides whether Plaid is
actually asked for the product, so flipping it is safe while the Plaid account
is still Assets-only. A file whose policy a person set deliberately
(plaid_policy_updated_at) keeps that choice.

Revision ID: 0254_plaid_statements_default_on
Revises: 0253_cp_package_documents
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0254_plaid_statements_default_on"
down_revision = "0253_cp_package_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("application_profiles", "dos_dealers"):
        op.alter_column(table, "plaid_statements_enabled", server_default=sa.text("true"))
        op.execute(
            f"UPDATE {table} SET plaid_statements_enabled = true "
            "WHERE plaid_statements_enabled = false AND plaid_policy_updated_at IS NULL"
        )


def downgrade() -> None:
    for table in ("application_profiles", "dos_dealers"):
        op.alter_column(table, "plaid_statements_enabled", server_default=sa.text("false"))
