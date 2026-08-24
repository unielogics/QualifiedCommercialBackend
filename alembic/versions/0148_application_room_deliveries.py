"""Secure application-room delivery receipts.

Revision ID: 0148_application_room_deliveries
Revises: 0147_field_desk_crm_products
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0148_application_room_deliveries"
down_revision = "0147_field_desk_crm_products"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_room_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_profiles.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "bucket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("buckets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"),
        ),
        sa.Column("action_kind", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("recipient_email", sa.String(320)),
        sa.Column("recipient_phone", sa.String(48)),
        sa.Column("status", sa.String(24), nullable=False, server_default="created"),
        sa.Column("detail", sa.Text()),
        sa.Column("provider_result", postgresql.JSONB()),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_application_room_deliveries_profile",
        "application_room_deliveries",
        ["profile_id", "created_at"],
    )
    op.create_index(
        "ix_application_room_deliveries_bucket",
        "application_room_deliveries",
        ["bucket_id", "created_at"],
    )
    op.create_index(
        "ix_application_room_deliveries_request",
        "application_room_deliveries",
        ["requested_document_id"],
    )


def downgrade() -> None:
    op.drop_table("application_room_deliveries")
