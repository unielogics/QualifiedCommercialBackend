"""Separate financial working drafts from immutable dated PDF versions.

Revision ID: 0242_financial_workspace
Revises: 0241_production_legal_retention
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0242_financial_workspace"
down_revision = "0241_production_legal_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    json = postgresql.JSONB()
    op.create_table("financial_form_families",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("profile_id", uuid, sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("identity_key", sa.String(200), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("identity", json, nullable=False),
        sa.Column("current_version_id", uuid),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("profile_id", "kind", "identity_key", name="uq_financial_form_family_identity"),
        sa.CheckConstraint("kind in ('pfs','debt_schedule','p_and_l','balance_sheet')", name="ck_financial_form_family_kind"))
    op.create_index("ix_financial_form_family_profile_kind", "financial_form_families", ["profile_id", "kind"])
    op.create_table("financial_form_versions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("family_id", uuid, sa.ForeignKey("financial_form_families.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("body", json, nullable=False),
        sa.Column("key_facts", json, nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("pdf_sha256", sa.String(64)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("parent_version_id", uuid, sa.ForeignKey("financial_form_versions.id", ondelete="SET NULL")),
        sa.Column("source_file_id", uuid, sa.ForeignKey("bucket_files.id", ondelete="SET NULL")),
        sa.Column("pdf_file_id", uuid, sa.ForeignKey("bucket_files.id", ondelete="SET NULL")),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("actor_user_id", uuid, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_name", sa.String(180)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("family_id", "version_number", name="uq_financial_form_version_number"),
        sa.UniqueConstraint("family_id", "idempotency_key", name="uq_financial_form_version_request"))
    op.create_index("ix_financial_form_version_source", "financial_form_versions", ["source_file_id"])
    op.create_index("ix_financial_form_version_pdf", "financial_form_versions", ["pdf_file_id"])
    op.create_foreign_key("fk_financial_family_current_version", "financial_form_families", "financial_form_versions", ["current_version_id"], ["id"], ondelete="SET NULL")
    op.create_table("financial_form_drafts",
        sa.Column("family_id", uuid, sa.ForeignKey("financial_form_families.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("body", json, nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("base_version_id", uuid, sa.ForeignKey("financial_form_versions.id", ondelete="SET NULL", name="fk_financial_draft_base_version")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", uuid, sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.create_table("financial_form_imports",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("family_id", uuid, sa.ForeignKey("financial_form_families.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_file_id", uuid, sa.ForeignKey("bucket_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("proposed_body", json, nullable=False),
        sa.Column("mappings", json, nullable=False),
        sa.Column("warnings", json, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", uuid, sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.create_index("ix_financial_form_imports_family_id", "financial_form_imports", ["family_id"])


def downgrade() -> None:
    op.drop_table("financial_form_imports")
    op.drop_table("financial_form_drafts")
    op.drop_constraint("fk_financial_family_current_version", "financial_form_families", type_="foreignkey")
    op.drop_table("financial_form_versions")
    op.drop_table("financial_form_families")
