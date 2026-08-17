"""dealer-os: client responses + comment threads on plan actions.

The published plan is the instruction set we hand the client ("call this
vendor, request the payment-date move"). The client must be able to
ACCEPT or DECLINE each action and discuss it — and the response feeds
back into the simulation: a declined action dismisses its linked payment
shift (its ADB lift leaves the optimized scenario), a completed one
keeps counting until real statements absorb it.

Revision ID: 0123_dos_plan_responses
Revises: 0122_dos_shift_plan_link
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0123_dos_plan_responses"
down_revision = "0122_dos_shift_plan_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_plan_actions",
        sa.Column("client_response", sa.String(16), nullable=True),  # accepted|declined
    )
    op.add_column(
        "dos_plan_actions",
        sa.Column("client_response_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "dos_plan_comments",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "action_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dos_plan_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("author_role", sa.String(16), nullable=False),  # team|dealer
        sa.Column("author_name", sa.String(120), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dos_plan_comments_action", "dos_plan_comments", ["action_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dos_plan_comments_action", table_name="dos_plan_comments")
    op.drop_table("dos_plan_comments")
    op.drop_column("dos_plan_actions", "client_response_at")
    op.drop_column("dos_plan_actions", "client_response")
