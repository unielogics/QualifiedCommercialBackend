"""Normalize the dealer intake variant to a single canonical marker.

Car-dealer intakes were stored with variant "dealer_financing_v1" (the historical
column default) while their AI persona marker (bucket.ai_context.review_type) was
"dealer_gatekeeper_v1" — two names for one product. Real-estate intakes already use
"real_estate_dscr_v1" for both. This backfills existing car rows to the canonical
"dealer_gatekeeper_v1" so variant == review_type for both products, and moves the
column default to match. Real-estate rows are untouched.

Revision ID: 0090_normalize_dealer_variant
Revises: 0089_public_underwriting_artifacts
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0090_normalize_dealer_variant"
down_revision = "0089_public_underwriting_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE public_underwriting_intakes "
        "SET variant = 'dealer_gatekeeper_v1' "
        "WHERE variant = 'dealer_financing_v1'"
    )
    op.alter_column(
        "public_underwriting_intakes",
        "variant",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default="dealer_gatekeeper_v1",
    )


def downgrade() -> None:
    op.alter_column(
        "public_underwriting_intakes",
        "variant",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        server_default="dealer_financing_v1",
    )
    op.execute(
        "UPDATE public_underwriting_intakes "
        "SET variant = 'dealer_financing_v1' "
        "WHERE variant = 'dealer_gatekeeper_v1'"
    )
