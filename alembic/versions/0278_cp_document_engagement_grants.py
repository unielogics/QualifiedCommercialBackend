"""Staff-approved investor access to a snapshot of listing source documents.

Revision ID: 0278_cp_document_grants
Revises: 0277_manual_program_criteria
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0278_cp_document_grants"
down_revision = "0277_manual_program_criteria"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cp_listing_document_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "listing_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cp_listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cp_members.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("file_ids", postgresql.JSONB(), nullable=False),
        sa.Column("file_stamps", postgresql.JSONB(), nullable=False),
        sa.Column(
            "granted_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "revoked_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revocation_reason", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("listing_id", "member_id", name="uq_cp_document_grant_listing_member"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_cp_document_grant_status"),
    )
    op.create_index(
        "ix_cp_listing_document_grants_listing_id", "cp_listing_document_grants", ["listing_id"]
    )
    op.create_index(
        "ix_cp_listing_document_grants_member_id", "cp_listing_document_grants", ["member_id"]
    )


def downgrade():
    op.drop_table("cp_listing_document_grants")
