"""operator file lineage and durable bucket-intake links

Revision ID: 0138_operator_file_lineage
Revises: 0137_dos_bank_consent
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision = "0138_operator_file_lineage"
down_revision = "0137_dos_bank_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_underwriting_intakes",
        sa.Column("promoted_loan_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_public_underwriting_intakes_promoted_loan_id_loans",
        "public_underwriting_intakes",
        "loans",
        ["promoted_loan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_public_underwriting_intakes_promoted_loan_id",
        "public_underwriting_intakes",
        ["promoted_loan_id"],
        unique=True,
    )

    op.add_column(
        "loans",
        sa.Column("source_intake_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_loans_source_intake_id_public_underwriting_intakes",
        "loans",
        "public_underwriting_intakes",
        ["source_intake_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_loans_source_intake_id", "loans", ["source_intake_id"], unique=True)

    op.create_table(
        "bucket_intake_links",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bucket_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("buckets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "intake_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relationship", sa.String(24), nullable=False, server_default="supporting"),
        sa.Column("note", sa.Text()),
        sa.Column(
            "linked_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "unlinked_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("unlinked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("bucket_id", "intake_id", name="uq_bucket_intake_links_pair"),
    )
    op.create_index(
        "ix_bucket_intake_links_bucket_active",
        "bucket_intake_links",
        ["bucket_id", "unlinked_at"],
    )
    op.create_index(
        "ix_bucket_intake_links_intake_active",
        "bucket_intake_links",
        ["intake_id", "unlinked_at"],
    )

    op.create_table(
        "bucket_intake_link_files",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "link_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("bucket_intake_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bucket_file_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("bucket_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "selected_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "removed_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("link_id", "bucket_file_id", name="uq_bucket_intake_link_files_pair"),
    )
    op.create_index(
        "ix_bucket_intake_link_files_active",
        "bucket_intake_link_files",
        ["link_id", "removed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_bucket_intake_link_files_active", table_name="bucket_intake_link_files")
    op.drop_table("bucket_intake_link_files")
    op.drop_index("ix_bucket_intake_links_intake_active", table_name="bucket_intake_links")
    op.drop_index("ix_bucket_intake_links_bucket_active", table_name="bucket_intake_links")
    op.drop_table("bucket_intake_links")
    op.drop_index("ix_loans_source_intake_id", table_name="loans")
    op.drop_constraint(
        "fk_loans_source_intake_id_public_underwriting_intakes", "loans", type_="foreignkey"
    )
    op.drop_column("loans", "source_intake_id")
    op.drop_index(
        "ix_public_underwriting_intakes_promoted_loan_id",
        table_name="public_underwriting_intakes",
    )
    op.drop_constraint(
        "fk_public_underwriting_intakes_promoted_loan_id_loans",
        "public_underwriting_intakes",
        type_="foreignkey",
    )
    op.drop_column("public_underwriting_intakes", "promoted_loan_id")
