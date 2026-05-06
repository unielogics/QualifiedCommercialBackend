"""Client investor profile fields: address, properties, experience

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-05

Adds three nullable text fields on `clients` so the Profile page's
Investor Profile dialog (properties owned + experience together) and a
mailing address have somewhere to live.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("address", sa.String(320), nullable=True))
    op.add_column("clients", sa.Column("properties", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("experience", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "experience")
    op.drop_column("clients", "properties")
    op.drop_column("clients", "address")
