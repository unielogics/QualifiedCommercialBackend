"""Agent reassignment audit trail.

Adds agent_reassignment_audits — the append-only history of every
Client.current_agent_id change, written by PATCH /clients/{id}/agent.
originating_agent_id (who first captured the lead) is never touched by
reassignment; this table records the from/to on current_agent_id.

Revision ID: 0097_agent_reassignment_audit
Revises: 0096_bucket_document_signatures
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0097_agent_reassignment_audit"
down_revision = "0096_bucket_document_signatures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_reassignment_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("performed_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_agent_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["to_agent_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["performed_by_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_agent_reassignment_audits_client_id",
        "agent_reassignment_audits",
        ["client_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_reassignment_audits_client_id", table_name="agent_reassignment_audits")
    op.drop_table("agent_reassignment_audits")
