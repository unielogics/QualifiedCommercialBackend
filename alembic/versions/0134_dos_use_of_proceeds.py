"""dos_dealers.use_of_proceeds — what the money is for, line by line

A funding_goal answers "how much" and no lender asks only that. The breakdown
is what a credit file wants, and it is also what catches a request nobody has
thought through.

Revision ID: 0134_dos_use_of_proceeds
Revises: 0133_dos_case_ref
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0134_dos_use_of_proceeds"
down_revision = "0133_dos_case_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("use_of_proceeds", JSONB()))
    op.add_column("dos_dealers", sa.Column("use_of_proceeds_note", sa.Text()))


def downgrade() -> None:
    op.drop_column("dos_dealers", "use_of_proceeds_note")
    op.drop_column("dos_dealers", "use_of_proceeds")
