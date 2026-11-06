"""Bind shared worksheet sheets to their selected financial families."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0264_client_form_edit_scope"
down_revision = "0263_flexible_loan_repayment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("financial_form_link_sheets", sa.Column("financial_family_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_form_link_sheet_family", "financial_form_link_sheets", "financial_form_families", ["financial_family_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_financial_form_link_sheets_financial_family_id", "financial_form_link_sheets", ["financial_family_id"])


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM financial_form_link_sheets
        WHERE financial_family_id IS NOT NULL) THEN
        RAISE EXCEPTION 'Bound client financial form scopes exist; preserve them before downgrading';
        END IF; END; $$""")
    op.drop_index("ix_financial_form_link_sheets_financial_family_id", table_name="financial_form_link_sheets")
    op.drop_constraint("fk_form_link_sheet_family", "financial_form_link_sheets", type_="foreignkey")
    op.drop_column("financial_form_link_sheets", "financial_family_id")
