"""Widen loans.base_rate / loans.final_rate from NUMERIC(7,6) to NUMERIC(9,6).

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-08

The old columns capped at 9.999999, which overflows on real fix-and-flip
and bridge rates (routinely 10-15%). Intake POSTs were 500ing in
production with `numeric field overflow` — frontend saw a CORS error
because the exception bypassed the CORS middleware.

NUMERIC(9,6) keeps the existing 6-decimal precision and allows up to
999.999999 — plenty of headroom for any reasonable interest rate.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "loans",
        "base_rate",
        existing_type=sa.Numeric(7, 6),
        type_=sa.Numeric(9, 6),
        existing_nullable=True,
    )
    op.alter_column(
        "loans",
        "final_rate",
        existing_type=sa.Numeric(7, 6),
        type_=sa.Numeric(9, 6),
        existing_nullable=True,
    )


def downgrade() -> None:
    # NB: downgrade only safe if no row currently exceeds 9.999999.
    op.alter_column(
        "loans",
        "final_rate",
        existing_type=sa.Numeric(9, 6),
        type_=sa.Numeric(7, 6),
        existing_nullable=True,
    )
    op.alter_column(
        "loans",
        "base_rate",
        existing_type=sa.Numeric(9, 6),
        type_=sa.Numeric(7, 6),
        existing_nullable=True,
    )
