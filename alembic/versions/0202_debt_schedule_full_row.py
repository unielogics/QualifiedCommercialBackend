"""The five debt-schedule fields a lender asks for and we had nowhere to put.

`dos_debts` already carried lender, balance, monthly payment, rate, term,
maturity, collateral and notes. A real schedule also states what the debt IS
(term loan, line of credit, equipment), what it started at, when it started,
and whether it is secured and being paid on time. Without those the borrower's
answer had to be squeezed into the notes field or lost, and the underwriter
went back to ask for a schedule they had already been sent.

Four columns, not five: `collateral` has been on this table since 0138 and the
form simply never collected it.

All nullable with no default: every existing row is a row nobody was asked
these questions, and inventing "current" or "unsecured" for it would be stating
something we were never told.
"""

from alembic import op
import sqlalchemy as sa

revision = "0202_debt_schedule_full_row"
down_revision = "0201_file_team_and_timeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_debts", sa.Column("original_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("dos_debts", sa.Column("originated_on", sa.Date(), nullable=True))
    # Short free-ish strings rather than enums: a schedule that arrives saying
    # "partially secured" should be recorded, not rejected at the boundary.
    op.add_column("dos_debts", sa.Column("secured", sa.String(16), nullable=True))
    op.add_column("dos_debts", sa.Column("payment_status", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("dos_debts", "payment_status")
    op.drop_column("dos_debts", "secured")
    op.drop_column("dos_debts", "originated_on")
    op.drop_column("dos_debts", "original_amount")
