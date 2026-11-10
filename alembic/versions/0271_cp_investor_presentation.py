"""Keep the investor presentation as its own versioned listing document.

Revision ID: 0271_cp_investor_presentation
Revises: 0270_operating_plan_form
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0271_cp_investor_presentation"
down_revision = "0270_operating_plan_form"
branch_labels = None
depends_on = None

OLD = ["executive_summary", "note", "other", "ppm", "servicing_summary", "subscription_agreement", "title", "underwriting_package", "valuation"]
NEW = sorted([*OLD, "investor_presentation"])


def _constraint(values):
    return "kind IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade():
    op.drop_constraint("ck_cp_listing_documents_kind", "cp_listing_documents", type_="check")
    op.create_check_constraint("ck_cp_listing_documents_kind", "cp_listing_documents", _constraint(NEW))


def downgrade():
    # The PDF, its name, access controls and version remain intact.
    op.execute(sa.text("UPDATE cp_listing_documents SET kind = 'other' WHERE kind = 'investor_presentation'"))
    op.drop_constraint("ck_cp_listing_documents_kind", "cp_listing_documents", type_="check")
    op.create_check_constraint("ck_cp_listing_documents_kind", "cp_listing_documents", _constraint(OLD))
