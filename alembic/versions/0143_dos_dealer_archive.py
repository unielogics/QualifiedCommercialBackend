"""Archive tombstones for Field Desk applications.

Revision ID: 0143_dos_dealer_archive
Revises: 0142_intake_taxonomy_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0143_dos_dealer_archive"
down_revision = "0142_intake_taxonomy_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column(
        "dos_dealers",
        sa.Column(
            "archived_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("ix_dos_dealers_archived_at", "dos_dealers", ["archived_at"])
    op.add_column("dos_owners", sa.Column("credit_workflow_status", sa.String(length=32)))
    op.add_column("dos_owners", sa.Column("credit_delivery_detail", sa.String(length=240)))
    op.add_column("dos_owners", sa.Column("credit_provider_request_id", sa.String(length=120)))
    op.add_column("dos_owners", sa.Column("credit_provider_error_category", sa.String(length=48)))


def downgrade() -> None:
    op.drop_column("dos_owners", "credit_provider_error_category")
    op.drop_column("dos_owners", "credit_provider_request_id")
    op.drop_column("dos_owners", "credit_delivery_detail")
    op.drop_column("dos_owners", "credit_workflow_status")
    op.drop_index("ix_dos_dealers_archived_at", table_name="dos_dealers")
    op.drop_column("dos_dealers", "archived_by_user_id")
    op.drop_column("dos_dealers", "archived_at")
