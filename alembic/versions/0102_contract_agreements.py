"""Real contract templates: ReferralPartnerCompany + ContractAgreement.

Retires the interim broker-NDA placeholder shipped earlier the same day
(broker_nda_acceptances, users.nda_signed_at) — safe to fully drop rather
than deprecate-in-place since it shipped with zero production signatures.
Replaces it with a generalized ContractAgreement model covering all 5 real
contract templates (Platform Access, Referral Protection, SBA Engagement,
Client Engagement, Consulting Addendum), signable by either an individual
User or a company (ReferralPartnerCompany) via a polymorphic
subject_type/subject_id pair.

Also adds:
  - referral_partner_companies: the dealer-partner referral company entity,
    linked from users.referral_partner_company_id (set at invite time).
  - contract_number_seq: one global sequence backing the human-readable
    QC-{TYPE_CODE}-{YYYY}-{seq} contract numbers, guaranteeing uniqueness
    without a race-prone read-then-increment in application code.

Revision ID: 0102_contract_agreements
Revises: 0101_broker_nda_acceptance
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0102_contract_agreements"
down_revision = "0101_broker_nda_acceptance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- retire the interim broker-NDA placeholder ---
    op.drop_index("ix_broker_nda_acceptances_user_id", table_name="broker_nda_acceptances")
    op.drop_table("broker_nda_acceptances")
    op.drop_column("users", "nda_signed_at")

    # --- referral_partner_companies ---
    op.create_table(
        "referral_partner_companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("state_of_formation", sa.String(64), nullable=True),
        sa.Column("principal_address", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_referral_partner_companies_name", "referral_partner_companies", ["name"], unique=True
    )

    # --- users.referral_partner_company_id ---
    op.add_column(
        "users",
        sa.Column(
            "referral_partner_company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("referral_partner_companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # --- contract_number_seq ---
    op.execute(sa.text("CREATE SEQUENCE contract_number_seq START WITH 1 INCREMENT BY 1"))

    # --- contract_agreements ---
    op.create_table(
        "contract_agreements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contract_type", sa.String(32), nullable=False),
        sa.Column("contract_number", sa.String(32), nullable=False, unique=True),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_values", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("document_hash", sa.String(128), nullable=False),
        sa.Column("typed_name", sa.String(160), nullable=False),
        sa.Column("esign_consent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("signature_s3_key", sa.String(512), nullable=True),
        sa.Column("signature_hash", sa.String(128), nullable=True),
        sa.Column("certificate_s3_key", sa.String(512), nullable=True),
        sa.Column("certificate_hash", sa.String(128), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_contract_agreements_contract_type", "contract_agreements", ["contract_type"])
    op.create_index("ix_contract_agreements_contract_number", "contract_agreements", ["contract_number"], unique=True)
    op.create_index("ix_contract_agreements_subject_id", "contract_agreements", ["subject_id"])


def downgrade() -> None:
    op.drop_index("ix_contract_agreements_subject_id", table_name="contract_agreements")
    op.drop_index("ix_contract_agreements_contract_number", table_name="contract_agreements")
    op.drop_index("ix_contract_agreements_contract_type", table_name="contract_agreements")
    op.drop_table("contract_agreements")

    op.execute(sa.text("DROP SEQUENCE contract_number_seq"))

    op.drop_column("users", "referral_partner_company_id")

    op.drop_index("ix_referral_partner_companies_name", table_name="referral_partner_companies")
    op.drop_table("referral_partner_companies")

    op.add_column("users", sa.Column("nda_signed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "broker_nda_acceptances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("document_hash", sa.String(128), nullable=False),
        sa.Column("typed_name", sa.String(160), nullable=False),
        sa.Column("esign_consent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("signature_s3_key", sa.String(512), nullable=True),
        sa.Column("signature_hash", sa.String(128), nullable=True),
        sa.Column("certificate_s3_key", sa.String(512), nullable=True),
        sa.Column("certificate_hash", sa.String(128), nullable=True),
        sa.Column("prior_relationships_disclosure", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_broker_nda_acceptances_user_id", "broker_nda_acceptances", ["user_id"])
