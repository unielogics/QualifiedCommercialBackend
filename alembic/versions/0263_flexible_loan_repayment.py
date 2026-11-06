"""Preserve versioned flexible loan repayment instructions."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0263_flexible_loan_repayment"
down_revision = "0262_financial_metric_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("application_term_sheets", sa.Column("repayment_config", postgresql.JSONB(), nullable=True))
    op.drop_constraint("ck_application_term_sheet_payment", "application_term_sheets", type_="check")
    op.create_check_constraint("ck_application_term_sheet_payment", "application_term_sheets", "periodic_payment > 0 OR (repayment_config IS NOT NULL AND jsonb_typeof(repayment_config) = 'object' AND periodic_payment >= 0)")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM application_term_sheets
        WHERE repayment_config IS NOT NULL) THEN
        RAISE EXCEPTION 'Flexible repayment terms exist; retain versioned terms before downgrading';
        END IF; END; $$""")
    op.drop_constraint("ck_application_term_sheet_payment", "application_term_sheets", type_="check")
    op.create_check_constraint("ck_application_term_sheet_payment", "application_term_sheets", "periodic_payment > 0")
    op.drop_column("application_term_sheets", "repayment_config")
