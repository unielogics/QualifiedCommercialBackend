"""Product booklet presentation artifacts and application pre-screen.

Revision ID: 0149_product_booklet_prescreen
Revises: 0148_application_room_deliveries
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0149_product_booklet_prescreen"
down_revision = "0148_application_room_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_product_presentations",
        sa.Column("catalog_versions", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("dos_product_presentations", sa.Column("pdf_sha256", sa.String(64)))

    op.create_table(
        "dos_application_pre_screens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rules_version", sa.String(32), nullable=False, server_default="quidity_step1_v1"),
        sa.Column("file_answers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("owner_answers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("routing_result", postgresql.JSONB()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "completed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dealer_id", name="uq_dos_application_pre_screen_dealer"),
    )
    op.create_index(
        "ix_dos_application_pre_screen_updated",
        "dos_application_pre_screens",
        ["updated_at"],
    )

    op.create_table(
        "dos_product_presentation_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "presentation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dos_product_presentations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("s3_key", sa.String(720), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_downloaded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("presentation_id", name="uq_dos_product_presentation_artifact"),
    )
    op.create_index(
        "ix_dos_product_presentation_token",
        "dos_product_presentation_artifacts",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_dos_product_presentation_token", table_name="dos_product_presentation_artifacts")
    op.drop_table("dos_product_presentation_artifacts")
    op.drop_index("ix_dos_application_pre_screen_updated", table_name="dos_application_pre_screens")
    op.drop_table("dos_application_pre_screens")
    op.drop_column("dos_product_presentations", "pdf_sha256")
    op.drop_column("dos_product_presentations", "catalog_versions")
