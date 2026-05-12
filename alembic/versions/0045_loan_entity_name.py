"""Loan.entity_name — borrowing entity (LLC) name on the loan record.

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-12

Adds a per-loan `entity_name` column. We already had `entity_type`
(SOLE_PROP / LLC / CORP …) on the loan via alembic 0044; this is the
DBA / display name of the borrowing entity (e.g. "Smith Properties
LLC") so the loan header can show it alongside the borrower's
personal name + FICO.

The Client table has `name` and `fico` for the natural person; the
entity name belongs on the Loan because the same borrower may use
different LLCs across deals.

Nullable so existing rows continue to work.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("loans", sa.Column("entity_name", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("loans", "entity_name")
