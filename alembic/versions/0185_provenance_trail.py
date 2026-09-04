"""Where a file came from, and where every document in it came from.

Before this, the answer lived in free text and JSON that disagreed with itself:
an operator's upload was attributed to the client because the browser pre-filled
the client's name, a field rep's upload through Capital OS was recorded as
uploaded by "Capital OS", and dos_documents had no uploader column at all. These
columns give both levels a server-decided answer with a real foreign key to the
person, so the trail survives the request that made it.

Nothing is backfilled with a guess. Rows written before today keep a null
source_kind, which reads as "not recorded" rather than claiming to know.

Revision ID: 0185_provenance_trail
Revises: 0184_booking_precall_video
"""

import sqlalchemy as sa

from alembic import op

revision = "0185_provenance_trail"
down_revision = "0184_booking_precall_video"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Documents.
    op.add_column("bucket_files", sa.Column("source_kind", sa.String(24), nullable=True))
    op.add_column("bucket_files", sa.Column("source_detail", sa.String(200), nullable=True))
    op.add_column(
        "bucket_files",
        sa.Column(
            "uploaded_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_bucket_files_source_kind", "bucket_files", ["source_kind"])

    # Capital OS documents had no uploader of any kind, which is why a field
    # rep's upload became "Capital OS" by the time it reached the lead file.
    op.add_column("dos_documents", sa.Column("source_kind", sa.String(24), nullable=True))
    op.add_column("dos_documents", sa.Column("source_detail", sa.String(200), nullable=True))
    op.add_column("dos_documents", sa.Column("uploaded_by_name", sa.String(200), nullable=True))
    op.add_column(
        "dos_documents",
        sa.Column(
            "uploaded_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_dos_documents_source_kind", "dos_documents", ["source_kind"])

    # Files.
    for table in ("public_underwriting_intakes", "dos_dealers"):
        op.add_column(table, sa.Column("source_kind", sa.String(24), nullable=True))
        op.add_column(table, sa.Column("source_detail", sa.String(200), nullable=True))
        op.add_column(table, sa.Column("source_actor_name", sa.String(200), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "source_user_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_source_kind", table, ["source_kind"])


def downgrade() -> None:
    for table in ("public_underwriting_intakes", "dos_dealers"):
        op.drop_index(f"ix_{table}_source_kind", table_name=table)
        op.drop_column(table, "source_user_id")
        op.drop_column(table, "source_actor_name")
        op.drop_column(table, "source_detail")
        op.drop_column(table, "source_kind")
    op.drop_index("ix_dos_documents_source_kind", table_name="dos_documents")
    op.drop_column("dos_documents", "uploaded_by_user_id")
    op.drop_column("dos_documents", "uploaded_by_name")
    op.drop_column("dos_documents", "source_detail")
    op.drop_column("dos_documents", "source_kind")
    op.drop_index("ix_bucket_files_source_kind", table_name="bucket_files")
    op.drop_column("bucket_files", "uploaded_by_user_id")
    op.drop_column("bucket_files", "source_detail")
    op.drop_column("bucket_files", "source_kind")
