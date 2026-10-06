"""Origination fee ACH and private-funding payment ledger.

Revision ID: 0233_origination_fee_payments
Revises: 0232_accepted_deal_economics
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0233_origination_fee_payments"
down_revision = "0232_accepted_deal_economics"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "fee_obligations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_id", UUID, sa.ForeignKey("clients.id", ondelete="SET NULL")),
        sa.Column("loan_id", UUID, sa.ForeignKey("loans.id", ondelete="SET NULL")),
        sa.Column("intake_id", UUID, sa.ForeignKey("public_underwriting_intakes.id", ondelete="SET NULL")),
        sa.Column("production_package_id", UUID, sa.ForeignKey("production_packages.id", ondelete="SET NULL")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("record_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(32), server_default="prepared", nullable=False),
        sa.Column("currency", sa.String(3), server_default="usd", nullable=False),
        sa.Column("accepted_amount", sa.Numeric(14, 2)),
        sa.Column("funded_amount", sa.Numeric(14, 2)),
        sa.Column("origination_points", sa.Numeric(7, 4)),
        sa.Column("origination_fee_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("consulting_fee_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("gross_fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("client_ach_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("origination_client_ach_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("consulting_client_ach_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("bank_direct_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("external_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("deferred_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("waived_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("business_name_snapshot", sa.String(240)),
        sa.Column("client_name_snapshot", sa.String(180)),
        sa.Column("client_email_snapshot", sa.String(320)),
        sa.Column("economics_snapshot", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("agreement_document_id", UUID, sa.ForeignKey("bucket_files.id", ondelete="RESTRICT")),
        sa.Column("agreement_reference", sa.String(240)),
        sa.Column("agreement_sha256", sa.String(64)),
        sa.Column("agreement_snapshot", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("consulting_milestone_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("consulting_milestone_confirmed_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("authorization_sent_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("application_profile_id", "version", name="uq_fee_obligations_profile_version"),
        sa.CheckConstraint("version > 0 AND record_version > 0", name="ck_fee_obligations_versions"),
        sa.CheckConstraint("gross_fee_cents >= 0", name="ck_fee_obligations_gross_nonnegative"),
        sa.CheckConstraint("client_ach_cents >= 0 AND bank_direct_cents >= 0 AND external_cents >= 0 AND deferred_cents >= 0 AND waived_cents >= 0", name="ck_fee_obligations_allocations_nonnegative"),
        sa.CheckConstraint("origination_client_ach_cents >= 0 AND consulting_client_ach_cents >= 0", name="ck_fee_obligations_component_ach_nonnegative"),
        sa.CheckConstraint("origination_client_ach_cents + consulting_client_ach_cents = client_ach_cents", name="ck_fee_obligations_component_ach_balanced"),
        sa.CheckConstraint("origination_client_ach_cents <= origination_fee_cents AND consulting_client_ach_cents <= consulting_fee_cents", name="ck_fee_obligations_component_ach_within_fee"),
        sa.CheckConstraint("client_ach_cents + bank_direct_cents + external_cents + deferred_cents + waived_cents = gross_fee_cents", name="ck_fee_obligations_allocation_balanced"),
    )
    op.create_index("ix_fee_obligations_application_profile_id", "fee_obligations", ["application_profile_id"])
    op.create_index("ix_fee_obligations_client_id", "fee_obligations", ["client_id"])
    op.create_index("ix_fee_obligations_loan_id", "fee_obligations", ["loan_id"])
    op.create_index("ix_fee_obligations_intake_id", "fee_obligations", ["intake_id"])
    op.create_index("ix_fee_obligations_status", "fee_obligations", ["status"])
    op.create_index("uq_fee_obligations_current", "fee_obligations", ["application_profile_id"], unique=True, postgresql_where=sa.text("superseded_at IS NULL AND status != 'cancelled'"))

    op.create_table(
        "fee_obligation_lines",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("obligation_id", UUID, sa.ForeignKey("fee_obligations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("line_type", sa.String(32), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("client_ach_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("calculation_snapshot", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("agreement_required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("earned_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("earned_confirmed_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("obligation_id", "line_type", name="uq_fee_obligation_lines_type"),
        sa.CheckConstraint("amount_cents >= 0", name="ck_fee_obligation_lines_amount"),
        sa.CheckConstraint("client_ach_cents >= 0 AND client_ach_cents <= amount_cents", name="ck_fee_obligation_lines_client_ach"),
    )
    op.create_index("ix_fee_obligation_lines_obligation_id", "fee_obligation_lines", ["obligation_id"])

    op.create_table(
        "fee_allocation_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("obligation_id", UUID, sa.ForeignKey("fee_obligations.id", ondelete="SET NULL")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("collection_mode", sa.String(24), nullable=False),
        sa.Column("gross_fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("origination_client_ach_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("consulting_client_ach_cents", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("allocation", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("allocation_sha256", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("application_profile_id", "version", name="uq_fee_allocation_versions_profile_version"),
        sa.CheckConstraint("version > 0", name="ck_fee_allocation_versions_version"),
        sa.CheckConstraint("origination_client_ach_cents >= 0 AND consulting_client_ach_cents >= 0 AND origination_client_ach_cents + consulting_client_ach_cents <= gross_fee_cents", name="ck_fee_allocation_versions_component_ach"),
    )
    op.create_index("ix_fee_allocation_versions_application_profile_id", "fee_allocation_versions", ["application_profile_id"])
    op.create_index("ix_fee_allocation_versions_obligation_id", "fee_allocation_versions", ["obligation_id"])

    op.create_table(
        "actual_funding_confirmations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("production_package_id", UUID, sa.ForeignKey("production_packages.id", ondelete="SET NULL")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actual_funding_date", sa.Date(), nullable=False),
        sa.Column("actual_funded_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("funding_party_name", sa.String(180), nullable=False),
        sa.Column("funding_reference", sa.String(160)),
        sa.Column("note", sa.Text()),
        sa.Column("evidence_document_id", UUID, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("source", sa.String(32), server_default="manual", nullable=False),
        sa.Column("confirmed_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("application_profile_id", "version", name="uq_actual_funding_profile_version"),
        sa.CheckConstraint("actual_funded_amount > 0", name="ck_actual_funding_amount"),
    )
    op.create_index("ix_actual_funding_confirmations_application_profile_id", "actual_funding_confirmations", ["application_profile_id"])
    op.create_index("uq_actual_funding_current", "actual_funding_confirmations", ["application_profile_id"], unique=True, postgresql_where=sa.text("superseded_at IS NULL"))

    op.create_table(
        "payment_funding_sources",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_id", UUID, sa.ForeignKey("clients.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("ach_class", sa.String(8), nullable=False),
        sa.Column("plaid_item_id", sa.String(128)),
        sa.Column("plaid_account_id", sa.String(128)),
        sa.Column("access_token_ciphertext", sa.Text()),
        sa.Column("account_name", sa.String(180)),
        sa.Column("account_mask", sa.String(8)),
        sa.Column("account_subtype", sa.String(48)),
        sa.Column("institution_name", sa.String(180)),
        sa.Column("holder_name", sa.String(180)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("metadata_json", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_payment_funding_sources_application_profile_id", "payment_funding_sources", ["application_profile_id"])
    op.create_index("ix_payment_funding_sources_client_id", "payment_funding_sources", ["client_id"])
    op.create_index("ix_payment_funding_sources_plaid_item_id", "payment_funding_sources", ["plaid_item_id"])
    op.create_index("ix_payment_funding_sources_profile_status", "payment_funding_sources", ["application_profile_id", "status"])
    op.create_index(
        "uq_payment_funding_sources_current_account",
        "payment_funding_sources",
        ["application_profile_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND status = 'verified'"),
    )

    op.create_table(
        "payment_servicing_authorities",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(24), server_default="active", nullable=False),
        sa.Column("agreement_reference", sa.String(240), nullable=False),
        sa.Column("agreement_sha256", sa.String(64), nullable=False),
        sa.Column("creditor_name", sa.String(180), nullable=False),
        sa.Column("payee_name", sa.String(180), nullable=False),
        sa.Column("settlement_destination_ref", sa.String(240), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
    )
    op.create_index("ix_payment_servicing_authorities_application_profile_id", "payment_servicing_authorities", ["application_profile_id"])
    op.create_index("ix_payment_servicing_authorities_profile_active", "payment_servicing_authorities", ["application_profile_id", "status"])

    op.create_table(
        "private_funding_payment_plans",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_id", UUID, sa.ForeignKey("clients.id", ondelete="SET NULL")),
        sa.Column("loan_id", UUID, sa.ForeignKey("loans.id", ondelete="SET NULL")),
        sa.Column("production_term_sheet_id", UUID, sa.ForeignKey("production_term_sheets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("production_term_sheet_version", sa.Integer(), nullable=False),
        sa.Column("production_package_id", UUID, sa.ForeignKey("production_packages.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("production_package_revision_id", UUID, sa.ForeignKey("production_package_revisions.id", ondelete="RESTRICT")),
        sa.Column("agreement_sha256", sa.String(64), nullable=False),
        sa.Column("agreement_executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_party_kind", sa.String(32), nullable=False),
        sa.Column("creditor_name", sa.String(180), nullable=False),
        sa.Column("payee_name", sa.String(180)),
        sa.Column("settlement_destination_ref", sa.String(240)),
        sa.Column("agreement_reference", sa.String(240), nullable=False),
        sa.Column("funding_confirmation_id", UUID, sa.ForeignKey("actual_funding_confirmations.id", ondelete="RESTRICT")),
        sa.Column("servicing_authority_id", UUID, sa.ForeignKey("payment_servicing_authorities.id", ondelete="RESTRICT")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("record_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("cadence", sa.String(32), nullable=False),
        sa.Column("timezone", sa.String(64), server_default="America/New_York", nullable=False),
        sa.Column("total_amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("installment_count", sa.Integer(), nullable=False),
        sa.Column("first_due_date", sa.Date(), nullable=False),
        sa.Column("next_due_date", sa.Date()),
        sa.Column("schedule_snapshot", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("schedule_sha256", sa.String(64), nullable=False),
        sa.Column("supersedes_id", UUID, sa.ForeignKey("private_funding_payment_plans.id", ondelete="RESTRICT")),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("activated_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("application_profile_id", "version", name="uq_private_payment_plans_profile_version"),
        sa.CheckConstraint("version > 0 AND record_version > 0", name="ck_private_payment_plans_versions"),
        sa.CheckConstraint("total_amount_cents > 0 AND installment_count > 0", name="ck_private_payment_plans_amount_count"),
    )
    op.create_index("ix_private_funding_payment_plans_application_profile_id", "private_funding_payment_plans", ["application_profile_id"])
    op.create_index("ix_private_funding_payment_plans_client_id", "private_funding_payment_plans", ["client_id"])
    op.create_index("ix_private_funding_payment_plans_loan_id", "private_funding_payment_plans", ["loan_id"])
    op.create_index("ix_private_funding_payment_plans_next_due_date", "private_funding_payment_plans", ["next_due_date"])
    op.create_index("ix_private_payment_plans_status_due", "private_funding_payment_plans", ["status", "next_due_date"])
    op.create_index(
        "uq_private_payment_plans_active_profile",
        "private_funding_payment_plans",
        ["application_profile_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "payment_installments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("plan_id", UUID, sa.ForeignKey("private_funding_payment_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), server_default="scheduled", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("plan_id", "sequence", name="uq_payment_installments_plan_sequence"),
        sa.CheckConstraint("sequence > 0 AND amount_cents > 0", name="ck_payment_installments_sequence_amount"),
    )
    op.create_index("ix_payment_installments_plan_id", "payment_installments", ["plan_id"])
    op.create_index("ix_payment_installments_status_due", "payment_installments", ["status", "due_date"])

    op.create_table(
        "ach_mandates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("funding_source_id", UUID, sa.ForeignKey("payment_funding_sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("fee_obligation_id", UUID, sa.ForeignKey("fee_obligations.id", ondelete="RESTRICT")),
        sa.Column("private_plan_id", UUID, sa.ForeignKey("private_funding_payment_plans.id", ondelete="RESTRICT", name="fk_ach_mandates_private_plan")),
        sa.Column("status", sa.String(24), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("ach_class", sa.String(8), nullable=False),
        sa.Column("authorized_amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("authorization_text_version", sa.String(32), nullable=False),
        sa.Column("obligation_sha256", sa.String(64), nullable=False),
        sa.Column("funding_source_snapshot", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("funding_source_sha256", sa.String(64), nullable=False),
        sa.Column("typed_name", sa.String(180), nullable=False),
        sa.Column("payer_name", sa.String(180), nullable=False),
        sa.Column("payer_email", sa.String(320)),
        sa.Column("signature_sha256", sa.String(64)),
        sa.Column("certificate_s3_key", sa.String(512)),
        sa.Column("certificate_sha256", sa.String(64)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.CheckConstraint("(fee_obligation_id IS NOT NULL)::int + (private_plan_id IS NOT NULL)::int = 1", name="ck_ach_mandates_one_target"),
        sa.CheckConstraint("authorized_amount_cents > 0", name="ck_ach_mandates_amount"),
    )
    op.create_index("ix_ach_mandates_application_profile_id", "ach_mandates", ["application_profile_id"])
    op.create_index("ix_ach_mandates_fee_obligation_id", "ach_mandates", ["fee_obligation_id"])
    op.create_index("ix_ach_mandates_private_plan_id", "ach_mandates", ["private_plan_id"])
    op.create_index("ix_ach_mandates_status", "ach_mandates", ["status"])

    op.create_table(
        "payment_transfers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("fee_obligation_id", UUID, sa.ForeignKey("fee_obligations.id", ondelete="RESTRICT")),
        sa.Column("installment_id", UUID, sa.ForeignKey("payment_installments.id", ondelete="RESTRICT")),
        sa.Column("funding_source_id", UUID, sa.ForeignKey("payment_funding_sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("mandate_id", UUID, sa.ForeignKey("ach_mandates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("attempt_group_id", UUID, nullable=False),
        sa.Column("retry_of_transfer_id", UUID, sa.ForeignKey("payment_transfers.id", ondelete="RESTRICT")),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(32), server_default="authorizing", nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("ach_class", sa.String(8), nullable=False),
        sa.Column("plaid_authorization_id", sa.String(128)),
        sa.Column("plaid_transfer_id", sa.String(128)),
        sa.Column("provider_status", sa.String(48)),
        sa.Column("provider_failure_code", sa.String(64)),
        sa.Column("provider_failure_message", sa.Text()),
        sa.Column("provider_failure_retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("funds_available_at", sa.DateTime(timezone=True)),
        sa.Column("returned_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("released_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("provider_metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_transfers_idempotency"),
        sa.UniqueConstraint("plaid_transfer_id", name="uq_payment_transfers_plaid_id"),
        sa.UniqueConstraint("attempt_group_id", "attempt_no", name="uq_payment_transfers_attempt_group_no"),
        sa.CheckConstraint("(fee_obligation_id IS NOT NULL)::int + (installment_id IS NOT NULL)::int = 1", name="ck_payment_transfers_one_target"),
        sa.CheckConstraint("amount_cents > 0", name="ck_payment_transfers_amount"),
    )
    for col in (
        "application_profile_id",
        "fee_obligation_id",
        "installment_id",
        "plaid_authorization_id",
        "attempt_group_id",
        "retry_of_transfer_id",
    ):
        op.create_index(f"ix_payment_transfers_{col}", "payment_transfers", [col])
    op.create_index("ix_payment_transfers_status_created", "payment_transfers", ["status", "created_at"])

    op.create_table(
        "payment_transfer_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("transfer_id", UUID, sa.ForeignKey("payment_transfers.id", ondelete="SET NULL")),
        sa.Column("plaid_event_id", sa.String(128), nullable=False),
        sa.Column("plaid_transfer_id", sa.String(128)),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True)),
        sa.Column("raw_event", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("plaid_event_id", name="uq_payment_transfer_events_plaid_event"),
    )
    op.create_index("ix_payment_transfer_events_transfer_id", "payment_transfer_events", ["transfer_id"])
    op.create_index("ix_payment_transfer_events_plaid_transfer_id", "payment_transfer_events", ["plaid_transfer_id"])
    op.create_index("ix_payment_transfer_events_transfer_created", "payment_transfer_events", ["transfer_id", "created_at"])

    op.create_table(
        "payment_refunds",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("transfer_id", UUID, sa.ForeignKey("payment_transfers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("operation_fingerprint", sa.String(64), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(50), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("plaid_refund_id", sa.String(128)),
        sa.Column("requested_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_refunds_idempotency"),
        sa.UniqueConstraint(
            "transfer_id", "operation_fingerprint", name="uq_payment_refunds_transfer_operation"
        ),
        sa.UniqueConstraint("provider_idempotency_key"),
        sa.UniqueConstraint("plaid_refund_id", name="uq_payment_refunds_plaid_id"),
        sa.CheckConstraint("amount_cents > 0", name="ck_payment_refunds_amount"),
    )
    op.create_index("ix_payment_refunds_transfer_id", "payment_refunds", ["transfer_id"])

    op.create_table(
        "bank_direct_fee_receipts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("obligation_id", UUID, sa.ForeignKey("fee_obligations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("receipt_type", sa.String(24), nullable=False, server_default="bank_direct"),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("reference", sa.String(180), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("evidence_document_id", UUID, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("recorded_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("obligation_id", "reference", name="uq_bank_direct_receipts_reference"),
        sa.CheckConstraint("amount_cents > 0", name="ck_bank_direct_receipts_amount"),
    )
    op.create_index("ix_bank_direct_fee_receipts_obligation_id", "bank_direct_fee_receipts", ["obligation_id"])

    op.create_table(
        "plaid_transfer_cursors",
        sa.Column("environment", sa.String(24), primary_key=True),
        sa.Column("cursor", sa.Text()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        *_timestamps(),
    )

    op.create_table(
        "payment_audit_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("application_profile_id", UUID, sa.ForeignKey("application_profiles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(48), nullable=False),
        sa.Column("entity_id", UUID),
        sa.Column("summary", sa.String(320), nullable=False),
        sa.Column("metadata_json", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payment_audit_events_application_profile_id", "payment_audit_events", ["application_profile_id"])
    op.create_index("ix_payment_audit_profile_created", "payment_audit_events", ["application_profile_id", "created_at"])


def downgrade() -> None:
    for table in (
        "payment_audit_events",
        "plaid_transfer_cursors",
        "bank_direct_fee_receipts",
        "payment_refunds",
        "payment_transfer_events",
        "payment_transfers",
        "ach_mandates",
        "payment_installments",
        "private_funding_payment_plans",
        "payment_servicing_authorities",
        "payment_funding_sources",
        "actual_funding_confirmations",
        "fee_allocation_versions",
        "fee_obligation_lines",
        "fee_obligations",
    ):
        op.drop_table(table)
