"""bedrock ai usage default.

Revision ID: 0079_bedrock_ai_usage_default
Revises: 0078
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079_bedrock_ai_usage_default"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ai_usage_events",
        "provider",
        existing_type=sa.String(length=32),
        server_default="bedrock",
    )


def downgrade() -> None:
    op.alter_column(
        "ai_usage_events",
        "provider",
        existing_type=sa.String(length=32),
        server_default="anthropic",
    )
