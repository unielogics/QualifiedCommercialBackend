"""AgentTask — internal agent CRM workflow tasks (Phase 7).

Revision ID: 0051
Revises: 0050
Create Date: 2026-05-12

Stores agent-side workflow items (open houses, showings, listing
prep, photography, CMA, funding prep, document collection) separate
from borrower-facing ClientRequirementStatus rows. Visibility tiers
gate what flows into the funding handoff baseline.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "deal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(32), nullable=False, server_default="other"),
        sa.Column("visibility", sa.String(24), nullable=False, server_default="team_visible"),
        sa.Column("owner_type", sa.String(16), nullable=False, server_default="human"),
        sa.Column(
            "assigned_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "ai_assignment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_task_assignments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("priority", sa.String(8), nullable=False, server_default="medium"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_tasks_client_id", "agent_tasks", ["client_id"])
    op.create_index("ix_agent_tasks_deal_id", "agent_tasks", ["deal_id"])
    op.create_index("ix_agent_tasks_loan_id", "agent_tasks", ["loan_id"])
    op.create_index("ix_agent_tasks_assigned_user_id", "agent_tasks", ["assigned_user_id"])
    op.create_index("ix_agent_tasks_client_status", "agent_tasks", ["client_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_tasks_client_status", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_assigned_user_id", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_loan_id", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_deal_id", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_client_id", table_name="agent_tasks")
    op.drop_table("agent_tasks")
