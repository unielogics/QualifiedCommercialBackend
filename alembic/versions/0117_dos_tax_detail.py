"""dos: keep the tax return's EBITDA components

_route_tax_years persisted only year + revenue_reported, so the numbers that
rebuild EBITDA — ordinary business income, officer compensation, interest,
depreciation and amortization — were extracted and then thrown away. That left
ebitda NULL on any dealer whose income statement lives only in tax returns
(bank statements carry no income statement), which in turn left DSCR null.

Revision ID: 0117_dos_tax_detail
Revises: 0116_dos_debts
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0117_dos_tax_detail"
down_revision = "0116_dos_debts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_tax_filings", sa.Column("detail", JSONB()))
    op.add_column("dos_tax_filings", sa.Column("entity_name", sa.String(180)))
    op.add_column("dos_tax_filings", sa.Column("form_type", sa.String(32)))


def downgrade() -> None:
    op.drop_column("dos_tax_filings", "form_type")
    op.drop_column("dos_tax_filings", "entity_name")
    op.drop_column("dos_tax_filings", "detail")
