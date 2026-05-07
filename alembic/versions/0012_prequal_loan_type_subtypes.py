"""Pre-qualification loan_type sub-types — DSCR splits into purchase/refi
and Fix & Flip becomes a first-class option alongside Bridge.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-07

The prequal letter intake form was conflating "DSCR Rental" into a single
loan_type code. Reality: DSCR comes in two flavors (Purchase vs Refinance)
with materially different LTV caps, and Fix & Flip was being skipped
entirely. This migration just remaps existing rows — the column type
(VARCHAR(16)) doesn't change, application code now accepts:

    dscr_purchase | dscr_refi | fix_flip | bridge

Old rows that submitted `loan_type='dscr'` are remapped to `dscr_purchase`
since that was the documented default in the picker (refi wasn't even an
option before this change).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE prequal_requests SET loan_type = 'dscr_purchase' WHERE loan_type = 'dscr'"
    )


def downgrade() -> None:
    # Collapse all DSCR variants back to the legacy single value. We
    # can't recover purchase-vs-refi distinction once we downgrade, so
    # the underwriter would have to re-classify on the spawned loan.
    op.execute(
        "UPDATE prequal_requests SET loan_type = 'dscr' WHERE loan_type IN ('dscr_purchase', 'dscr_refi')"
    )
    op.execute(
        "UPDATE prequal_requests SET loan_type = 'bridge' WHERE loan_type = 'fix_flip'"
    )
