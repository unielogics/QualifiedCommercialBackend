"""Living Loan Profile — structured 4-section output from 'The Associate'

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-05

Adds loans.living_profile (JSONB) — the structured output of the upgraded
summarizer ("The Associate"). The existing loans.status_summary stays as
the fallback plain-text rendering for older clients / activity log entries.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "loans",
        sa.Column("living_profile", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("loans", "living_profile")
