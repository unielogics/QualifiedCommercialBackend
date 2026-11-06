"""Keep immutable versions of existing loan-vault documents."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0268_document_file_versions"
down_revision = "0267_bucket_document_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("file_version_number", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("documents", sa.Column("latest_file_version_number", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("documents", sa.Column("latest_file_version_status", sa.String(24), nullable=False, server_default="current"))
    op.add_column("documents", sa.Column("current_file_version_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_table("document_file_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False), sa.Column("s3_key", sa.String(700), nullable=False),
        sa.Column("s3_version_id", sa.String(256)),
        sa.Column("content_type", sa.String(160), nullable=False), sa.Column("content_hash", sa.String(64)),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("analysis_status", sa.String(24), nullable=False),
        sa.Column("analysis_snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True)),
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("uploaded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_file_version_number"),
        sa.UniqueConstraint("document_id", "idempotency_key", name="uq_document_file_version_retry"))
    op.create_index("ix_document_file_versions_document_id", "document_file_versions", ["document_id"])
    op.add_column("document_analysis_results", sa.Column("document_version_id", postgresql.UUID(as_uuid=True)))
    op.add_column("document_analysis_results", sa.Column("is_current", sa.Boolean(), nullable=False, server_default="true"))
    op.create_index("ix_document_analysis_results_document_version_id", "document_analysis_results", ["document_version_id"])
    # No upload date or actor is invented for an older file. created_at is the
    # known record timestamp; source labels it as a truthful legacy baseline.
    op.execute("""INSERT INTO document_file_versions
        (id,document_id,version_number,file_name,s3_key,content_type,status,analysis_status,analysis_snapshot,source,created_at)
        SELECT md5(id::text || chr(58) || 'file-version' || chr(58) || '1')::uuid,id,1,name,s3_key,'application/octet-stream',
          CASE WHEN status IN ('received','verified','flagged') THEN 'uploaded' ELSE 'uploading' END,
          CASE WHEN status='verified' AND verified_by IS DISTINCT FROM 'ai' THEN 'reviewed' ELSE ai_scan_status END,
          jsonb_build_object('ai_notes',ai_notes,'status',status,'ai_scan_status',ai_scan_status),
          'legacy_baseline',coalesce(updated_at,created_at) FROM documents WHERE s3_key IS NOT NULL""")
    op.execute("""UPDATE documents SET current_file_version_id=md5(id::text || chr(58) || 'file-version' || chr(58) || '1')::uuid,
        file_version_number=1,latest_file_version_number=1 WHERE s3_key IS NOT NULL""")
    op.execute("UPDATE document_analysis_results SET is_current=false")
    op.execute("""UPDATE document_analysis_results a SET document_version_id=d.current_file_version_id,is_current=true
        FROM documents d WHERE a.document_id=d.id AND d.ai_scan_status='scanned'
        AND a.id IN (SELECT DISTINCT ON (document_id) id FROM document_analysis_results
                     ORDER BY document_id,analyzed_at DESC,id DESC)""")
    op.execute("""CREATE FUNCTION protect_document_version_binary() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF (NEW.document_id,NEW.version_number,NEW.file_name,NEW.content_type,NEW.source)
         IS DISTINCT FROM (OLD.document_id,OLD.version_number,OLD.file_name,OLD.content_type,OLD.source)
         OR (OLD.status <> 'uploading' AND (NEW.s3_key,NEW.s3_version_id) IS DISTINCT FROM (OLD.s3_key,OLD.s3_version_id))
         OR (OLD.content_hash IS NOT NULL AND NEW.content_hash IS DISTINCT FROM OLD.content_hash) THEN
        RAISE EXCEPTION 'Document version binaries are immutable'; END IF; RETURN NEW; END; $$""")
    op.execute("""CREATE TRIGGER document_version_binary_immutable BEFORE UPDATE ON document_file_versions
        FOR EACH ROW EXECUTE FUNCTION protect_document_version_binary()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_file_versions) THEN
        RAISE EXCEPTION 'Document version history exists; preserve it before downgrade'; END IF; END; $$""")
    op.execute("DROP TRIGGER document_version_binary_immutable ON document_file_versions")
    op.execute("DROP FUNCTION protect_document_version_binary()")
    op.drop_index("ix_document_analysis_results_document_version_id", table_name="document_analysis_results")
    op.drop_column("document_analysis_results", "is_current")
    op.drop_column("document_analysis_results", "document_version_id")
    op.drop_table("document_file_versions")
    op.drop_column("documents", "current_file_version_id")
    op.drop_column("documents", "latest_file_version_number")
    op.drop_column("documents", "latest_file_version_status")
    op.drop_column("documents", "file_version_number")
