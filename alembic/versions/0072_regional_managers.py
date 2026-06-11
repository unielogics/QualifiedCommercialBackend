"""regional managers.

Revision ID: 0072
Revises: 0071
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regional_manager_agents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("manager_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("manager_user_id", "agent_user_id", name="uq_regional_manager_agent"),
    )
    op.create_index("ix_regional_manager_agents_manager_user_id", "regional_manager_agents", ["manager_user_id"])
    op.create_index("ix_regional_manager_agents_agent_user_id", "regional_manager_agents", ["agent_user_id"])

    op.execute(
        "UPDATE users SET role = 'regional_manager' "
        "WHERE lower(email) = 'denzel@qualifiedcommercial.com' AND deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE users SET role = 'broker' "
        "WHERE lower(email) = 'denzel@qualifiedcommercial.com' AND role = 'regional_manager'"
    )
    op.drop_index("ix_regional_manager_agents_agent_user_id", table_name="regional_manager_agents")
    op.drop_index("ix_regional_manager_agents_manager_user_id", table_name="regional_manager_agents")
    op.drop_table("regional_manager_agents")
