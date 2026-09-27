"""Add versioned document review traits and exclusion-first industry policy.

Revision ID: 0229_funding_document_review
Revises: 0228_pipeline_economics
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0229_funding_document_review"
down_revision = "0228_pipeline_economics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Empty defaults preserve all existing approvals and scope behavior. Suggested
    # policies remain explicit admin choices, not automatic production rule edits.
    op.add_column(
        "funding_program_scopes",
        sa.Column(
            "excluded_naics_prefixes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "ai_collection_requirements",
        sa.Column(
            "review_checks",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_collection_requirements", "review_checks")
    op.drop_column("funding_program_scopes", "excluded_naics_prefixes")
