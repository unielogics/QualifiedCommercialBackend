"""Deal.notes_entries — timestamped agent notes (floating widget).

Revision ID: 0053
Revises: 0052
Create Date: 2026-05-12

Replaces the single-blob notes_text (0052) with a JSONB array so the
floating Notes widget on /deals/[id] can track per-date/time entries.

Migration is additive — notes_text stays for backward compat. The
widget reads/writes notes_entries; if it's NULL the widget falls
back to displaying notes_text as a single legacy entry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column(
            "notes_entries",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("deals", "notes_entries")
