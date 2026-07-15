"""Add live progress to bucket AI reviews.

Adds bucket_ai_reviews.progress (JSONB) so the run-review UI can poll honest,
real-time state — {stage, label, percent, files_total, files_done} — updated and
committed by the background runner at each step.

Revision ID: 0092_bucket_ai_review_progress
Revises: 0091_bucket_file_analysis_cache
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0092_bucket_ai_review_progress"
down_revision = "0091_bucket_file_analysis_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bucket_ai_reviews", sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("bucket_ai_reviews", "progress")
