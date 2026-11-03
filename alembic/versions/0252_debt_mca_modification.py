"""The merchant-cash-advance question on the business debt schedule.

A funder that lowered a remittance because sales fell has changed the
obligation a lender is being asked to read, so the schedule now asks whether
that happened and how. Two nullable columns, asked only of an advance; every
other kind of debt keeps null, because a row nobody asked must not claim an
answer.

Revision ID: 0252_debt_mca_modification
Revises: 0251_debt_schedule_everywhere
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0252_debt_mca_modification"
down_revision = "0251_debt_schedule_everywhere"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_debts", sa.Column("mca_modified", sa.Boolean(), nullable=True))
    op.add_column("dos_debts", sa.Column("mca_modification_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("dos_debts", "mca_modification_note")
    op.drop_column("dos_debts", "mca_modified")
