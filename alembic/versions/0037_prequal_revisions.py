"""Prequalification revisions — linked-version chains.

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-11

When an operator needs to update an approved prequalification (e.g.
market shift, borrower phones in for a higher amount), the system now
spawns a new prequal_requests row linked back to the source via
parent_prequal_request_id. The source's superseded_by_id is set to the
new row's id, so the chain is navigable in either direction. The
quote_number on the new row carries a -vN suffix (Q-1042 → Q-1042-v2).

Three new columns:

  parent_prequal_request_id  UUID, FK to prequal_requests(id). NULL on
                             the original; set on every revision to
                             point at the immediate predecessor.

  version_num                INT NOT NULL, default 1. 1 for originals
                             (and all existing rows after backfill); 2,
                             3, ... for successive revisions in the chain.

  superseded_by_id           UUID, FK to prequal_requests(id), UNIQUE.
                             Set on the predecessor when a revision is
                             created — the UNIQUE constraint enforces
                             that a given row can only be superseded
                             once (the chain is strictly linear).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prequal_requests",
        sa.Column(
            "parent_prequal_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prequal_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "prequal_requests",
        sa.Column(
            "version_num",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "prequal_requests",
        sa.Column(
            "superseded_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prequal_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_prequal_requests_parent",
        "prequal_requests",
        ["parent_prequal_request_id"],
    )
    op.create_unique_constraint(
        "uq_prequal_requests_superseded_by",
        "prequal_requests",
        ["superseded_by_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_prequal_requests_superseded_by",
        "prequal_requests",
        type_="unique",
    )
    op.drop_index("ix_prequal_requests_parent", table_name="prequal_requests")
    op.drop_column("prequal_requests", "superseded_by_id")
    op.drop_column("prequal_requests", "version_num")
    op.drop_column("prequal_requests", "parent_prequal_request_id")
