"""dealer-os: payment-shift direction (outflow later vs deposit earlier).

Every existing row is an outflow move ("pay later under vendor terms"), so
the server default 'out' is retroactively correct and stored est_adb_impact
values remain valid. 'in' rows are RECEIVABLES ACCELERATION — deposits may
only move EARLIER (enforced at the API layer), never timed around statement
cutoffs (the standing product guardrail).

Revision ID: 0121_dos_shift_direction
Revises: 0120_dos_desk_admin
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0121_dos_shift_direction"
down_revision = "0120_dos_desk_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_payment_shifts",
        sa.Column("direction", sa.String(8), nullable=False, server_default="out"),
    )


def downgrade() -> None:
    op.drop_column("dos_payment_shifts", "direction")
