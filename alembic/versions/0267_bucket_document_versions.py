"""Keep immutable uploaded document versions and explicit replacement lineage."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0267_bucket_document_versions"
down_revision = "0266_attention_queue_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bucket_files", sa.Column("document_family_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("bucket_files", sa.Column("document_version_number", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("bucket_files", sa.Column("is_current_document_version", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("bucket_files", sa.Column("document_version_status", sa.String(24), nullable=False, server_default="current"))
    op.add_column("bucket_files", sa.Column("supersedes_file_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("bucket_files", sa.Column("duplicate_of_file_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("bucket_files", sa.Column("upload_idempotency_key", postgresql.UUID(as_uuid=True), nullable=True))
    # Every available original is a truthful baseline. Matching names/types do
    # not prove that documents describe the same owner, account, period or loan.
    op.execute("UPDATE bucket_files SET document_family_id=id")
    op.alter_column("bucket_files", "document_family_id", nullable=False, server_default=sa.text("gen_random_uuid()"))
    op.create_unique_constraint("uq_bucket_document_version", "bucket_files", ["document_family_id", "document_version_number"])
    op.create_index("ix_bucket_document_family", "bucket_files", ["document_family_id"])
    op.create_index("uq_bucket_upload_retry", "bucket_files", ["bucket_id", "upload_idempotency_key"], unique=True, postgresql_where=sa.text("upload_idempotency_key IS NOT NULL"))
    op.execute("""CREATE FUNCTION protect_bucket_document_version() RETURNS trigger AS $$
        BEGIN
          IF NEW.document_family_id IS DISTINCT FROM OLD.document_family_id
             OR NEW.document_version_number IS DISTINCT FROM OLD.document_version_number
             OR NEW.supersedes_file_id IS DISTINCT FROM OLD.supersedes_file_id THEN
            RAISE EXCEPTION 'Document version identity is immutable; upload a new version';
          END IF;
          IF OLD.status <> 'uploading' AND (
             NEW.s3_key IS DISTINCT FROM OLD.s3_key OR
             (OLD.s3_version_id IS NOT NULL AND NEW.s3_version_id IS DISTINCT FROM OLD.s3_version_id)) THEN
            RAISE EXCEPTION 'Stored document version bytes are immutable; upload a new version';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute("CREATE TRIGGER trg_bucket_document_version_immutable BEFORE UPDATE ON bucket_files FOR EACH ROW EXECUTE FUNCTION protect_bucket_document_version()")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM bucket_files WHERE document_version_number>1)
        THEN RAISE EXCEPTION 'Document version history exists; retain it before downgrading'; END IF; END; $$""")
    op.execute("DROP TRIGGER trg_bucket_document_version_immutable ON bucket_files")
    op.execute("DROP FUNCTION protect_bucket_document_version()")
    op.drop_index("uq_bucket_upload_retry", table_name="bucket_files")
    op.drop_index("ix_bucket_document_family", table_name="bucket_files")
    op.drop_constraint("uq_bucket_document_version", "bucket_files", type_="unique")
    for column in ("upload_idempotency_key", "duplicate_of_file_id", "supersedes_file_id", "document_version_status", "is_current_document_version", "document_version_number", "document_family_id"):
        op.drop_column("bucket_files", column)
