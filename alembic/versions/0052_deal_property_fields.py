"""Deal property + notes columns (Phase 8 — agent-file rebuild).

Revision ID: 0052
Revises: 0051
Create Date: 2026-05-12

Adds the property-snapshot columns and the private notes column the
agent edits on the Property + Notes tabs of /deals/[id]. The property
fields get copied onto the new Loan at promote_deal_to_loan time so
funding sees the snapshot.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


_NEW_COLS = [
    ("address", sa.String(320)),
    ("city", sa.String(160)),
    ("state", sa.String(2)),
    ("zip", sa.String(10)),
    ("property_type", sa.String(32)),
    ("beds", sa.Integer()),
    ("baths", sa.Numeric(4, 1)),
    ("sqft", sa.Integer()),
    ("year_built", sa.Integer()),
    ("list_price", sa.Numeric(14, 2)),
    ("target_price", sa.Numeric(14, 2)),
    ("listing_status", sa.String(32)),
    ("mls_number", sa.String(40)),
    ("notes_text", sa.Text()),
]


def upgrade() -> None:
    for name, type_ in _NEW_COLS:
        op.add_column("deals", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_NEW_COLS):
        op.drop_column("deals", name)
