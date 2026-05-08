"""Document.due_date — per-loan override of the computed due date.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-08

Lets the operator (or AI's tool-use loop, later) shift any single
document's due date for one specific loan without touching the
per-loan-type checklist defaults in app_settings. NULL = use the
default `requested_on + due_offset_days` math; non-NULL wins.

Surfaces as the per-row date picker on the new Workflow tab on the
loan detail page — operators can accelerate ("Bank Statements due
next Monday for this borrower") or push out a doc, and the AI's
collection scenarios re-classify on the next scan tick.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("due_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "due_date")
