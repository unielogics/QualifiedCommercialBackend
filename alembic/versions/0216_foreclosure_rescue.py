"""Add foreclosure rescue and professional referral partner fields.

Revision ID: 0216_foreclosure_rescue
Revises: 0215_persistent_intake_sms_preference
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0216_foreclosure_rescue"
down_revision = "0215_persistent_intake_sms_preference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "professional_partner_applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("firm_type", sa.String(length=64), nullable=False),
        sa.Column("specialties", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("geographic_states", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("estimated_annual_referrals", sa.Integer(), nullable=True),
        sa.Column("website", sa.String(length=320), nullable=True),
        sa.Column("contact_name", sa.String(length=180), nullable=False),
        sa.Column("contact_title", sa.String(length=120), nullable=True),
        sa.Column("contact_email", sa.String(length=320), nullable=False),
        sa.Column("contact_phone", sa.String(length=48), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("consent", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("referral_partner_companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("promoted_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_professional_partner_applications_status", "professional_partner_applications", ["status"])
    op.create_index("ix_professional_partner_applications_contact_email", "professional_partner_applications", ["contact_email"])
    op.add_column("users", sa.Column("referral_partner_company_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("public_underwriting_intakes", sa.Column("referral_partner_company_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("public_underwriting_intakes", sa.Column("assigned_underwriter_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("public_underwriting_intakes", sa.Column("foreclosure_rescue_status", sa.String(length=40), nullable=True))
    op.add_column("public_underwriting_intakes", sa.Column("foreclosure_sale_date", sa.Date(), nullable=True))
    op.add_column("public_underwriting_intakes", sa.Column("client_contact_suppressed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_foreign_key("fk_rescue_intake_partner_company", "public_underwriting_intakes", "referral_partner_companies", ["referral_partner_company_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_rescue_intake_underwriter", "public_underwriting_intakes", "users", ["assigned_underwriter_user_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_public_underwriting_intakes_referral_partner_company_id", "public_underwriting_intakes", ["referral_partner_company_id"])
    op.create_index("ix_public_underwriting_intakes_assigned_underwriter_user_id", "public_underwriting_intakes", ["assigned_underwriter_user_id"])
    op.create_index("ix_public_underwriting_intakes_foreclosure_rescue_status", "public_underwriting_intakes", ["foreclosure_rescue_status"])
    op.create_index("ix_public_underwriting_intakes_foreclosure_sale_date", "public_underwriting_intakes", ["foreclosure_sale_date"])


def downgrade() -> None:
    op.drop_index("ix_public_underwriting_intakes_foreclosure_sale_date", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_foreclosure_rescue_status", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_assigned_underwriter_user_id", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_referral_partner_company_id", table_name="public_underwriting_intakes")
    op.drop_constraint("fk_rescue_intake_underwriter", "public_underwriting_intakes", type_="foreignkey")
    op.drop_constraint("fk_rescue_intake_partner_company", "public_underwriting_intakes", type_="foreignkey")
    op.drop_column("public_underwriting_intakes", "client_contact_suppressed")
    op.drop_column("public_underwriting_intakes", "foreclosure_sale_date")
    op.drop_column("public_underwriting_intakes", "foreclosure_rescue_status")
    op.drop_column("public_underwriting_intakes", "assigned_underwriter_user_id")
    op.drop_column("public_underwriting_intakes", "referral_partner_company_id")
    op.drop_column("users", "referral_partner_company_admin")
    op.drop_index("ix_professional_partner_applications_contact_email", table_name="professional_partner_applications")
    op.drop_index("ix_professional_partner_applications_status", table_name="professional_partner_applications")
    op.drop_table("professional_partner_applications")
