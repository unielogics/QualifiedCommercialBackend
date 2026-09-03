"""Production packages: the Production Arrangement filled, presented and signed
inside a car-industry AI intake file; SMS consent grants may belong to an
application profile.

Revision ID: 0181_production_packages
Revises: 0180_per_file_plaid_products
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0181_production_packages"
down_revision = "0180_per_file_plaid_products"
branch_labels = None
depends_on = None


def _uuid() -> postgresql.UUID:
    return postgresql.UUID(as_uuid=True)


def _user_ref(name: str) -> sa.Column:
    return sa.Column(name, _uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"))


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.create_table(
        "production_packages",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "profile_id", _uuid(),
            sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("intake_id", _uuid()),
        sa.Column("dealer_id", _uuid()),
        sa.Column("stage", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("arrangement", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("prefill_provenance", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attention", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("computed_cache", postgresql.JSONB()),
        sa.Column(
            "sponsor_company_id", _uuid(),
            sa.ForeignKey("referral_partner_companies.id", ondelete="SET NULL"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("presentation_s3_key", sa.String(512)),
        sa.Column("presentation_sha256", sa.String(64)),
        sa.Column("presentation_generated_at", sa.DateTime(timezone=True)),
        sa.Column("presentation_snapshot_sha256", sa.String(64)),
        sa.Column("frozen_revision_id", _uuid()),  # FK added after the revisions table exists
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        _user_ref("sent_by_user_id"),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        _user_ref("executed_by_user_id"),
        sa.Column("executed_pdf_s3_key", sa.String(512)),
        sa.Column("executed_pdf_sha256", sa.String(64)),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        _user_ref("voided_by_user_id"),
        sa.Column("void_reason", sa.Text()),
        sa.Column("delivery_history", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _user_ref("created_by_user_id"),
        _user_ref("updated_by_user_id"),
        sa.Column("updated_via", sa.String(16)),
        sa.Column("updated_share_link_id", _uuid()),
        *_timestamps(),
        sa.UniqueConstraint("profile_id", name="uq_production_packages_profile"),
    )
    op.create_index("ix_production_packages_intake", "production_packages", ["intake_id"])
    op.create_index("ix_production_packages_dealer", "production_packages", ["dealer_id"])
    op.create_index("ix_production_packages_status", "production_packages", ["status"])

    op.create_table(
        "production_package_revisions",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "package_id", _uuid(),
            sa.ForeignKey("production_packages.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("stage", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="out_for_signature"),
        sa.Column("document_key", sa.String(48), nullable=False),
        sa.Column("document_title", sa.String(180), nullable=False),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("rendered_text", sa.Text()),
        sa.Column("rendered_pdf_s3_key", sa.String(512)),
        sa.Column("rendered_pdf_sha256", sa.String(64)),
        sa.Column("current_pdf_s3_key", sa.String(512)),
        sa.Column("current_pdf_sha256", sa.String(64)),
        sa.Column("funding", postgresql.JSONB()),
        _user_ref("created_by_user_id"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        _user_ref("voided_by_user_id"),
        sa.Column("void_reason", sa.Text()),
        *_timestamps(),
        sa.UniqueConstraint("package_id", "revision_no", name="uq_production_package_revisions_no"),
    )
    op.create_index("ix_production_package_revisions_package", "production_package_revisions", ["package_id"])
    op.create_foreign_key(
        "fk_production_packages_frozen_revision",
        "production_packages",
        "production_package_revisions",
        ["frozen_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "production_package_signatures",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "package_id", _uuid(),
            sa.ForeignKey("production_packages.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "revision_id", _uuid(),
            sa.ForeignKey("production_package_revisions.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("stage", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("party", sa.String(16), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.Text()),
        sa.Column("expected_signer_name", sa.String(160)),
        sa.Column("typed_name", sa.String(160)),
        sa.Column("signature_s3_key", sa.String(512)),
        sa.Column("signature_sha256", sa.String(64)),
        sa.Column("document_sha256", sa.String(64)),
        sa.Column("esign_consent_version", sa.String(32)),
        sa.Column("esign_consent_at", sa.DateTime(timezone=True)),
        sa.Column("esign_consent_ip", sa.String(64)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("viewed_at", sa.DateTime(timezone=True)),
        sa.Column("signed_at", sa.DateTime(timezone=True)),
        sa.Column("signed_pdf_s3_key", sa.String(512)),
        sa.Column("signed_pdf_sha256", sa.String(64)),
        sa.Column("certificate_sha256", sa.String(64)),
        sa.Column("signer_name", sa.String(160)),
        sa.Column("signer_title", sa.String(120)),
        sa.Column("signed_on", sa.Date()),
        sa.Column("scan_s3_key", sa.String(512)),
        sa.Column("scan_sha256", sa.String(64)),
        sa.Column("attestation_version", sa.String(32)),
        _user_ref("recorded_by_user_id"),
        sa.Column("recorded_at", sa.DateTime(timezone=True)),
        sa.Column("recorded_ip", sa.String(64)),
        sa.Column("recorded_user_agent", sa.String(400)),
        sa.Column("note", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_production_package_signatures_package", "production_package_signatures", ["package_id"])
    op.create_index("ix_production_package_signatures_revision", "production_package_signatures", ["revision_id"])
    op.create_index(
        "uq_production_package_signatures_live",
        "production_package_signatures",
        ["revision_id", "party"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'signed')"),
    )

    op.create_table(
        "production_package_share_links",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column(
            "package_id", _uuid(),
            sa.ForeignKey("production_packages.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("rep_user_id", _uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(120)),
        sa.Column("outside_book", sa.Boolean(), nullable=False, server_default=sa.false()),
        _user_ref("created_by_user_id"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        _user_ref("revoked_by_user_id"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_index("ix_production_package_share_links_package", "production_package_share_links", ["package_id"])
    op.create_index(
        "uq_production_package_share_links_live",
        "production_package_share_links",
        ["package_id", "rep_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # SMS consent may belong to an application profile (an AI intake client
    # with no Capital OS file). consent_for() is keyed on the number, so
    # nothing about the send-time check changes.
    op.alter_column("dos_sms_consent", "dealer_id", existing_type=_uuid(), nullable=True)
    op.add_column("dos_sms_consent", sa.Column("profile_id", _uuid()))
    op.create_index("ix_dos_sms_consent_profile", "dos_sms_consent", ["profile_id"])
    op.create_check_constraint(
        "ck_dos_sms_consent_subject",
        "dos_sms_consent",
        "dealer_id IS NOT NULL OR profile_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_dos_sms_consent_subject", "dos_sms_consent", type_="check")
    op.drop_index("ix_dos_sms_consent_profile", table_name="dos_sms_consent")
    op.execute("DELETE FROM dos_sms_consent WHERE dealer_id IS NULL")
    op.drop_column("dos_sms_consent", "profile_id")
    op.alter_column("dos_sms_consent", "dealer_id", existing_type=_uuid(), nullable=False)

    op.drop_index("uq_production_package_share_links_live", table_name="production_package_share_links")
    op.drop_index("ix_production_package_share_links_package", table_name="production_package_share_links")
    op.drop_table("production_package_share_links")

    op.drop_index("uq_production_package_signatures_live", table_name="production_package_signatures")
    op.drop_index("ix_production_package_signatures_revision", table_name="production_package_signatures")
    op.drop_index("ix_production_package_signatures_package", table_name="production_package_signatures")
    op.drop_table("production_package_signatures")

    op.drop_constraint("fk_production_packages_frozen_revision", "production_packages", type_="foreignkey")
    op.drop_index("ix_production_package_revisions_package", table_name="production_package_revisions")
    op.drop_table("production_package_revisions")

    op.drop_index("ix_production_packages_status", table_name="production_packages")
    op.drop_index("ix_production_packages_dealer", table_name="production_packages")
    op.drop_index("ix_production_packages_intake", table_name="production_packages")
    op.drop_table("production_packages")
