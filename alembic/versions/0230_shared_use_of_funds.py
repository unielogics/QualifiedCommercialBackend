"""Shared use-of-funds budget, without changing any published lending policy.

Revision ID: 0230_shared_use_of_funds
Revises: 0229_funding_document_review
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0230_shared_use_of_funds"
down_revision = "0229_funding_document_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("application_profiles", sa.Column(
        "use_of_funds", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb"),
    ))
    op.add_column("application_profiles", sa.Column(
        "use_of_funds_revision", sa.Integer(), nullable=False, server_default="0",
    ))
    op.add_column("application_profiles", sa.Column("use_of_funds_updated_at", sa.DateTime(timezone=True)))
    op.add_column("application_profiles", sa.Column(
        "use_of_funds_updated_by_user_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    ))


def downgrade() -> None:
    for name in ("use_of_funds_updated_by_user_id", "use_of_funds_updated_at", "use_of_funds_revision", "use_of_funds"):
        op.drop_column("application_profiles", name)
