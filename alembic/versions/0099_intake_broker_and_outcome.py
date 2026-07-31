"""Dealer-partner broker_id + loan outcome status on public_underwriting_intakes.

Adds:
  - broker_id: nullable FK -> users.id (ON DELETE SET NULL), indexed. Mirrors
    PublicUnderwritingIntakeArtifact.created_by_user_id's pattern. Set only on
    leads created via the new /broker/ai-underwriter-leads endpoints; existing
    rows (public-site and admin-created leads) stay NULL, meaning
    house/admin-attributed.
  - outcome_status: the firm's loan decision (submitted/closed/denied) — a
    separate concept from the existing `status` column, which tracks the AI
    screening lifecycle (collecting/reviewed), not the loan outcome.

Purely additive; no existing columns are touched. No backfill needed beyond
the outcome_status server_default.

Revision ID: 0099_intake_broker_and_outcome
Revises: 0098_bucket_public_share
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0099_intake_broker_and_outcome"
down_revision = "0098_bucket_public_share"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_underwriting_intakes",
        sa.Column(
            "broker_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_public_underwriting_intakes_broker_id",
        "public_underwriting_intakes",
        ["broker_id"],
    )
    op.add_column(
        "public_underwriting_intakes",
        sa.Column(
            "outcome_status",
            sa.String(16),
            nullable=False,
            server_default="submitted",
        ),
    )


def downgrade() -> None:
    op.drop_column("public_underwriting_intakes", "outcome_status")
    op.drop_index("ix_public_underwriting_intakes_broker_id", table_name="public_underwriting_intakes")
    op.drop_column("public_underwriting_intakes", "broker_id")
