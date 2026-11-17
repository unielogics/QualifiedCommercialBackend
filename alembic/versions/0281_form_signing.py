"""Signing passes over frozen client-form versions (the real-estate application)."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0281_form_signing"
down_revision = "0280_client_forms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "form_signing_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("form_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("client_forms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("client_form_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="in_progress"),
        sa.Column("signature_config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("required_roles", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("signers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("unsigned_sha256", sa.String(64), nullable=False),
        sa.Column("current_pdf_key", sa.String(512), nullable=True),
        sa.Column("current_sha256", sa.String(64), nullable=True),
        sa.Column("chain", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("executed_file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bucket_files.id", ondelete="SET NULL"), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.String(500), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status in ('in_progress','executed','void')", name="ck_form_signing_sessions_status"),
    )
    for column in ("profile_id", "form_id", "version_id"):
        op.create_index(f"ix_form_signing_sessions_{column}", "form_signing_sessions", [column])
    op.execute(
        """
        CREATE TRIGGER trg_qc_application_history_retention_propagate
        AFTER INSERT OR UPDATE ON form_signing_sessions
        FOR EACH ROW
        EXECUTE FUNCTION qc_propagate_application_history_retention()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_retained_history_delete
        BEFORE DELETE ON form_signing_sessions
        FOR EACH ROW
        EXECUTE FUNCTION qc_guard_retained_application_history_delete()
        """
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM form_signing_sessions)
        THEN RAISE EXCEPTION 'Signing sessions exist; preserve them before downgrading';
        END IF;
    END; $$""")
    op.execute("DROP TRIGGER IF EXISTS trg_qc_retained_history_delete ON form_signing_sessions")
    op.execute("DROP TRIGGER IF EXISTS trg_qc_application_history_retention_propagate ON form_signing_sessions")
    op.drop_table("form_signing_sessions")
