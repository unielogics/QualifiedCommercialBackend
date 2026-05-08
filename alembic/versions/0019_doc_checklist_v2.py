"""Loan.unit_count + extended doc checklist (per-item config).

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-08

The doc-checklist substrate grew up with one global cadence per
loan-type and a single `(name, required, auto_request)` per item.
Operator surfaced four gaps:

  - internal vs external ownership isn't modeled (Appraisal,
    Insurance Binder, Title, PFS are operator-ordered)
  - per-unit fan-out (4-plex needs 4 leases, not 1)
  - per-item due-offset relative to a trigger event
  - dependency anchors ("Appraisal 2 days after Bank Statements
    received")

The DocChecklistItem extensions are pure Pydantic — they live in
the existing `app_settings.data` JSONB blob, so no DB column
changes there. Every new field has a default so older blobs still
parse cleanly.

The one DB change is `loans.unit_count INTEGER NULL DEFAULT 1`,
which drives the per-unit fan-out at kickoff time and surfaces on
the desktop loan detail page's Property tab next to beds / baths /
sqft / year_built.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "loans",
        sa.Column(
            "unit_count",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("1"),
        ),
    )
    # Backfill existing rows to 1 (single-family default). The
    # server_default keeps fresh inserts at 1; existing rows that
    # were NULL (none today, but defensive) get set explicitly.
    op.execute("UPDATE loans SET unit_count = 1 WHERE unit_count IS NULL")


def downgrade() -> None:
    op.drop_column("loans", "unit_count")
