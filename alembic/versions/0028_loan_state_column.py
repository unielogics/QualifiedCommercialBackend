"""Add loans.state (USPS 2-letter code).

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-08

The desktop's address-collection forms now split city + state into
separate fields with a state dropdown. This migration adds the
backing column on loans so AssetStep.state from /intake actually
persists. Nullable — existing rows just leave it NULL until the next
edit through a form that captures it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "loans",
        sa.Column("state", sa.String(2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("loans", "state")
