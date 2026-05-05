"""Deal Workspace: instructions, chat, corrections, scenarios, AI feedback

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-05

Adds the per-loan AI workspace data model:
  - loans.ai_paused_until (column)         — engagement pause for super-admin overrides
  - loan_instructions                      — persistent loan-scoped directives
  - loan_chat_messages                     — persisted per-loan AI conversation
  - ai_modify_corrections                  — super-admin corrections on past AI turns
  - loan_scenarios                         — named simulator snapshots per loan
  - ai_feedback                            — polymorphic thumbs/comments on AI outputs
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Engagement pause column on loans
    op.add_column(
        "loans",
        sa.Column("ai_paused_until", sa.DateTime(timezone=True), nullable=True),
    )

    # 2) loan_instructions
    op.create_table(
        "loan_instructions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 3) loan_chat_messages
    op.create_table(
        "loan_chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("from_role", sa.String(32), nullable=False),
        sa.Column(
            "from_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("client_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 4) ai_modify_corrections
    op.create_table(
        "ai_modify_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "target_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loan_chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("correction", sa.Text(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 5) loan_scenarios
    op.create_table(
        "loan_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("discount_points", sa.Numeric(5, 3), nullable=False, server_default="0"),
        sa.Column("loan_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("base_rate", sa.Numeric(7, 6), nullable=True),
        sa.Column("annual_taxes", sa.Numeric(12, 2), nullable=True),
        sa.Column("annual_insurance", sa.Numeric(12, 2), nullable=True),
        sa.Column("monthly_hoa", sa.Numeric(10, 2), nullable=True),
        sa.Column("ltv", sa.Numeric(6, 4), nullable=True),
        sa.Column("recalc_snapshot", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 6) ai_feedback (polymorphic by output_type/output_id)
    op.create_table(
        "ai_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("output_type", sa.String(32), nullable=False, index=True),
        sa.Column("output_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("output_type", "output_id", "created_by", name="uq_ai_feedback_actor"),
    )


def downgrade() -> None:
    op.drop_table("ai_feedback")
    op.drop_table("loan_scenarios")
    op.drop_table("ai_modify_corrections")
    op.drop_table("loan_chat_messages")
    op.drop_table("loan_instructions")
    op.drop_column("loans", "ai_paused_until")
