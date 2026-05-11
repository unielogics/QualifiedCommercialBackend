"""Property listing-style fields + geocoding cache.

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-11

Adds the columns the new Property tab needs to render almost like a
listing page:

  • description           — agent-written marketing/property narrative
  • lot_size_sqft         — distinct from interior sqft
  • zoning                — short code (R-1 / C-2 / etc.)
  • parcel_id             — APN
  • listing_status        — short string ("on_market" / "off_market" /
                            "in_contract" / "closed") — drives a badge
  • highlight_features    — JSONB list of strings; rendered as chips
  • street_view_url       — optional override for a hero photo
  • latitude / longitude  — geocoded once; serves the Geoapify map
                            without re-geocoding on every render

All nullable to keep existing loans unbroken.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("loans", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("loans", sa.Column("lot_size_sqft", sa.Integer(), nullable=True))
    op.add_column("loans", sa.Column("zoning", sa.String(40), nullable=True))
    op.add_column("loans", sa.Column("parcel_id", sa.String(80), nullable=True))
    op.add_column("loans", sa.Column("listing_status", sa.String(32), nullable=True))
    op.add_column(
        "loans",
        sa.Column(
            "highlight_features",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("loans", sa.Column("street_view_url", sa.String(2000), nullable=True))
    op.add_column("loans", sa.Column("latitude", sa.Numeric(9, 6), nullable=True))
    op.add_column("loans", sa.Column("longitude", sa.Numeric(9, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("loans", "longitude")
    op.drop_column("loans", "latitude")
    op.drop_column("loans", "street_view_url")
    op.drop_column("loans", "highlight_features")
    op.drop_column("loans", "listing_status")
    op.drop_column("loans", "parcel_id")
    op.drop_column("loans", "zoning")
    op.drop_column("loans", "lot_size_sqft")
    op.drop_column("loans", "description")
