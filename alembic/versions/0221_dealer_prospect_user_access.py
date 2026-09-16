"""Add per-user Dealer Prospect Pipeline entitlement.

Revision ID: 0221_dealer_prospect_user_access
Revises: 0220_prospect_outreach
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0221_dealer_prospect_user_access"
down_revision = "0220_prospect_outreach"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fail closed: the deployment can apply the schema and configure the pilot
    # population before enabling the global master switch.
    op.add_column(
        "users",
        sa.Column(
            "dealer_prospect_pipeline_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "dealer_prospect_pipeline_enabled")
