"""Add client payment pre-authorization tables.

Revision ID: 0086_billing_preauthorization
Revises: 0085_bucket_vendor_access
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0086_billing_preauthorization"
down_revision = "0085_bucket_vendor_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_payment_methods",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stripe_customer_id", sa.String(length=128), nullable=False),
        sa.Column("stripe_payment_method_id", sa.String(length=128), nullable=False),
        sa.Column("setup_intent_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("brand", sa.String(length=32), nullable=True),
        sa.Column("last4", sa.String(length=4), nullable=True),
        sa.Column("exp_month", sa.Integer(), nullable=True),
        sa.Column("exp_year", sa.Integer(), nullable=True),
        sa.Column("billing_name", sa.String(length=160), nullable=True),
        sa.Column("billing_email", sa.String(length=320), nullable=True),
        sa.Column("billing_phone", sa.String(length=48), nullable=True),
        sa.Column("billing_line1", sa.String(length=240), nullable=True),
        sa.Column("billing_line2", sa.String(length=240), nullable=True),
        sa.Column("billing_city", sa.String(length=160), nullable=True),
        sa.Column("billing_state", sa.String(length=80), nullable=True),
        sa.Column("billing_postal_code", sa.String(length=32), nullable=True),
        sa.Column("billing_country", sa.String(length=2), nullable=True),
        sa.Column("verification_status", sa.String(length=64), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_payment_method_id"),
    )
    op.create_index("ix_client_payment_methods_client_id", "client_payment_methods", ["client_id"])
    op.create_index("ix_client_payment_methods_setup_intent_id", "client_payment_methods", ["setup_intent_id"])
    op.create_index("ix_client_payment_methods_stripe_customer_id", "client_payment_methods", ["stripe_customer_id"])
    op.create_index("ix_client_payment_methods_stripe_payment_method_id", "client_payment_methods", ["stripe_payment_method_id"])
    op.create_index("ix_client_payment_methods_user_id", "client_payment_methods", ["user_id"])

    op.create_table(
        "payment_authorizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_method_row_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="started", nullable=False),
        sa.Column("document_version", sa.String(length=32), nullable=False),
        sa.Column("document_hash", sa.String(length=128), nullable=False),
        sa.Column("typed_name", sa.String(length=160), nullable=True),
        sa.Column("esign_consent", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("payment_terms_consent", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("signature_s3_key", sa.String(length=512), nullable=True),
        sa.Column("signature_hash", sa.String(length=128), nullable=True),
        sa.Column("certificate_s3_key", sa.String(length=512), nullable=True),
        sa.Column("certificate_hash", sa.String(length=128), nullable=True),
        sa.Column("stripe_customer_id", sa.String(length=128), nullable=True),
        sa.Column("stripe_payment_method_id", sa.String(length=128), nullable=True),
        sa.Column("setup_intent_id", sa.String(length=128), nullable=True),
        sa.Column("setup_intent_status", sa.String(length=64), nullable=True),
        sa.Column("billing_name", sa.String(length=160), nullable=True),
        sa.Column("billing_email", sa.String(length=320), nullable=True),
        sa.Column("billing_phone", sa.String(length=48), nullable=True),
        sa.Column("billing_line1", sa.String(length=240), nullable=True),
        sa.Column("billing_line2", sa.String(length=240), nullable=True),
        sa.Column("billing_city", sa.String(length=160), nullable=True),
        sa.Column("billing_state", sa.String(length=80), nullable=True),
        sa.Column("billing_postal_code", sa.String(length=32), nullable=True),
        sa.Column("billing_country", sa.String(length=2), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("device_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_method_row_id"], ["client_payment_methods.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payment_authorizations_client_id", "payment_authorizations", ["client_id"])
    op.create_index("ix_payment_authorizations_client_status", "payment_authorizations", ["client_id", "status"])
    op.create_index("ix_payment_authorizations_setup_intent_id", "payment_authorizations", ["setup_intent_id"])
    op.create_index("ix_payment_authorizations_status", "payment_authorizations", ["status"])
    op.create_index("ix_payment_authorizations_stripe_customer_id", "payment_authorizations", ["stripe_customer_id"])
    op.create_index("ix_payment_authorizations_stripe_payment_method_id", "payment_authorizations", ["stripe_payment_method_id"])
    op.create_index("ix_payment_authorizations_user_id", "payment_authorizations", ["user_id"])
    op.create_index("ix_payment_authorizations_user_status", "payment_authorizations", ["user_id", "status"])

    op.create_table(
        "esign_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["authorization_id"], ["payment_authorizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_esign_events_authorization_id", "esign_events", ["authorization_id"])
    op.create_index("ix_esign_events_client_id", "esign_events", ["client_id"])
    op.create_index("ix_esign_events_event_type", "esign_events", ["event_type"])
    op.create_index("ix_esign_events_user_id", "esign_events", ["user_id"])

    op.create_table(
        "billable_expenses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bucket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending_approval", nullable=False),
        sa.Column("category", sa.String(length=64), server_default="other", nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="usd", nullable=False),
        sa.Column("vendor_name", sa.String(length=160), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["bucket_id"], ["buckets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_billable_expenses_bucket_id", "billable_expenses", ["bucket_id"])
    op.create_index("ix_billable_expenses_category", "billable_expenses", ["category"])
    op.create_index("ix_billable_expenses_client_created", "billable_expenses", ["client_id", "created_at"])
    op.create_index("ix_billable_expenses_client_id", "billable_expenses", ["client_id"])
    op.create_index("ix_billable_expenses_client_status", "billable_expenses", ["client_id", "status"])
    op.create_index("ix_billable_expenses_loan_id", "billable_expenses", ["loan_id"])
    op.create_index("ix_billable_expenses_status", "billable_expenses", ["status"])
    op.create_index("ix_billable_expenses_stripe_payment_intent_id", "billable_expenses", ["stripe_payment_intent_id"])

    op.create_table(
        "charge_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expense_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_method_row_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="usd", nullable=False),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("requires_action_url", sa.String(length=1024), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["expense_id"], ["billable_expenses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_method_row_id"], ["client_payment_methods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_charge_attempts_client_id", "charge_attempts", ["client_id"])
    op.create_index("ix_charge_attempts_expense_id", "charge_attempts", ["expense_id"])
    op.create_index("ix_charge_attempts_status", "charge_attempts", ["status"])
    op.create_index("ix_charge_attempts_stripe_payment_intent_id", "charge_attempts", ["stripe_payment_intent_id"])


def downgrade() -> None:
    op.drop_index("ix_charge_attempts_stripe_payment_intent_id", table_name="charge_attempts")
    op.drop_index("ix_charge_attempts_status", table_name="charge_attempts")
    op.drop_index("ix_charge_attempts_expense_id", table_name="charge_attempts")
    op.drop_index("ix_charge_attempts_client_id", table_name="charge_attempts")
    op.drop_table("charge_attempts")

    op.drop_index("ix_billable_expenses_stripe_payment_intent_id", table_name="billable_expenses")
    op.drop_index("ix_billable_expenses_status", table_name="billable_expenses")
    op.drop_index("ix_billable_expenses_loan_id", table_name="billable_expenses")
    op.drop_index("ix_billable_expenses_client_status", table_name="billable_expenses")
    op.drop_index("ix_billable_expenses_client_id", table_name="billable_expenses")
    op.drop_index("ix_billable_expenses_client_created", table_name="billable_expenses")
    op.drop_index("ix_billable_expenses_category", table_name="billable_expenses")
    op.drop_index("ix_billable_expenses_bucket_id", table_name="billable_expenses")
    op.drop_table("billable_expenses")

    op.drop_index("ix_esign_events_user_id", table_name="esign_events")
    op.drop_index("ix_esign_events_event_type", table_name="esign_events")
    op.drop_index("ix_esign_events_client_id", table_name="esign_events")
    op.drop_index("ix_esign_events_authorization_id", table_name="esign_events")
    op.drop_table("esign_events")

    op.drop_index("ix_payment_authorizations_user_status", table_name="payment_authorizations")
    op.drop_index("ix_payment_authorizations_user_id", table_name="payment_authorizations")
    op.drop_index("ix_payment_authorizations_stripe_payment_method_id", table_name="payment_authorizations")
    op.drop_index("ix_payment_authorizations_stripe_customer_id", table_name="payment_authorizations")
    op.drop_index("ix_payment_authorizations_status", table_name="payment_authorizations")
    op.drop_index("ix_payment_authorizations_setup_intent_id", table_name="payment_authorizations")
    op.drop_index("ix_payment_authorizations_client_status", table_name="payment_authorizations")
    op.drop_index("ix_payment_authorizations_client_id", table_name="payment_authorizations")
    op.drop_table("payment_authorizations")

    op.drop_index("ix_client_payment_methods_user_id", table_name="client_payment_methods")
    op.drop_index("ix_client_payment_methods_stripe_payment_method_id", table_name="client_payment_methods")
    op.drop_index("ix_client_payment_methods_stripe_customer_id", table_name="client_payment_methods")
    op.drop_index("ix_client_payment_methods_setup_intent_id", table_name="client_payment_methods")
    op.drop_index("ix_client_payment_methods_client_id", table_name="client_payment_methods")
    op.drop_table("client_payment_methods")
