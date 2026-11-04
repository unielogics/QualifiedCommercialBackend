"""Listing documents may be the file's own package.

The desk pushes the executive credit brief and the lender packet from the
file onto a listing; the kind check on listing documents learns the two
kinds.

Revision ID: 0253_cp_package_documents
Revises: 0252_debt_mca_modification
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0253_cp_package_documents"
down_revision = "0252_debt_mca_modification"
branch_labels = None
depends_on = None

OLD = ["note", "other", "ppm", "servicing_summary", "subscription_agreement", "title", "valuation"]
NEW = sorted([*OLD, "executive_summary", "underwriting_package"])


def _constraint(values: list[str]) -> str:
    return "kind IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_cp_listing_documents_kind", "cp_listing_documents", type_="check")
    op.create_check_constraint(
        "ck_cp_listing_documents_kind", "cp_listing_documents", _constraint(NEW)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE cp_listing_documents SET kind = 'other' WHERE kind IN ('executive_summary', 'underwriting_package')"
        )
    )
    op.drop_constraint("ck_cp_listing_documents_kind", "cp_listing_documents", type_="check")
    op.create_check_constraint(
        "ck_cp_listing_documents_kind", "cp_listing_documents", _constraint(OLD)
    )
