"""Program application packages and multi-document envelopes.

Revision ID: 0160_contract_packages_envelopes
Revises: 0159_application_underwriting_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0160_contract_packages_envelopes"
down_revision = "0159_application_underwriting_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_application_profiles", sa.Column("guaranty_type", sa.String(32)))
    op.add_column("dos_application_profiles", sa.Column("office_space", sa.String(80)))
    op.add_column("dos_application_profiles", sa.Column("business_stage", sa.String(24)))
    op.add_column("dos_application_profiles", sa.Column("existing_mca_balance", sa.Numeric(14, 2)))
    op.add_column("dos_application_profiles", sa.Column("existing_sba_balance", sa.Numeric(14, 2)))
    op.add_column("dos_application_profiles", sa.Column("active_ucc_filings", sa.Integer()))
    op.add_column("dos_application_profiles", sa.Column("affiliate_businesses", sa.Boolean()))
    op.add_column("dos_application_profiles", sa.Column("send_welcome_email", sa.Boolean()))

    op.create_table(
        "dos_contract_template_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("has_acroform", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("field_names", postgresql.JSONB()),
        sa.Column("overlay_map", postgresql.JSONB()),
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["template_id"], ["dos_contract_templates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("template_id", "revision", name="uq_dos_contract_template_version"),
    )
    op.create_index(
        "ix_dos_contract_template_versions_template",
        "dos_contract_template_versions",
        ["template_id"],
    )

    op.create_table(
        "dos_contract_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("program_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("program_key", "version", name="uq_dos_contract_package_program_version"),
    )
    op.create_index(
        "ix_dos_contract_packages_program",
        "dos_contract_packages",
        ["program_key", "active"],
    )

    op.create_table(
        "dos_contract_package_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_key", sa.String(48), nullable=False),
        sa.Column("template_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("title_snapshot", sa.String(180), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("conditions", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["package_id"], ["dos_contract_packages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_version_id"], ["dos_contract_template_versions.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("package_id", "template_key", name="uq_dos_contract_package_item"),
    )
    op.create_index(
        "ix_dos_contract_package_items_package",
        "dos_contract_package_items",
        ["package_id", "sort_order"],
    )

    op.create_table(
        "dos_contract_envelopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_key", sa.String(80), nullable=False),
        sa.Column("package_version", sa.Integer(), nullable=False),
        sa.Column("program_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("source_sha256", sa.String(64)),
        sa.Column("recipient_owner_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("voided_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("void_reason", sa.Text()),
        sa.Column("signer_name", sa.String(160)),
        sa.Column("signer_title", sa.String(120)),
        sa.Column("signature_sha256", sa.String(64)),
        sa.Column("signer_ip", sa.String(64)),
        sa.Column("signer_user_agent", sa.String(400)),
        sa.Column("bundle_s3_key", sa.String(512)),
        sa.Column("bundle_sha256", sa.String(64)),
        sa.Column("delivery_history", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["dealer_id"], ["dos_dealers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["package_id"], ["dos_contract_packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_owner_id"], ["dos_owners.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voided_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_dos_contract_envelopes_dealer",
        "dos_contract_envelopes",
        ["dealer_id", "created_at"],
    )
    op.create_index("ix_dos_contract_envelopes_status", "dos_contract_envelopes", ["status"])

    op.drop_constraint("uq_dos_contract_doc", "dos_contract_documents", type_="unique")
    op.add_column(
        "dos_contract_documents",
        sa.Column("envelope_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "dos_contract_documents",
        sa.Column("template_version_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_dos_contract_documents_envelope",
        "dos_contract_documents",
        "dos_contract_envelopes",
        ["envelope_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_dos_contract_documents_template_version",
        "dos_contract_documents",
        "dos_contract_template_versions",
        ["template_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_dos_contract_doc_legacy",
        "dos_contract_documents",
        ["dealer_id", "template_key"],
        unique=True,
        postgresql_where=sa.text("envelope_id IS NULL"),
    )
    op.create_unique_constraint(
        "uq_dos_contract_doc_envelope",
        "dos_contract_documents",
        ["envelope_id", "template_key"],
    )

    op.create_table(
        "dos_contract_envelope_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("envelope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contract_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title_snapshot", sa.String(180), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["envelope_id"], ["dos_contract_envelopes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["contract_document_id"], ["dos_contract_documents.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("envelope_id", "contract_document_id", name="uq_dos_envelope_document"),
    )
    op.create_index(
        "ix_dos_envelope_documents_order",
        "dos_contract_envelope_documents",
        ["envelope_id", "sort_order"],
    )

    op.execute(
        """
        INSERT INTO dos_contract_templates
          (id, key, title, render_kind, revision, active, has_acroform,
           field_names, field_map, created_at, updated_at)
        VALUES
          (gen_random_uuid(), 'qc_program_application',
           'Business Loan Application', 'uploaded_pdf', 1, true, false,
           '[]'::jsonb, '{}'::jsonb, now(), now())
        ON CONFLICT (key) DO UPDATE
        SET title = EXCLUDED.title, active = true, render_kind = 'uploaded_pdf', updated_at = now()
        """
    )
    op.execute(
        """
        INSERT INTO dos_contract_packages
          (id, key, program_key, title, version, active, created_at, updated_at)
        VALUES
          (gen_random_uuid(), 'ez_term_application', 'term_loan_3_5_year',
           'EZ Term Application Package', 1, true, now(), now()),
          (gen_random_uuid(), 'microcap_application', 'term_loan_10_year',
           'MicroCap Application Package', 1, true, now(), now())
        ON CONFLICT (program_key, version) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO dos_contract_package_items
          (id, package_id, template_key, title_snapshot, sort_order, required,
           conditions, created_at, updated_at)
        SELECT gen_random_uuid(), p.id, 'qc_program_application',
               'Business Loan Application', 0, true, '{}'::jsonb, now(), now()
        FROM dos_contract_packages p
        WHERE p.program_key IN ('term_loan_3_5_year', 'term_loan_10_year')
          AND p.version = 1
        ON CONFLICT (package_id, template_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_dos_envelope_documents_order", table_name="dos_contract_envelope_documents")
    op.drop_table("dos_contract_envelope_documents")
    op.drop_constraint("uq_dos_contract_doc_envelope", "dos_contract_documents", type_="unique")
    op.drop_index("uq_dos_contract_doc_legacy", table_name="dos_contract_documents")
    op.drop_constraint(
        "fk_dos_contract_documents_template_version", "dos_contract_documents", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_dos_contract_documents_envelope", "dos_contract_documents", type_="foreignkey"
    )
    op.drop_column("dos_contract_documents", "template_version_id")
    op.drop_column("dos_contract_documents", "envelope_id")
    op.create_unique_constraint(
        "uq_dos_contract_doc", "dos_contract_documents", ["dealer_id", "template_key"]
    )

    op.drop_index("ix_dos_contract_envelopes_status", table_name="dos_contract_envelopes")
    op.drop_index("ix_dos_contract_envelopes_dealer", table_name="dos_contract_envelopes")
    op.drop_table("dos_contract_envelopes")
    op.drop_index("ix_dos_contract_package_items_package", table_name="dos_contract_package_items")
    op.drop_table("dos_contract_package_items")
    op.drop_index("ix_dos_contract_packages_program", table_name="dos_contract_packages")
    op.drop_table("dos_contract_packages")
    op.drop_index(
        "ix_dos_contract_template_versions_template", table_name="dos_contract_template_versions"
    )
    op.drop_table("dos_contract_template_versions")

    # The version/package tables have been removed, so the seeded legacy
    # template can now be deleted without RESTRICT/CASCADE conflicts from
    # executed envelopes or immutable package versions.
    op.execute("DELETE FROM dos_contract_templates WHERE key = 'qc_program_application'")

    for column in (
        "send_welcome_email",
        "affiliate_businesses",
        "active_ucc_filings",
        "existing_sba_balance",
        "existing_mca_balance",
        "business_stage",
        "office_space",
        "guaranty_type",
    ):
        op.drop_column("dos_application_profiles", column)
