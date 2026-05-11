"""AI Deal Secretary — task dependencies + parent grouping.

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-11

User direction: per-task config should NOT include hand-tuned cadence
hours. The system should sequence work itself — Next / In Progress /
Upcoming / Done — and group sub-tasks under a parent.

Two columns on AICollectionRequirement:

  depends_on JSONB
    Array of requirement_key strings this task waits for. The
    timeline resolver marks a task as 'Next Up' only when every
    key in depends_on resolves to a 'verified' (or equivalent)
    CRS status on the same loan. NULL / empty array = no
    dependencies, task is ready immediately.

  parent_key VARCHAR(120)
    Optional pointer to another row's requirement_key. Sub-tasks
    roll up under a parent in the workbench. The parent's
    aggregate state is computed from its children (all done →
    parent done; any in progress → parent in progress).

The user also asked for an AI-inferred-with-confirmation flow.
Two more columns support that:

  inferred_depends_on JSONB
    Same shape as depends_on but populated by the AI's suggestion
    pass. The UI shows these as dim "Suggested" chips that the
    user clicks to confirm (which moves them into depends_on).

  deps_confirmed BOOLEAN
    True once the user has reviewed the inferred deps. Until then,
    the UI shows a "Review suggested order" banner on the row.

Phase B will add the Claude-driven inference pass; for now these
columns just exist + are empty.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "depends_on",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column("parent_key", sa.String(120), nullable=True),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "inferred_depends_on",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "deps_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    # Index: timeline query needs to find children of a given parent.
    op.create_index(
        "ix_ai_collection_requirements_parent",
        "ai_collection_requirements",
        ["parent_key"],
        postgresql_where=sa.text("parent_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ai_collection_requirements_parent", table_name="ai_collection_requirements")
    op.drop_column("ai_collection_requirements", "deps_confirmed")
    op.drop_column("ai_collection_requirements", "inferred_depends_on")
    op.drop_column("ai_collection_requirements", "parent_key")
    op.drop_column("ai_collection_requirements", "depends_on")
