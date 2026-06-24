"""bucket activity audit metadata.

Revision ID: 0078
Revises: 0077
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bucket_activity_logs", sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("bucket_activity_logs", sa.Column("actor_email", sa.String(length=320), nullable=True))
    op.add_column("bucket_activity_logs", sa.Column("ip_address", sa.String(length=80), nullable=True))
    op.add_column("bucket_activity_logs", sa.Column("user_agent", sa.String(length=500), nullable=True))
    op.create_foreign_key(
        "fk_bucket_activity_logs_actor_user_id_users",
        "bucket_activity_logs",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_bucket_activity_logs_actor_user_id", "bucket_activity_logs", ["actor_user_id"])
    op.create_index("ix_bucket_activity_logs_bucket_created_at", "bucket_activity_logs", ["bucket_id", "created_at"])
    op.create_index("ix_bucket_activity_logs_action", "bucket_activity_logs", ["action"])
    op.create_index("ix_bucket_activity_logs_actor_role", "bucket_activity_logs", ["actor_role"])
    op.create_index("ix_bucket_activity_logs_target_type", "bucket_activity_logs", ["target_type"])


def downgrade() -> None:
    op.drop_index("ix_bucket_activity_logs_target_type", table_name="bucket_activity_logs")
    op.drop_index("ix_bucket_activity_logs_actor_role", table_name="bucket_activity_logs")
    op.drop_index("ix_bucket_activity_logs_action", table_name="bucket_activity_logs")
    op.drop_index("ix_bucket_activity_logs_bucket_created_at", table_name="bucket_activity_logs")
    op.drop_index("ix_bucket_activity_logs_actor_user_id", table_name="bucket_activity_logs")
    op.drop_constraint("fk_bucket_activity_logs_actor_user_id_users", "bucket_activity_logs", type_="foreignkey")
    op.drop_column("bucket_activity_logs", "user_agent")
    op.drop_column("bucket_activity_logs", "ip_address")
    op.drop_column("bucket_activity_logs", "actor_email")
    op.drop_column("bucket_activity_logs", "actor_user_id")
