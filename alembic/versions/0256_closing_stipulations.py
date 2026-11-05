"""Stipulations to close on the profile.

Once the client accepts an offer the desk lists what must be in hand before
funding (the terms' conditions seed it, the desk adds its own) and marks
each received or waived; the estimated close date sits beside it.

Revision ID: 0256_closing_stipulations
Revises: 0255_client_terms_structure
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0256_closing_stipulations"
down_revision = "0255_client_terms_structure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column(
            "closing_stipulations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("application_profiles", "closing_stipulations")
