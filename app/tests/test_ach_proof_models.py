from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy import CheckConstraint

from app.models.bucket import BucketFile
from app.models.payments import AchMandate, FeeObligationLine, PaymentDebitNotice
from app.routers import agreements, public_payments
from app.routers import payments as payment_routes
from app.schemas.payments import PaymentDebitNoticeRead, PaymentSummary
from app.services import ach_fee_workflow, payment_authorization


def _checks(table) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }


def test_debit_notice_retains_artifact_and_delivery_evidence() -> None:
    table = PaymentDebitNotice.__table__

    assert table.c.notice_bucket_file_id.foreign_keys
    notice_file_fk = next(iter(table.c.notice_bucket_file_id.foreign_keys))
    assert notice_file_fk.target_fullname == "bucket_files.id"
    assert notice_file_fk.ondelete == "RESTRICT"
    assert {
        "ix_payment_debit_notices_notice_bucket_file_id",
        "ix_payment_debit_notices_provider_message_id",
        "ix_payment_debit_notices_rfc_message_id",
    } <= {index.name for index in table.indexes}
    assert table.c.delivery_evidence_snapshot.nullable is False


def test_one_time_fee_notice_requires_complete_ordered_window() -> None:
    checks = _checks(PaymentDebitNotice.__table__)

    assert "notice_business_days IS NOT NULL" in checks[
        "ck_payment_debit_notices_one_time_fee_window_required"
    ]
    timing = checks["ck_payment_debit_notices_one_time_fee_timing"]
    assert "revocation_cutoff_at <= scheduled_debit_at" in timing
    assert "debit_window_start_at <= scheduled_debit_at" in timing
    assert "scheduled_debit_at <= debit_window_end_at" in timing


def test_agreement_scope_is_complete_and_matches_fee_component() -> None:
    checks = _checks(FeeObligationLine.__table__)

    complete = checks["ck_fee_obligation_lines_governing_agreement_complete"]
    assert "governing_agreement_document_id IS NULL" in complete
    assert "governing_agreement_document_id IS NOT NULL" in complete
    scope = checks["ck_fee_obligation_lines_agreement_scope"]
    assert "agreement_component_scope = line_type" in scope


def test_new_ach_foreign_keys_are_indexed() -> None:
    assert {
        "ix_ach_mandates_funding_source_id",
        "ix_ach_mandates_agreement_document_id",
        "ix_ach_mandates_certificate_bucket_file_id",
        "ix_ach_mandates_proof_copy_message_send_id",
    } <= {index.name for index in AchMandate.__table__.indexes}
    proof_delivery_fk = next(iter(AchMandate.__table__.c.proof_copy_message_send_id.foreign_keys))
    assert proof_delivery_fk.target_fullname == "message_sends.id"
    assert proof_delivery_fk.ondelete == "SET NULL"
    assert "ix_fee_obligation_lines_governing_agreement_document_id" in {
        index.name for index in FeeObligationLine.__table__.indexes
    }


def test_protected_bucket_artifacts_bind_source_digest_and_s3_version() -> None:
    table = BucketFile.__table__
    protected_index = next(
        index
        for index in table.indexes
        if index.name == "uq_bucket_files_protected_source_ref"
    )

    assert table.c.s3_version_id.nullable is True
    assert protected_index.unique is True
    assert [column.name for column in protected_index.columns] == [
        "source_entity_type",
        "source_entity_id",
        "retention_class",
        "source_immutable_ref",
    ]


def test_protected_pdf_storage_is_content_addressed_and_replay_safe() -> None:
    digest = "a" * 64
    key = ach_fee_workflow._content_addressed_key(
        "payments/ach-mandates/profile/mandate/certificate.pdf", digest
    )
    source = inspect.getsource(ach_fee_workflow._store_protected_pdf)

    assert key.endswith(f"certificate-{digest}.pdf")
    assert "_reserve_protected_file" in source
    assert "prevent_overwrite=True" in source
    assert "expected_sha256=digest" in source


def test_retention_extension_locks_rows_and_includes_component_agreements() -> None:
    source = inspect.getsource(ach_fee_workflow.extend_mandate_retention)

    assert "with_for_update" in source
    assert "FeeObligationLine.governing_agreement_document_id" in source
    assert "locked_mandate.retention_until" in source


def test_certificate_routes_verify_protected_bytes_before_presigning() -> None:
    staff = inspect.getsource(payment_routes.download_ach_mandate_proof)
    public = inspect.getsource(public_payments.public_payment_certificate)
    agreement = inspect.getsource(public_payments.public_fee_agreement_certificate)

    for source in (staff, public, agreement):
        assert "verified_protected_download_url" in source
        assert "presign_private_s3_object" not in source
    audit = inspect.getsource(agreements._rows_from_ach_mandates)
    assert "verified_protected_download_url" in audit


