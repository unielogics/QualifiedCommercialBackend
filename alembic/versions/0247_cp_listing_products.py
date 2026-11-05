"""Products offered on a capital-partner listing.

An unfunded file is listed with suggested terms, and the desk may offer more
than one product on it (a ten-year term loan and a shorter advance refinance,
say). Each product carries its own amount, rate, term, payment and the
coverage it leaves; the primary one supplies the listing's headline terms.

Revision ID: 0247_cp_listing_products
Revises: 0246_cp_core
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0247_cp_listing_products"
down_revision = "0246_cp_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cp_listings",
        sa.Column("products", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("cp_listings", "products")
