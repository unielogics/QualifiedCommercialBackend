"""Deal registrations: real per-deal registration numbering for Exhibit 1
("Deal Registration and Introduction Confirmation") of the Referral
Protection Agreement, issued by an admin each time Qualified Commercial
introduces a specific financing opportunity to a referral partner company --
a separate, later event from signing the master agreement.

deal_registration_number_seq mirrors contract_number_seq's pattern exactly.

Revision ID: 0103_deal_registrations
Revises: 0102_contract_agreements
Create Date: 2026-08-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0103_deal_registrations"
down_revision = "0102_contract_agreements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("CREATE SEQUENCE deal_registration_number_seq START WITH 1 INCREMENT BY 1"))

    op.create_table(
        "deal_registrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "referral_partner_company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("referral_partner_companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("registration_number", sa.String(32), nullable=False, unique=True),
        sa.Column("introduced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_borrower", sa.String(255), nullable=False),
        sa.Column("financing_opportunity", sa.Text(), nullable=False),
        sa.Column("introduced_capital_source", sa.String(255), nullable=False),
        sa.Column("introduced_program", sa.String(255), nullable=True),
        sa.Column("introduced_contact", sa.String(255), nullable=True),
        sa.Column("method_of_introduction", sa.String(32), nullable=False),
        sa.Column("method_other_description", sa.String(255), nullable=True),
        sa.Column("documents_transmitted", sa.Text(), nullable=True),
        sa.Column("coded_designation", sa.String(255), nullable=True),
        sa.Column("capital_source_number", sa.String(64), nullable=True),
        sa.Column("date_identity_disclosed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("certificate_s3_key", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_deal_registrations_referral_partner_company_id",
        "deal_registrations",
        ["referral_partner_company_id"],
    )
    op.create_index(
        "ix_deal_registrations_registration_number", "deal_registrations", ["registration_number"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_deal_registrations_registration_number", table_name="deal_registrations")
    op.drop_index("ix_deal_registrations_referral_partner_company_id", table_name="deal_registrations")
    op.drop_table("deal_registrations")
    op.execute(sa.text("DROP SEQUENCE deal_registration_number_seq"))
