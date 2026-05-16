"""Loan.collection_starts_on — delayed document-collection start.

NULL = collection started immediately at kickoff (the existing
behavior for every loan to date and every non-broker caller). Set by
the broker new-file modals when the broker delays outreach.

Revision ID: 0060
Revises: 0059
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loans",
        sa.Column("collection_starts_on", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("loans", "collection_starts_on")
