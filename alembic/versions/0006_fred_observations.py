"""FRED observations + lender spreads

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-05

Adds:
  - fred_observations  — daily FRED API time-series storage
  - lender_spreads     — basis-point spread per FRED series, append-only audit trail

Seeds the four "must-have commercial bases" with sensible default spreads so
the dashboard has something to show before the first POST /admin/fred/refresh.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Default spreads in basis points. Conservative starting points; super-admin
# updates them in Settings → Pricing once the team locks in real numbers.
SEED_SPREADS = [
    ("DGS10", 215, "Default spread vs 10-Year Treasury (DSCR 30-yr)"),
    ("SOFR", 350, "Default spread vs SOFR (Bridge / floating-rate)"),
    ("DPRIME", 200, "Default spread vs Bank Prime (Fix & Flip / Ground Up)"),
    ("DGS5", 250, "Default spread vs 5-Year Treasury (5-year hybrid products)"),
]


def upgrade() -> None:
    op.create_table(
        "fred_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("series_id", sa.String(32), nullable=False, index=True),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(10, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("series_id", "observation_date", name="uq_fred_series_date"),
    )

    op.create_table(
        "lender_spreads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("series_id", sa.String(32), nullable=False, index=True),
        sa.Column("spread_bps", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Seed default spreads so the dashboard widget has values on day one.
    spreads_table = sa.table(
        "lender_spreads",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("series_id", sa.String),
        sa.column("spread_bps", sa.Integer),
        sa.column("notes", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        spreads_table,
        [
            {
                "id": uuid.uuid4(),
                "series_id": series_id,
                "spread_bps": bps,
                "notes": note,
                "created_at": now,
                "updated_at": now,
            }
            for series_id, bps, note in SEED_SPREADS
        ],
    )


def downgrade() -> None:
    op.drop_table("lender_spreads")
    op.drop_table("fred_observations")
