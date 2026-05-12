"""Loan funding-file fields — source_deal_id + baseline + handoff (Phase 4).

Revision ID: 0048
Revises: 0047
Create Date: 2026-05-12

Adds the columns that turn a Loan row into the canonical FundingFile
representation:

- source_deal_id           FK deals.id (SET NULL). Set by
                           promote_deal_to_loan() when the agent fires
                           Ready-for-Lending.
- baseline_profile_snapshot JSONB. Frozen at handoff so the audit
                           trail survives later edits to the Deal or
                           ClientAIPlan.
- handoff_summary           Text. AI-generated narrative captured at
                           promote time. Distinct from status_summary
                           which the live summarizer keeps rewriting.
- funding_file_kind         String. bridge | dscr_purchase | dscr_refi
                           | fix_flip | construction | other. Derived
                           initially from Loan.type+purpose but human
                           edit may diverge.

Existing rows get NULL across all four; back-fill happens lazily as
agents promote deals in the new flow. Phase 4's service code
(`services/handoff.promote_deal_to_loan`) is the only writer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loans",
        sa.Column(
            "source_deal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_loans_source_deal_id", "loans", ["source_deal_id"])
    op.add_column(
        "loans",
        sa.Column(
            "baseline_profile_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("loans", sa.Column("handoff_summary", sa.Text(), nullable=True))
    op.add_column("loans", sa.Column("funding_file_kind", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("loans", "funding_file_kind")
    op.drop_column("loans", "handoff_summary")
    op.drop_column("loans", "baseline_profile_snapshot")
    op.drop_index("ix_loans_source_deal_id", table_name="loans")
    op.drop_column("loans", "source_deal_id")
