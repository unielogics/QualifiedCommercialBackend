"""Fintech Orchestrator: loan participants, email drafts, deal health, status summary

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Living Loan File columns on loans
    op.add_column("loans", sa.Column("status_summary", sa.Text(), nullable=True))
    op.add_column(
        "loans",
        sa.Column(
            "deal_health",
            sa.String(32),
            nullable=False,
            server_default="on_track",
        ),
    )

    # 2) loan_participants — frontend-managed thread membership
    op.create_table(
        "loan_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("company", sa.String(160), nullable=True),
        sa.Column("cc_outbound", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("bcc_outbound", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("hide_identity", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("loan_id", "email", name="uq_loan_participants_loan_email"),
    )

    # 3) email_drafts — pending Broker-approval inbox
    op.create_table(
        "email_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("to_email", sa.String(320), nullable=False),
        sa.Column("cc_emails", postgresql.JSONB, nullable=True),
        sa.Column("bcc_emails", postgresql.JSONB, nullable=True),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("triggered_by_kind", sa.String(64), nullable=True),
        sa.Column("triggered_by_payload", postgresql.JSONB, nullable=True),
        sa.Column("actioned_by", sa.String(160), nullable=True),
        sa.Column("sent_message_id", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("email_drafts")
    op.drop_table("loan_participants")
    op.drop_column("loans", "deal_health")
    op.drop_column("loans", "status_summary")
