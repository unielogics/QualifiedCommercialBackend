"""Dealer OS — regulated address fields on dealers (street + zip).

Revision ID: 0113_dos_dealer_address
Revises: 0112_dos_phase3
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0113_dos_dealer_address"
down_revision = "0112_dos_phase3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("address", sa.String(240)))
    op.add_column("dos_dealers", sa.Column("zip", sa.String(12)))


def downgrade() -> None:
    op.drop_column("dos_dealers", "zip")
    op.drop_column("dos_dealers", "address")
