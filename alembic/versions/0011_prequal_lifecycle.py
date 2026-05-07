"""Pre-qualification lifecycle change — submit no longer spawns a Loan,
the borrower's seller-accept is what creates one.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-07

Schema changes:
  • prequal_requests.loan_id  → NULLABLE (only set when borrower marks
                                  the seller's offer as accepted; until
                                  then the request lives without a loan)
  • prequal_requests.approved_scenario  → JSONB NULL (calculator snapshot
                                  the underwriter saved on approve —
                                  drives the PDF and pre-fills the loan
                                  on conversion)
  • prequal_requests.quote_number  → VARCHAR(16) NULL (set on
                                  offer_accepted; format Q-{4-digit})

Status enum is unchanged at the column level — it stays a plain
VARCHAR. Application code now uses the values:

    pending → approved → offer_accepted | offer_declined
                ↓
              rejected

(Existing rows are 'pending' / 'approved' / 'rejected' and that's still
valid — only new accept/decline values are added by the application.)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # loan_id was NOT NULL in 0010 — relax it. The new lifecycle only
    # populates it when the seller accepts and we create the Loan.
    op.alter_column(
        "prequal_requests",
        "loan_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "prequal_requests",
        sa.Column("approved_scenario", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "prequal_requests",
        sa.Column("quote_number", sa.String(16), nullable=True),
    )
    # Borrower-supplied entity / LLC name used on the letter. NULL = TBD
    # (the borrower will form the LLC later — letter falls back to the
    # individual client's legal name in that case).
    op.add_column(
        "prequal_requests",
        sa.Column("borrower_entity", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prequal_requests", "borrower_entity")
    op.drop_column("prequal_requests", "quote_number")
    op.drop_column("prequal_requests", "approved_scenario")
    # NOTE: tightening loan_id back to NOT NULL would fail if any rows
    # have loan_id IS NULL. Skip the constraint flip on downgrade.
    op.alter_column(
        "prequal_requests",
        "loan_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
