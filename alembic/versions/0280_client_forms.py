"""Client-filled forms (the real-estate loan application) and their frozen versions.

Adds ``client_forms`` and ``client_form_versions``, protects both with the
application-history retention triggers from 0239, and lets a form link carry
the ``re_application`` kind.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0280_client_forms"
down_revision = "0279_chatgpt_product_feed"
branch_labels = None
depends_on = None

LINK_KINDS = "'pfs','debt_schedule','p_and_l','balance_sheet','real_estate_schedule','operating_plan','worksheet'"
TABLES = ("client_forms", "client_form_versions")


def upgrade() -> None:
    op.create_table(
        "client_forms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("answers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("touched", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("profile_id", "kind", name="uq_client_forms_profile_kind"),
        sa.CheckConstraint("kind in ('re_application')", name="ck_client_forms_kind"),
        sa.CheckConstraint("status in ('draft','submitted')", name="ck_client_forms_status"),
    )
    op.create_index("ix_client_forms_profile_id", "client_forms", ["profile_id"])
    op.create_table(
        "client_form_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("form_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("client_forms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("provenance", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("pdf_s3_key", sa.String(512), nullable=True),
        sa.Column("pdf_sha256", sa.String(64), nullable=True),
        sa.Column("submitted_by", sa.String(16), nullable=False),
        sa.Column("submitted_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("idempotency_key", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("form_id", "number", name="uq_client_form_versions_number"),
        sa.UniqueConstraint("form_id", "idempotency_key", name="uq_client_form_versions_idempotency"),
        sa.CheckConstraint("submitted_by in ('client','desk')", name="ck_client_form_versions_submitted_by"),
    )
    op.create_index("ix_client_form_versions_form_id", "client_form_versions", ["form_id"])
    op.create_index("ix_client_form_versions_profile_id", "client_form_versions", ["profile_id"])
    op.create_foreign_key(
        "fk_client_forms_current_version", "client_forms", "client_form_versions",
        ["current_version_id"], ["id"], ondelete="SET NULL",
    )
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_application_history_retention_propagate
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_propagate_application_history_retention()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_qc_retained_history_delete
            BEFORE DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION qc_guard_retained_application_history_delete()
            """
        )
    op.drop_constraint("ck_financial_form_links_kind", "financial_form_links", type_="check")
    op.create_check_constraint("ck_financial_form_links_kind", "financial_form_links", f"kind in ({LINK_KINDS},'re_application')")


def downgrade() -> None:
    # Refuse to erase a client's application or strand a link that opens one.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM client_forms) OR EXISTS (SELECT 1 FROM financial_form_links WHERE kind='re_application')
        THEN RAISE EXCEPTION 'Client forms or their links exist; preserve them before downgrading';
        END IF;
    END; $$""")
    op.drop_constraint("ck_financial_form_links_kind", "financial_form_links", type_="check")
    op.create_check_constraint("ck_financial_form_links_kind", "financial_form_links", f"kind in ({LINK_KINDS})")
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_qc_retained_history_delete ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_qc_application_history_retention_propagate ON {table}")
    op.drop_constraint("fk_client_forms_current_version", "client_forms", type_="foreignkey")
    op.drop_table("client_form_versions")
    op.drop_table("client_forms")
