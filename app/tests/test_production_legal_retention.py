from __future__ import annotations

import inspect
from pathlib import Path

from app.models.bucket import BucketFile
from app.models.document import Document
from app.models.payments import ActualFundingConfirmation, BankDirectFeeReceipt
from app.models.production_package import (
    ProductionPackage,
    ProductionPackageRevision,
    ProductionPackageSignature,
    ProductionTermSheet,
)
from app.routers import documents

MIGRATION = Path("alembic/versions/0241_production_legal_retention.py")

PRODUCTION_LEGAL_MODELS = (
    ProductionPackage,
    ProductionTermSheet,
    ProductionPackageRevision,
    ProductionPackageSignature,
)


def _migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_production_legal_retention_is_the_migration_head() -> None:
    source = _migration_source()

    assert 'revision = "0241_production_legal_retention"' in source
    assert 'down_revision = "0240_payment_retention_guards"' in source
    assert len("0241_production_legal_retention") <= 32


def test_all_production_legal_tables_have_direct_delete_guards() -> None:
    source = _migration_source()

    for model in PRODUCTION_LEGAL_MODELS:
        assert f'"{model.__tablename__}"' in source
    assert "CREATE TRIGGER trg_qc_production_legal_delete" in source
    assert "physical deletion of production legal history requires an explicit governed retention purge" in source
    assert "history_anchor + INTERVAL '2 years'" in source


def test_production_legal_retention_uses_latest_lifecycle_event() -> None:
    source = _migration_source()

    for timestamp in (
        "presentation_generated_at",
        "sent_at",
        "executed_at",
        "voided_at",
        "completed_at",
        "viewed_at",
        "esign_consent_at",
        "signed_at",
        "recorded_at",
        "placed_at",
        "entered_at",
        "superseded_at",
        "withdrawn_at",
        "consumed_at",
    ):
        assert f"'{timestamp}'" in source


def test_production_legal_deadlines_propagate_to_the_effective_file_graph() -> None:
    source = _migration_source()

    assert "qc_propagate_production_legal_retention" in source
    assert "UPDATE application_profiles AS profile" in source
    assert "profile.protected_until" in source
    assert "qc_payment_profile_retention_blocked(profile_uuid)" in source
    assert "ApplicationProfile/Intake/Bucket/File graph" in source


def test_package_cascade_children_are_independently_guarded() -> None:
    revision_package_fk = next(
        iter(ProductionPackageRevision.__table__.c.package_id.foreign_keys)
    )
    signature_package_fk = next(
        iter(ProductionPackageSignature.__table__.c.package_id.foreign_keys)
    )
    signature_revision_fk = next(
        iter(ProductionPackageSignature.__table__.c.revision_id.foreign_keys)
    )
    package_profile_fk = next(
        iter(ProductionPackage.__table__.c.profile_id.foreign_keys)
    )
    term_profile_fk = next(
        iter(ProductionTermSheet.__table__.c.profile_id.foreign_keys)
    )

    assert revision_package_fk.ondelete == "CASCADE"
    assert signature_package_fk.ondelete == "CASCADE"
    assert signature_revision_fk.ondelete == "RESTRICT"
    assert package_profile_fk.ondelete == "RESTRICT"
    assert term_profile_fk.ondelete == "RESTRICT"


def test_production_legal_downgrade_removes_every_trigger_and_function() -> None:
    source = _migration_source()

    assert "DROP TRIGGER IF EXISTS trg_qc_production_legal_delete" in source
    assert "DROP TRIGGER IF EXISTS trg_qc_production_legal_retention" in source
    assert "DROP FUNCTION IF EXISTS qc_guard_production_legal_record_delete()" in source
    assert "DROP FUNCTION IF EXISTS qc_propagate_production_legal_retention()" in source
    assert "DROP FUNCTION IF EXISTS qc_production_legal_anchor(jsonb)" in source
    assert "DROP FUNCTION IF EXISTS qc_production_legal_profile_uuid(text, jsonb)" in source


def test_payment_evidence_documents_are_conditionally_guarded_and_backfilled() -> None:
    source = _migration_source()

    document_loan_fk = next(iter(Document.__table__.c.loan_id.foreign_keys))
    confirmation_fk = next(
        iter(ActualFundingConfirmation.__table__.c.evidence_document_id.foreign_keys)
    )
    receipt_fk = next(
        iter(BankDirectFeeReceipt.__table__.c.evidence_document_id.foreign_keys)
    )
    assert document_loan_fk.ondelete == "CASCADE"
    assert confirmation_fk.ondelete == "SET NULL"
    assert receipt_fk.ondelete == "SET NULL"
    assert "CREATE TRIGGER trg_qc_payment_evidence_document_delete" in source
    assert "BEFORE DELETE ON documents" in source
    assert "FROM actual_funding_confirmations AS confirmation" in source
    assert "FROM bank_direct_fee_receipts AS receipt" in source
    assert "IF NOT is_payment_evidence THEN" in source
    assert "RETURN OLD;" in source
    assert "physical deletion of payment evidence requires an explicit governed retention purge" in source
    assert "payment_deadline > CURRENT_TIMESTAMP" in source
    assert "'actual_funding_date'" in source
    assert "'received_on'" in source
    assert "qc_payment_profile_retention_blocked(" in source
    unrelated_check = source.index("IF NOT is_payment_evidence THEN")
    governed_check = source.index(
        "physical deletion of payment evidence requires an explicit governed retention purge"
    )
    assert unrelated_check < governed_check


def test_payment_evidence_deadline_reaches_profile_and_bucket_file_graph() -> None:
    source = _migration_source()

    assert "qc_propagate_payment_evidence_retention" in source
    assert "AFTER INSERT OR UPDATE ON" in source
    assert "AFTER INSERT OR UPDATE OF evidence_document_id" not in source
    assert "UPDATE actual_funding_confirmations" in source
    assert "UPDATE bank_direct_fee_receipts" in source
    assert "UPDATE application_profiles AS profile" in source
    assert "UPDATE bucket_files AS file" in source
    assert "bucket_file_id" not in Document.__table__.columns
    assert "s3_key" in Document.__table__.columns
    assert "s3_key" in BucketFile.__table__.columns
    assert "protected_until" in BucketFile.__table__.columns
    assert "file.s3_key = document.s3_key" in source
    assert "document.s3_key IS NOT NULL" in source


def test_document_merge_repoints_payment_evidence_before_deleting_orphan() -> None:
    source = inspect.getsource(documents.route_document)

    blob_move = source.index("target.s3_key = doc.s3_key")
    confirmation_update = source.index("update(ActualFundingConfirmation)")
    receipt_update = source.index("update(BankDirectFeeReceipt)")
    delete_orphan = source.index("await db.delete(doc)")
    assert blob_move < confirmation_update < delete_orphan
    assert receipt_update < delete_orphan
    assert ".values(evidence_document_id=target.id)" in source
