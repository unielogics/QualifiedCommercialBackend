"""Preserve Field Desk/Dealer OS document versions without merging identities."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0269_dealer_document_versions"
down_revision = "0268_document_file_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_documents", sa.Column("document_family_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_documents", sa.Column("document_version_number", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("dos_documents", sa.Column("is_current_document_version", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("dos_documents", sa.Column("document_version_status", sa.String(24), nullable=False, server_default="current"))
    op.add_column("dos_documents", sa.Column("supersedes_document_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_documents", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("dos_documents", sa.Column("upload_idempotency_key", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_documents", sa.Column("upload_request_fingerprint", sa.String(64), nullable=True))
    op.execute("UPDATE dos_documents SET document_family_id=id")
    op.alter_column("dos_documents", "document_family_id", nullable=False, server_default=sa.text("gen_random_uuid()"))
    op.create_unique_constraint("uq_dos_document_version", "dos_documents", ["document_family_id", "document_version_number"])
    op.create_index("ix_dos_document_family", "dos_documents", ["document_family_id"])
    op.create_index("uq_dos_document_upload_retry", "dos_documents", ["dealer_id", "upload_idempotency_key"], unique=True, postgresql_where=sa.text("upload_idempotency_key IS NOT NULL"))
    op.execute("""CREATE FUNCTION protect_dealer_document_version() RETURNS trigger AS $$ BEGIN
        IF NEW.document_family_id IS DISTINCT FROM OLD.document_family_id
          OR NEW.document_version_number IS DISTINCT FROM OLD.document_version_number
          OR NEW.supersedes_document_id IS DISTINCT FROM OLD.supersedes_document_id
          OR (OLD.s3_key IS NOT NULL AND NEW.s3_key IS DISTINCT FROM OLD.s3_key) THEN
            RAISE EXCEPTION 'Document version identity and original bytes are immutable; upload a new version';
        END IF; RETURN NEW; END; $$ LANGUAGE plpgsql""")
    op.execute("CREATE TRIGGER trg_dealer_document_version_immutable BEFORE UPDATE ON dos_documents FOR EACH ROW EXECUTE FUNCTION protect_dealer_document_version()")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM dos_documents WHERE document_version_number>1)
        THEN RAISE EXCEPTION 'Dealer document version history exists; retain it before downgrading'; END IF; END; $$""")
    op.execute("DROP TRIGGER trg_dealer_document_version_immutable ON dos_documents")
    op.execute("DROP FUNCTION protect_dealer_document_version()")
    op.drop_index("uq_dos_document_upload_retry", table_name="dos_documents")
    op.drop_index("ix_dos_document_family", table_name="dos_documents")
    op.drop_constraint("uq_dos_document_version", "dos_documents", type_="unique")
    for column in ("upload_request_fingerprint", "upload_idempotency_key", "content_hash", "supersedes_document_id", "document_version_status", "is_current_document_version", "document_version_number", "document_family_id"):
        op.drop_column("dos_documents", column)
