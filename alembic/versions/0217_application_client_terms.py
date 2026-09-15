"""Add client-facing offer terms to application profiles.

Revision ID: 0217_application_client_terms
Revises: 0216_foreclosure_rescue
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0217_application_client_terms"
down_revision = "0216_foreclosure_rescue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_term_sheets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("program_key", sa.String(length=64), nullable=False),
        sa.Column("program_name", sa.String(length=160), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("apr_pct", sa.Numeric(7, 3), nullable=False),
        sa.Column("term_months", sa.Integer(), nullable=False),
        sa.Column("funder_type", sa.String(length=32), nullable=False),
        sa.Column("funder_name", sa.String(length=160), nullable=True),
        sa.Column("repayment_frequency", sa.String(length=20), nullable=False),
        sa.Column("payments_per_year", sa.Numeric(8, 3), nullable=False),
        sa.Column("custom_repayment_label", sa.String(length=80), nullable=True),
        sa.Column("debt_service_treatment", sa.String(length=20), nullable=False),
        sa.Column("retained_annual_debt_service", sa.Numeric(14, 2), nullable=True),
        sa.Column("periodic_payment", sa.Numeric(14, 2), nullable=False),
        sa.Column("payment_count", sa.Integer(), nullable=False),
        sa.Column("annual_new_debt_service", sa.Numeric(14, 2), nullable=False),
        sa.Column("projected_annual_debt_service", sa.Numeric(14, 2), nullable=True),
        sa.Column("cash_flow_value", sa.Numeric(14, 2), nullable=True),
        sa.Column("cash_flow_label", sa.String(length=80), nullable=False),
        sa.Column("current_annual_debt_service", sa.Numeric(14, 2), nullable=True),
        sa.Column("annual_property_carrying_costs", sa.Numeric(14, 2), nullable=True),
        sa.Column("dscr_before", sa.Numeric(8, 4), nullable=True),
        sa.Column("dscr_after", sa.Numeric(8, 4), nullable=True),
        sa.Column("dscr_method", sa.String(length=24), nullable=False),
        sa.Column("dscr_status", sa.String(length=24), nullable=False),
        sa.Column("dscr_explanation", sa.Text(), nullable=False),
        sa.Column("dscr_source", sa.String(length=200), nullable=False),
        sa.Column("expiration_days", sa.Integer(), nullable=False),
        sa.Column("closing_estimate_days", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("issued_pdf_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("issued_pdf_sha256", sa.String(length=64), nullable=True),
        sa.Column("issued_filename", sa.String(length=240), nullable=True),
        sa.Column("co_brand_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sponsor_name", sa.String(length=160), nullable=True),
        sa.Column("client_note", sa.Text(), nullable=True),
        sa.Column("conditions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_application_term_sheet_amount"),
        sa.CheckConstraint("apr_pct BETWEEN 0 AND 100", name="ck_application_term_sheet_apr"),
        sa.CheckConstraint("term_months BETWEEN 1 AND 480", name="ck_application_term_sheet_term"),
        sa.CheckConstraint("expiration_days BETWEEN 1 AND 180", name="ck_application_term_sheet_expiration"),
        sa.CheckConstraint("closing_estimate_days BETWEEN 0 AND 180", name="ck_application_term_sheet_close_estimate"),
        sa.CheckConstraint("status IN ('draft', 'issued', 'superseded')", name="ck_application_term_sheet_status"),
        sa.CheckConstraint("repayment_frequency IN ('daily', 'weekly', 'biweekly', 'monthly', 'custom')", name="ck_application_term_sheet_frequency"),
        sa.CheckConstraint("debt_service_treatment IN ('additive', 'refinance')", name="ck_application_term_sheet_debt_treatment"),
        sa.CheckConstraint("payments_per_year > 0", name="ck_application_term_sheet_payments_year"),
        sa.CheckConstraint("periodic_payment > 0", name="ck_application_term_sheet_payment"),
        sa.CheckConstraint("payment_count > 0", name="ck_application_term_sheet_payment_count"),
        sa.CheckConstraint("annual_new_debt_service > 0", name="ck_application_term_sheet_annual_debt"),
        sa.UniqueConstraint("profile_id", "version", name="uq_application_term_sheet_version"),
    )
    op.create_index("ix_application_term_sheets_profile_id", "application_term_sheets", ["profile_id"])
    op.create_index("uq_application_term_sheet_current", "application_term_sheets", ["profile_id"], unique=True, postgresql_where=sa.text("is_current"))
    op.create_table(
        "application_term_sheet_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("term_sheet_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("application_term_sheets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("to_emails", postgresql.JSONB(), nullable=False),
        sa.Column("cc_emails", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider_detail", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=320), nullable=True),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=False),
        sa.Column("pdf_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("sent_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('sending', 'sent', 'failed')", name="ck_application_term_sheet_delivery_status"),
        sa.CheckConstraint("char_length(pdf_sha256) = 64", name="ck_application_term_sheet_delivery_sha"),
    )
    op.create_index("ix_application_term_sheet_deliveries_idempotency_key", "application_term_sheet_deliveries", ["idempotency_key"], unique=True)
    op.create_index("ix_application_term_sheet_deliveries_term_sheet_id", "application_term_sheet_deliveries", ["term_sheet_id"])


def downgrade() -> None:
    op.drop_index("ix_application_term_sheet_deliveries_term_sheet_id", table_name="application_term_sheet_deliveries")
    op.drop_index("ix_application_term_sheet_deliveries_idempotency_key", table_name="application_term_sheet_deliveries")
    op.drop_table("application_term_sheet_deliveries")
    op.drop_index("uq_application_term_sheet_current", table_name="application_term_sheets")
    op.drop_index("ix_application_term_sheets_profile_id", table_name="application_term_sheets")
    op.drop_table("application_term_sheets")
