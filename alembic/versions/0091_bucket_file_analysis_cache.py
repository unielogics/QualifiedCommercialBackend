"""Add centralized per-file AI analysis cache.

Introduces bucket_file_analyses — one durable, reusable per-file AI analysis row
per (file, content_hash, analysis_version) — so a file's bytes are sent to the
model at most once per version and every surface (buckets, leads, admin, chat,
whole-bucket review) reads the same analysis instead of re-spending tokens.
Also adds bucket_files.content_hash as the current-content fingerprint.

Revision ID: 0091_bucket_file_analysis_cache
Revises: 0090_normalize_dealer_variant
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0091_bucket_file_analysis_cache"
down_revision = "0090_normalize_dealer_variant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bucket_files", sa.Column("content_hash", sa.String(length=64), nullable=True))

    op.create_table(
        "bucket_file_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("skip_reason", sa.String(length=48), nullable=True),
        sa.Column("skip_detail", sa.Text(), nullable=True),
        sa.Column("classification", sa.String(length=48), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bucket_file_id"], ["bucket_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bucket_id"], ["buckets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket_file_id", "content_hash", "analysis_version", name="uq_file_analysis_hash_version"),
    )
    op.create_index("ix_bucket_file_analyses_bucket_file_id", "bucket_file_analyses", ["bucket_file_id"])
    op.create_index("ix_bucket_file_analyses_bucket_id", "bucket_file_analyses", ["bucket_id"])
    op.create_index(
        "ix_bucket_file_analyses_file_ver_status",
        "bucket_file_analyses",
        ["bucket_file_id", "analysis_version", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_bucket_file_analyses_file_ver_status", table_name="bucket_file_analyses")
    op.drop_index("ix_bucket_file_analyses_bucket_id", table_name="bucket_file_analyses")
    op.drop_index("ix_bucket_file_analyses_bucket_file_id", table_name="bucket_file_analyses")
    op.drop_table("bucket_file_analyses")
    op.drop_column("bucket_files", "content_hash")
