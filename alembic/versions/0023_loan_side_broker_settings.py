"""Loan.side + Broker.settings_data — agent overlay + buyer/seller split.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-08

Two changes that unlock the agent-settings parity work:

1. `loans.side` (buyer | seller) — captured at intake; doc-collection
   cron filters checklist items by `item.side ∈ (loan.side, 'both')`.
   Defaults to 'buyer' since that's the dominant case in the current
   pipeline. Existing rows get backfilled to 'buyer'.

2. `brokers.settings_data` JSONB — per-broker overlay layered on top
   of the firm checklist. Lets each agent disable specific firm
   items + add their own custom rows for their loans. Also stores
   per-broker AI cadence overrides and personal letterhead. NULL
   means "use firm defaults as-is".

Pure column adds — no breaking changes to any existing query.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "loans",
        sa.Column(
            "side",
            sa.String(length=8),
            nullable=False,
            server_default="buyer",
        ),
    )
    op.add_column(
        "brokers",
        sa.Column(
            "settings_data",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("brokers", "settings_data")
    op.drop_column("loans", "side")
