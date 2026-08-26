"""Step 5 funded-amount finalization.

Revision ID: 0156_step5_finalization
Revises: 0155_repair_naics_application_handoff
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0156_step5_finalization"
down_revision = "0155_repair_naics_application_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("funded_amount", sa.Numeric(14, 2)))


def downgrade() -> None:
    op.drop_column("dos_dealers", "funded_amount")
