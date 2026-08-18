"""dealer-os: refinance workbench — contract terms on dos_debts.

payment_amount/payment_frequency carry the contract's native cadence
(an MCA debits $420/day — monthly_payment stays the monthly equivalent),
factor_rate distinguishes MCA pricing from APR, payoff_amount is the
contract-derived payoff estimate, document_id links the row to the
uploaded agreement it was extracted from.

Revision ID: 0126_dos_refi
Revises: 0125_dos_owner_invites
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0126_dos_refi"
down_revision = "0125_dos_owner_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_debts", sa.Column("payment_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("dos_debts", sa.Column("payment_frequency", sa.String(12), nullable=True))
    op.add_column("dos_debts", sa.Column("factor_rate", sa.Numeric(6, 3), nullable=True))
    op.add_column("dos_debts", sa.Column("payoff_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column(
        "dos_debts",
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dos_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dos_debts", "document_id")
    op.drop_column("dos_debts", "payoff_amount")
    op.drop_column("dos_debts", "factor_rate")
    op.drop_column("dos_debts", "payment_frequency")
    op.drop_column("dos_debts", "payment_amount")