def test_private_storage_conditional_put_is_idempotent(monkeypatch) -> None:
    class FakeS3:
        put_params: dict | None = None

        def put_object(self, **params):
            self.put_params = params
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )

        def head_object(self, **_params):
            return {"VersionId": "proof-v1", "ETag": '"etag"'}

    client = FakeS3()
    monkeypatch.setattr(
        payment_authorization,
        "get_settings",
        lambda: SimpleNamespace(
            s3_bucket="private-bucket",
            buckets_kms_key_id=None,
        ),
    )
    monkeypatch.setattr(payment_authorization, "_private_s3_client", lambda: client)

    result = payment_authorization.put_private_s3_object(
        key="proof/hash.pdf",
        body=b"proof",
        content_type="application/pdf",
        prevent_overwrite=True,
    )

    assert client.put_params is not None
    assert client.put_params["IfNoneMatch"] == "*"
    assert result == {"created": False, "version_id": "proof-v1", "etag": '"etag"'}


def test_notice_audit_schema_and_payment_summary_contract() -> None:
    now = datetime.now(UTC)
    notice = PaymentDebitNoticeRead(
        id="00000000-0000-0000-0000-000000000001",
        application_profile_id="00000000-0000-0000-0000-000000000002",
        fee_obligation_id="00000000-0000-0000-0000-000000000003",
        mandate_id=None,
        transfer_id=None,
        installment_id=None,
        message_send_id=None,
        status="sent",
        delivery_status="provider_accepted",
        notice_type="one_time_fee",
        amount_cents=10_000,
        currency="usd",
        recipient_name="Ada Borrower",
        recipient_email="ada@example.com",
        account_mask="1234",
        scheduled_debit_at=now,
        debit_window_start_at=now,
        debit_window_end_at=now,
        notice_business_days=2,
        revocation_cutoff_at=now,
        authorization_text_sha256=None,
        notice_sha256="a" * 64,
        idempotency_key="notice-1",
        sent_at=now,
        provider_accepted_at=now,
        delivered_at=None,
        bounced_at=None,
        failed_at=None,
        superseded_at=None,
        revoked_at=None,
        created_at=now,
        updated_at=now,
        provider="ses",
        provider_message_id="provider-1",
        rfc_message_id="<notice-1@example.com>",
        delivery_evidence_snapshot={"accepted": True},
    )
    summary = PaymentSummary(
        profile_id="00000000-0000-0000-0000-000000000002",
        debit_notice=notice,
        server_now=now,
    )

    assert summary.debit_notice is notice
    assert summary.debit_notice.delivery_evidence_snapshot == {"accepted": True}


def test_debit_window_is_exact_and_weekends_fail_closed() -> None:
    scheduled, start, end, cutoff = ach_fee_workflow.debit_window(date(2026, 10, 7))

    assert scheduled.astimezone(ach_fee_workflow.FIRM_TIMEZONE).hour == 12
    assert start.astimezone(ach_fee_workflow.FIRM_TIMEZONE).date() == date(2026, 10, 7)
    assert end.astimezone(ach_fee_workflow.FIRM_TIMEZONE).date() == date(2026, 10, 7)
    assert cutoff.astimezone(ach_fee_workflow.FIRM_TIMEZONE).date() == date(2026, 10, 6)
    assert cutoff.astimezone(ach_fee_workflow.FIRM_TIMEZONE).hour == 17

    with pytest.raises(HTTPException) as exc:
        ach_fee_workflow.debit_window(date(2026, 10, 10))
    assert exc.value.status_code == 422


def test_authorization_terms_are_bound_to_exact_hash_and_date() -> None:
    exact_text = "One-time business CCD authorization"
    scheduled = datetime(2026, 10, 7, 16, tzinfo=UTC)
    digest = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
    notice = SimpleNamespace(scheduled_debit_at=scheduled)

    assert (
        ach_fee_workflow.require_authorization_terms_binding(
            submitted_sha256=digest,
            submitted_scheduled_debit_at=scheduled,
            exact_text=exact_text,
            notice=notice,
        )
        == digest
    )
    with pytest.raises(HTTPException) as exc:
        ach_fee_workflow.require_authorization_terms_binding(
            submitted_sha256="0" * 64,
            submitted_scheduled_debit_at=scheduled,
            exact_text=exact_text,
            notice=notice,
        )
    assert exc.value.status_code == 409


def test_production_ach_requires_explicit_legal_approval(monkeypatch) -> None:
    monkeypatch.setattr(
        ach_fee_workflow,
        "get_settings",
        lambda: SimpleNamespace(payments_ach_legal_approved=False),
    )
    from app.services import plaid_transfer

    monkeypatch.setattr(plaid_transfer, "environment", lambda: "production")

    assert ach_fee_workflow.legal_approval_required() is True
    with pytest.raises(HTTPException) as exc:
        ach_fee_workflow.require_legal_approval()
    assert exc.value.status_code == 503
