"""legal_acceptances.disclosure_version — track the Funding/AI/
Communications/Platform Disclosure version a user accepted alongside
Terms + Privacy.

Added with the v1.0 (2026-05-19) legal-doc deploy. Nullable so existing
acceptance rows (which only captured Terms + Privacy) remain valid and
older clients that don't send the field can still POST /legal/accept.

Revision ID: 0061
Revises: 0060
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "legal_acceptances",
        sa.Column("disclosure_version", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("legal_acceptances", "disclosure_version")
