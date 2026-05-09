"""Client properties — first-class property records linked to clients.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-09

Replaces the loose "buyer_profile.target_property_type" / single
"seller_profile.property_address" approach with a real table that
lets one client carry many properties:

  - Buyer side: properties they're considering (target criteria, then
    specific addresses as they shortlist), each with status (active /
    offered / under_contract / closed / dropped).
  - Seller side: their listing(s) — most sellers have one, but
    repeat sellers / portfolio owners can have many.

A property can be linked to a Loan once underwriting starts
(linked_loan_id) so the loan workspace and the relationship
workspace agree on the same record.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "client_properties",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("side", sa.String(16), nullable=False),
        # buyer_target | seller_listing — drives whether the property
        # is something they want vs something they own.
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        # active | offered | under_contract | listed | sold | dropped | archived
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("zip", sa.String(10), nullable=True),
        sa.Column("property_type", sa.String(32), nullable=True),
        # single_family | multifamily | mixed_use | commercial | retail | office | industrial | land
        sa.Column("target_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("list_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("sold_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("bedrooms", sa.Integer(), nullable=True),
        sa.Column("bathrooms", sa.Numeric(3, 1), nullable=True),
        sa.Column("sqft", sa.Integer(), nullable=True),
        sa.Column("units", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "linked_loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_client_properties_lookup",
        "client_properties",
        ["client_id", "side", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_client_properties_lookup", table_name="client_properties")
    op.drop_table("client_properties")
