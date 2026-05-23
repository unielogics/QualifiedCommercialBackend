"""ai_agents.is_default_new_deal_{buyer,seller} — flag default agents.

Lets the broker star ONE agent per slot (buyer / seller new-deal
workflow) so the New Deal modal auto-picks it. The set-default
endpoint clears any prior holder before flipping the new one on.

Revision ID: 0067
Revises: 0066
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_agents",
        sa.Column(
            "is_default_new_deal_buyer",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.add_column(
        "ai_agents",
        sa.Column(
            "is_default_new_deal_seller",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_agents", "is_default_new_deal_seller")
    op.drop_column("ai_agents", "is_default_new_deal_buyer")
