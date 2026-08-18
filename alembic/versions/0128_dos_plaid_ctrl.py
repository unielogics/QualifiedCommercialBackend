"""dealer-os: Plaid connection controls.

auto_refresh — super-admin can stop/resume the 30-day automatic refresh per
bank. accounts_label — the connected accounts' names + last-4, captured from
Plaid at sync time so each connection row can say WHICH accounts it covers.

Revision ID: 0128_dos_plaid_ctrl
Revises: 0127_dos_plaid
"""

import sqlalchemy as sa
from alembic import op

revision = "0128_dos_plaid_ctrl"
down_revision = "0127_dos_plaid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_plaid_items",
        sa.Column("auto_refresh", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column("dos_plaid_items", sa.Column("accounts_label", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("dos_plaid_items", "accounts_label")
    op.drop_column("dos_plaid_items", "auto_refresh")
