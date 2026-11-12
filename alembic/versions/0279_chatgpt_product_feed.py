"""Managed ChatGPT product feed and durable upload history.

Revision ID: 0279_chatgpt_product_feed
Revises: 0278_cp_document_grants
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0279_chatgpt_product_feed"
down_revision = "0278_cp_document_grants"
branch_labels = None
depends_on = None


def upgrade():
    config = op.create_table(
        "chatgpt_feed_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("public_key", sa.Text()),
        sa.Column("connection_signature", sa.String(64)),
        sa.Column("delivery_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_hash", sa.String(64)),
        sa.Column("last_source_hash", sa.String(64)),
        sa.Column("last_exported_rows", postgresql.JSONB(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1", name="ck_chatgpt_feed_singleton"),
    )
    op.bulk_insert(config, [{"id": 1, "revision": 0, "data": {}, "last_exported_rows": []}])
    op.create_table(
        "chatgpt_feed_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("catalog_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("trigger", sa.String(24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("source_hash", sa.String(64)),
        sa.Column("last_error", sa.Text()),
        sa.Column("processing_status", sa.String(24), nullable=False),
        sa.CheckConstraint("catalog_id = 1", name="ck_chatgpt_feed_run_catalog"),
    )
    op.create_index("ix_chatgpt_feed_runs_status", "chatgpt_feed_runs", ["status"])
    op.create_index("uq_chatgpt_feed_one_active", "chatgpt_feed_runs", ["catalog_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued','running')"))


def downgrade():
    op.drop_index("uq_chatgpt_feed_one_active", table_name="chatgpt_feed_runs")
    op.drop_index("ix_chatgpt_feed_runs_status", table_name="chatgpt_feed_runs")
    op.drop_table("chatgpt_feed_runs")
    op.drop_table("chatgpt_feed_config")
