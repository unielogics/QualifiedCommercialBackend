"""Legal acceptances — Terms + Privacy consent audit trail

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-05

Records each time a user accepts the Terms of Service + Privacy Policy
(captured at signup or whenever the documents are versioned and the user
has to re-accept). Stored fields are sufficient for TCPA / GLBA audit:
  - which user accepted
  - which version of each document
  - when (created_at)
  - server-captured IP + user agent at acceptance time
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "legal_acceptances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("terms_version", sa.String(32), nullable=False),
        sa.Column("privacy_version", sa.String(32), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("legal_acceptances")
