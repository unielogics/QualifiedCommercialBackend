"""Production package stage two: term sheets, the final (child) package pinned to
the executed stage-one revision, signatures on file, typed initials.

Revision ID: 0182_production_package_final
Revises: 0181_production_packages
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0182_production_package_final"
down_revision = "0181_production_packages"
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
    # ---- signatures on file ----
    op.create_table(
        "stored_signatures",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", _uuid()),
        sa.Column("typed_name", sa.String(160), nullable=False),
        sa.Column("title", sa.String(120)),
        sa.Column("signature_s3_key", sa.String(512), nullable=False),
        sa.Column("signature_sha256", sa.String(64), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("source_agreement_id", _uuid(), sa.ForeignKey("contract_agreements.id", ondelete="SET NULL")),
        sa.Column("adoption_consent_version", sa.String(32)),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        _user_ref("adopted_by_user_id"),
        sa.Column("adopted_ip", sa.String(64)),
        sa.Column("adopted_user_agent", sa.String(400)),
        sa.Column("authorization_note", sa.Text()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        _user_ref("revoked_by_user_id"),
        *_timestamps(),
    )
    op.create_index(
        "uq_stored_signatures_live", "stored_signatures", ["subject_type", "subject_id"],
        unique=True, postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ---- term sheets ----
    op.create_table(
        "production_term_sheets",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("profile_id", _uuid(), sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="current"),
        sa.Column("funding_party_kind", sa.String(24), nullable=False),
        sa.Column("lender_id", _uuid(), sa.ForeignKey("lenders.id", ondelete="SET NULL")),
        sa.Column("funding_party_name", sa.String(180), nullable=False),
        sa.Column("facility_type", sa.String(48), nullable=False),
        sa.Column("approved_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("min_activation_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("rate_pct", sa.Numeric(7, 3), nullable=False),
        sa.Column("term_months", sa.SmallInteger(), nullable=False),
        sa.Column("monthly_debt_service", sa.Numeric(14, 2), nullable=False),
        sa.Column("debt_service_is_level_payment", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expected_funding_date", sa.Date()),
        sa.Column("activation_date", sa.Date()),
        sa.Column("commencement_date", sa.Date()),
        sa.Column("maturity_date", sa.Date()),
        sa.Column("use_of_funds", postgresql.JSONB()),
        sa.Column("conditions", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("extra", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("supersedes_id", _uuid(), sa.ForeignKey("production_term_sheets.id", ondelete="RESTRICT")),
        _user_ref("entered_by_user_id"),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("entered_ip", sa.String(64)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        _user_ref("superseded_by_user_id"),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        _user_ref("withdrawn_by_user_id"),
        sa.Column("withdraw_reason", sa.Text()),
        sa.Column("consumed_by_package_id", _uuid()),  # FK added below (circular with production_packages)
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("profile_id", "version", name="uq_production_term_sheets_version"),
        sa.CheckConstraint(
            "approved_amount > 0 AND min_activation_amount > 0 AND min_activation_amount <= approved_amount",
            name="ck_production_term_sheets_amounts",
        ),
        sa.CheckConstraint("rate_pct >= 0 AND term_months > 0 AND monthly_debt_service > 0", name="ck_production_term_sheets_pricing"),
    )
    op.create_index("ix_production_term_sheets_profile", "production_term_sheets", ["profile_id"])
    op.create_index(
        "uq_production_term_sheets_current", "production_term_sheets", ["profile_id"],
        unique=True, postgresql_where=sa.text("status = 'current'"),
    )
    op.create_foreign_key(
        "fk_production_term_sheets_consumed_by", "production_term_sheets", "production_packages",
        ["consumed_by_package_id"], ["id"], ondelete="SET NULL",
    )

    # ---- the final (child) package ----
    op.drop_constraint("uq_production_packages_profile", "production_packages", type_="unique")
    op.add_column("production_packages", sa.Column("parent_package_id", _uuid(), sa.ForeignKey("production_packages.id", ondelete="RESTRICT")))
    op.add_column("production_packages", sa.Column("source_revision_id", _uuid()))
    op.add_column("production_packages", sa.Column("term_sheet_id", _uuid()))
    op.add_column("production_packages", sa.Column("sent_via", sa.String(16)))
    op.add_column("production_packages", sa.Column("sent_share_link_id", _uuid()))
    op.add_column("production_packages", sa.Column("execution_pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_foreign_key(
        "fk_production_packages_source_revision", "production_packages", "production_package_revisions",
        ["source_revision_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_production_packages_term_sheet", "production_packages", "production_term_sheets",
        ["term_sheet_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_production_packages_parent", "production_packages", ["parent_package_id"])
    op.create_index(
        "uq_production_packages_profile_stage_live", "production_packages", ["profile_id", "stage"],
        unique=True, postgresql_where=sa.text("stage = 1 OR status <> 'void'"),
    )
    op.create_index(
        "uq_production_packages_profile_out", "production_packages", ["profile_id"],
        unique=True, postgresql_where=sa.text("status = 'out_for_signature'"),
    )
    op.create_check_constraint(
        "ck_production_packages_parent_stage", "production_packages",
        "(stage = 1 AND parent_package_id IS NULL AND source_revision_id IS NULL AND term_sheet_id IS NULL) "
        "OR (stage = 2 AND parent_package_id IS NOT NULL AND source_revision_id IS NOT NULL AND term_sheet_id IS NOT NULL)",
    )

    # ---- signatures: initials + placed-from-file ----
    op.add_column("production_package_signatures", sa.Column("initials", sa.String(8)))
    op.add_column("production_package_signatures", sa.Column("stored_signature_id", _uuid()))
    op.add_column("production_package_signatures", sa.Column("placed_at", sa.DateTime(timezone=True)))
    op.add_column("production_package_signatures", _user_ref("placed_by_user_id"))
    op.create_foreign_key(
        "fk_production_package_signatures_stored", "production_package_signatures", "stored_signatures",
        ["stored_signature_id"], ["id"], ondelete="RESTRICT",
    )


def downgrade() -> None:
    # Stage-two rows cannot survive the absolute unique on profile_id; signatures -> revisions is RESTRICT.
    op.execute("DELETE FROM production_package_signatures WHERE stage = 2")
    op.execute("DELETE FROM production_package_revisions WHERE stage = 2")
    op.execute("UPDATE production_term_sheets SET consumed_by_package_id = NULL")
    op.execute("DELETE FROM production_packages WHERE stage = 2")

    op.drop_constraint("fk_production_package_signatures_stored", "production_package_signatures", type_="foreignkey")
    op.drop_column("production_package_signatures", "placed_by_user_id")
    op.drop_column("production_package_signatures", "placed_at")
    op.drop_column("production_package_signatures", "stored_signature_id")
    op.drop_column("production_package_signatures", "initials")

    op.drop_constraint("ck_production_packages_parent_stage", "production_packages", type_="check")
    op.drop_index("uq_production_packages_profile_out", table_name="production_packages")
    op.drop_index("uq_production_packages_profile_stage_live", table_name="production_packages")
    op.drop_index("ix_production_packages_parent", table_name="production_packages")
    op.drop_constraint("fk_production_packages_term_sheet", "production_packages", type_="foreignkey")
    op.drop_constraint("fk_production_packages_source_revision", "production_packages", type_="foreignkey")
    op.drop_column("production_packages", "execution_pending")
    op.drop_column("production_packages", "sent_share_link_id")
    op.drop_column("production_packages", "sent_via")
    op.drop_column("production_packages", "term_sheet_id")
    op.drop_column("production_packages", "source_revision_id")
    op.drop_column("production_packages", "parent_package_id")
    op.create_unique_constraint("uq_production_packages_profile", "production_packages", ["profile_id"])

    op.drop_constraint("fk_production_term_sheets_consumed_by", "production_term_sheets", type_="foreignkey")
    op.drop_index("uq_production_term_sheets_current", table_name="production_term_sheets")
    op.drop_index("ix_production_term_sheets_profile", table_name="production_term_sheets")
    op.drop_table("production_term_sheets")

    op.drop_index("uq_stored_signatures_live", table_name="stored_signatures")
    op.drop_table("stored_signatures")
