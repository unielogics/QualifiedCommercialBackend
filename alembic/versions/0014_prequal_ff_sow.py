"""Fix & Flip prequal — collect ARV + scope-of-work alongside BRV.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-07

The Fix & Flip prequal flow was treating `purchase_price` as the
property's after-repair value when it's actually the BRV (Before
Repair Value — what the borrower is paying for the as-is property).
The system never collected the ARV (After Repair Value) or the
borrower's scope-of-work breakdown, so underwriting couldn't run
the LTARV math: (loan_amount + construction) / arv ≤ matrix cap.

Schema additions on `prequal_requests` (all nullable; only F&F
populates them):

  arv_estimate           NUMERIC(14,2) — borrower's stated ARV
  approved_arv           NUMERIC(14,2) — admin's verified / overridden ARV
  sow_items              JSONB — list of {category, description, total_usd}
                                  rows. Sums to total_construction.
  total_construction     NUMERIC(14,2) — derived from sow_items.sum(total_usd).
                                  Stored for query speed + admin override
                                  (admin can adjust without touching individual
                                  line items).
  approved_total_construction  NUMERIC(14,2) — admin override

The PDF rendering doesn't change — none of these fields surface on
the printed letter. Sellers continue to see the deliberately
stripped "Negotiation Shield" version. These columns drive the
underwriting math and feed the approved_scenario JSONB which the
spawned Loan reads on offer-accepted.

Existing F&F rows backfill safely — every column is nullable.
Operators going forward will be guided through the 2-step intake
on desktop / mobile.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prequal_requests",
        sa.Column("arv_estimate", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "prequal_requests",
        sa.Column("approved_arv", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "prequal_requests",
        sa.Column(
            "sow_items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "prequal_requests",
        sa.Column("total_construction", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "prequal_requests",
        sa.Column("approved_total_construction", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prequal_requests", "approved_total_construction")
    op.drop_column("prequal_requests", "total_construction")
    op.drop_column("prequal_requests", "sow_items")
    op.drop_column("prequal_requests", "approved_arv")
    op.drop_column("prequal_requests", "arv_estimate")
