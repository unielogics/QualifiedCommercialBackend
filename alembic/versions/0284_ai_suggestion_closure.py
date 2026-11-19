"""Let an AI suggestion be closed when its file leaves active work.

A suggestion waits for the desk to approve or dismiss it. A file marked lost
or denied keeps its room open on purpose (its evidence stays readable), so the
suggestions on it waited forever: on 2026-11-19, 116 of the 283 the dashboard
counted sat on denied or lost files. Closing is a status with a reason and a
time, the contract an archived room's open asks already keep (0243), so a file
that comes back reopens exactly what its closing closed.

No rows change here. The rule for "this file has left active work" is the
application's (``services/ai_suggestion_closure``), and the sweep it runs a
few minutes after start closes the backlog by that rule.

Revision ID: 0284_ai_suggestion_closure
Revises: 0283_resource_assessments
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0284_ai_suggestion_closure"
down_revision = "0283_resource_assessments"
branch_labels = None
depends_on = None

TABLE = "bucket_ai_action_items"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("closed_reason", sa.String(length=40), nullable=True))
    # No new index: 0084 already indexes status and bucket_id.


def downgrade() -> None:
    # A closed suggestion goes back to waiting: without the columns nothing
    # could tell it apart from one a person dismissed.
    op.execute(sa.text(f"UPDATE {TABLE} SET status = 'proposed' WHERE status = 'closed'"))
    op.drop_column(TABLE, "closed_reason")
    op.drop_column(TABLE, "closed_at")
