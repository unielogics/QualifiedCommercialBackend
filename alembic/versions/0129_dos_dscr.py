"""dealer-os: the debt schedule as the DSCR denominator ledger.

count_in_dscr — the per-row include/exclude law for the DSCR denominator.
The previous implicit rule (AI-drafted credit cards excluded) becomes an
explicit, visible, toggleable flag: the data migration stamps those rows
false so behavior is unchanged, but now the desk can see and flip it.

Revision ID: 0129_dos_dscr
Revises: 0128_dos_plaid_ctrl
"""

import sqlalchemy as sa
from alembic import op

revision = "0129_dos_dscr"
down_revision = "0128_dos_plaid_ctrl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_debts",
        sa.Column("count_in_dscr", sa.Boolean(), nullable=False, server_default="true"),
    )
    # Preserve the shipped behavior: unconfirmed card rows stay out of the
    # denominator — but as data the desk can now see and toggle.
    op.execute(
        "UPDATE dos_debts SET count_in_dscr = false "
        "WHERE category = 'credit_card' AND origin != 'admin'"
    )


def downgrade() -> None:
    op.drop_column("dos_debts", "count_in_dscr")
