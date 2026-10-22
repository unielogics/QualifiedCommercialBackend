from __future__ import annotations

import inspect
from pathlib import Path

from app.models.payments import (
    AchMandate,
    ActualFundingConfirmation,
    BankDirectFeeReceipt,
    FeeAllocationVersion,
    FeeObligation,
    FeeObligationLine,
    PaymentAuditEvent,
    PaymentDebitNotice,
    PaymentFundingSource,
    PaymentInstallment,
    PaymentRefund,
    PaymentServicingAuthority,
    PaymentTransfer,
    PaymentTransferEvent,
    PrivateFundingPaymentPlan,
)
from app.services import payments

MIGRATION = Path(
    "alembic/versions/0240_payment_retention_guards.py"
)

PAYMENT_MODELS = (
    FeeObligation,
    FeeObligationLine,
    FeeAllocationVersion,
    ActualFundingConfirmation,
    PaymentFundingSource,
    AchMandate,
    PaymentDebitNotice,
    PaymentServicingAuthority,
    PrivateFundingPaymentPlan,
    PaymentInstallment,
    PaymentTransfer,
    PaymentTransferEvent,
    PaymentRefund,
    BankDirectFeeReceipt,
    PaymentAuditEvent,
)


def _migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_payment_retention_migration_covers_the_complete_ledger() -> None:
    source = _migration_source()

    assert 'down_revision = "0239_ai_intake_record_retention"' in source
    assert len("0240_payment_retention_guards") <= 32
    for model in PAYMENT_MODELS:
        assert f'"{model.__tablename__}"' in source
    assert "CREATE TRIGGER trg_qc_retained_payment_delete" in source
    assert "physical deletion of payment history requires an explicit governed retention purge" in source


def test_payment_delete_guard_uses_own_age_and_effective_profile_graph() -> None:
    source = _migration_source()

    assert "history_anchor + INTERVAL '2 years'" in source
    assert "qc_payment_profile_retention_blocked(profile_uuid)" in source
    assert "WITH RECURSIVE parent_profile" in source
    assert "FROM bucket_intake_links AS link" in source
    assert "FROM bucket_intake_link_files AS link_file" in source
    assert "FROM bucket_files AS bf" in source
    assert "FROM application_profiles AS linked_profile" in source
    assert "ap.legal_hold" in source
    assert "linked_profile.legal_hold" in source
    assert "bf.legal_hold" in source


def test_ach_retention_uses_terminal_and_provider_event_anchors() -> None:
    source = _migration_source()

    assert "qc_normalize_ach_mandate_retention" in source
    assert "NEW.revoked_at" in source
    assert "NEW.terminated_at" in source
    assert "NEW.expires_at" in source
    assert "qc_extend_ach_retention_from_transfer_event" in source
    assert "NEW.event_timestamp" in source
    assert "qc_extend_ach_retention_from_refund" in source
    assert "NEW.completed_at" in source
    assert "certificate_bucket_file_id" in source
    assert "notice_bucket_file_id" in source
    assert "governing_agreement_document_id" in source


def test_payment_rows_anchor_to_domain_lifecycle_dates() -> None:
    source = _migration_source()

    for timestamp_key in (
        "consulting_milestone_confirmed_at",
        "earned_confirmed_at",
        "verified_at",
        "agreement_executed_at",
        "actual_funding_date",
        "effective_to",
        "due_date",
        "received_on",
    ):
        assert f"'{timestamp_key}'" in source


def test_legacy_provider_events_extend_retention_by_plaid_transfer_id() -> None:
    source = _migration_source()

    assert "event.transfer_id IS NULL" in source
    assert "event.plaid_transfer_id IS NOT NULL" in source
    assert "transfer.plaid_transfer_id = event.plaid_transfer_id" in source


def test_payment_compliance_messages_are_retained_with_the_file_graph() -> None:
    source = _migration_source()

    assert "qc_guard_retained_payment_message_delete" in source
    for context in (
        "success_fee_agreement",
        "ach_mandate_proof",
        "ach_debit_notice",
        "ach_mandate_revocation",
    ):
        assert context in source
    assert "payment delivery evidence has no retained ApplicationProfile reference" in source
    assert "qc_payment_profile_retention_blocked(OLD.profile_id)" in source


def test_cascade_and_unlink_edges_are_covered_by_parent_and_child_guards() -> None:
    source = _migration_source()

    obligation_line_fk = next(
        iter(FeeObligationLine.__table__.c.obligation_id.foreign_keys)
    )
    installment_fk = next(iter(PaymentInstallment.__table__.c.plan_id.foreign_keys))
    event_fk = next(iter(PaymentTransferEvent.__table__.c.transfer_id.foreign_keys))
    notice_transfer_fk = next(
        iter(PaymentDebitNotice.__table__.c.transfer_id.foreign_keys)
    )

    assert obligation_line_fk.ondelete == "CASCADE"
    assert installment_fk.ondelete == "CASCADE"
    assert event_fk.ondelete == "SET NULL"
    assert notice_transfer_fk.ondelete == "SET NULL"
    for table in (
        "fee_obligations",
        "fee_obligation_lines",
        "private_funding_payment_plans",
        "payment_installments",
        "payment_transfers",
        "payment_transfer_events",
        "payment_debit_notices",
    ):
        assert f'"{table}"' in source


def test_refund_paths_extend_authorization_proof_retention() -> None:
    event_source = inspect.getsource(payments.apply_plaid_transfer_event)
    request_source = inspect.getsource(payments.create_refund_intent)

    assert "refund_transfer.mandate_id" in event_source
    assert "extend_mandate_retention" in event_source
    assert "transfer.mandate_id" in request_source
    assert "extend_mandate_retention" in request_source

