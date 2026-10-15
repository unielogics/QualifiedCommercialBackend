from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os.services import plaid_client, plaid_webhook
from app.routers import public_payments
from app.services import (
    application_plaid_sync,
    payment_processing,
    plaid_lifecycle,
    plaid_payment_lifecycle,
    plaid_transfer,
)


class Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return None


def rows_result(rows):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def report_row(**overrides):
    return SimpleNamespace(**{
        "id": uuid4(), "status": "ingested", "environment": "production",
        "encrypted_asset_report_token": "cipher", "provider_removal_requested_at": None,
        "provider_removed_at": None, "provider_removal_error": None,
        "removed_at": None, "document_id": uuid4(), "bucket_file_id": None,
        "dealer_id": uuid4(), "profile_id": None, "ingested_at": datetime.now(UTC),
        **overrides,
    })


@pytest.mark.parametrize("rationale,expected", [
    (None, True), ({"code": None, "description": None}, True),
    ({"code": "MANUALLY_VERIFIED_ITEM"}, False),
    ({"code": "ITEM_LOGIN_REQUIRED"}, False),
    ({"code": "MIGRATED_ACCOUNT_ITEM"}, False),
    ({"code": "NSF"}, False), ("unexpected", False),
])
def test_only_completed_risk_checks_allow_submission(rationale, expected):
    assert plaid_transfer.authorization_passed_risk_check({
        "id": "auth-1", "decision": "approved", "decision_rationale": rationale,
    }) is expected
    assert not plaid_transfer.authorization_passed_risk_check({
        "id": "auth-1", "decision": "declined", "decision_rationale": rationale,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("saved,lookup_error", [(False, False), (True, False), (True, True)])
async def test_unsafe_authorization_never_creates_even_during_crash_recovery(monkeypatch, saved, lookup_error):
    context = payment_processing._ProviderContext(
        transfer_id=uuid4(), amount_cents=100, ach_class="ccd", idempotency_key="key",
        attempt_no=1, account_id="account", access_token="access", legal_name="Example LLC",
        email=None, ip_address=None, user_agent=None, authorization_id="auth-1" if saved else None,
        fee_obligation_id=uuid4(), installment_id=None,
    )
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(payment_processing, "SessionLocal", lambda: Session(db))
    monkeypatch.setattr(payment_processing, "get_settings", lambda: SimpleNamespace(
        payments_enabled=True, private_funding_payments_enabled=False,
    ))
    monkeypatch.setattr(payment_processing.ach_fee_workflow, "legal_approval_required", lambda: False)
    monkeypatch.setattr(plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(payment_processing.payments, "claim_due_transfers", AsyncMock(return_value=[context]))
    monkeypatch.setattr(payment_processing, "_provider_context", AsyncMock(return_value=context))
    lookup = AsyncMock(return_value=None)
    if lookup_error:
        lookup.side_effect = plaid_transfer.PlaidTransferError("Temporary error", retryable=True)
    monkeypatch.setattr(plaid_transfer, "get_transfer_by_authorization", lookup)
    monkeypatch.setattr(plaid_transfer, "create_authorization", AsyncMock(return_value={
        "id": "auth-1", "decision": "approved", "decision_rationale": {"code": "MANUALLY_VERIFIED_ITEM"},
    }))
    record = AsyncMock()
    failure = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(payment_processing, "_record_authorization", record)
    monkeypatch.setattr(payment_processing, "_submission_failed", failure)
    monkeypatch.setattr(plaid_transfer, "create_transfer", create)

    assert await payment_processing.dispatch_payment_transfers() == 0
    create.assert_not_awaited()
    failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_report_removal_preserves_token_and_retries(monkeypatch):
    report = report_row()
    monkeypatch.setenv("DEALER_OS_PLAID_ENV", "production")
    monkeypatch.setattr(plaid_client, "enabled", lambda: True)
    monkeypatch.setattr(plaid_client, "decrypt_token", lambda token: "report-token" if token else None)
    remove = AsyncMock(side_effect=plaid_client.PlaidUnavailable("Temporary failure"))
    monkeypatch.setattr(plaid_client, "asset_report_remove", remove)
    await plaid_lifecycle.remove_asset_report(report, strict=False)
    assert report.status == "removed"
    assert report.encrypted_asset_report_token == "cipher"
    assert report.provider_removal_requested_at is not None
    assert report.provider_removed_at is None
    assert report.provider_removal_error

    db = SimpleNamespace(execute=AsyncMock(return_value=rows_result([report])), commit=AsyncMock())
    monkeypatch.setattr("app.db.SessionLocal", lambda: Session(db))
    remove.side_effect = None
    assert await plaid_lifecycle.cleanup_asset_reports() == 1
    assert report.encrypted_asset_report_token is None
    assert report.provider_removed_at is not None
    assert report.provider_removal_error is None


@pytest.mark.asyncio
async def test_ingested_cleanup_requires_archive_and_preserves_freshness(monkeypatch):
    report = report_row()
    plaid_lifecycle.queue_report_cleanup(report)
    db = SimpleNamespace(execute=AsyncMock(return_value=rows_result([report])), commit=AsyncMock())
    monkeypatch.setattr("app.db.SessionLocal", lambda: Session(db))
    monkeypatch.setenv("DEALER_OS_PLAID_ENV", "production")
    monkeypatch.setattr(plaid_client, "enabled", lambda: True)
    monkeypatch.setattr(plaid_client, "decrypt_token", lambda _: "report-token")
    archive = AsyncMock(return_value=None)
    remove = AsyncMock()
    monkeypatch.setattr(plaid_lifecycle, "retained_report_pdf", archive)
    monkeypatch.setattr(plaid_client, "asset_report_remove", remove)
    assert await plaid_lifecycle.cleanup_asset_reports() == 0
    remove.assert_not_awaited()
    assert report.encrypted_asset_report_token == "cipher"
    archive.return_value = b"%PDF-archived"
    assert await plaid_lifecycle.cleanup_asset_reports() == 1
    assert report.status == "ingested"
    assert report.removed_at is None
    assert report.encrypted_asset_report_token is None


@pytest.mark.asyncio
async def test_report_download_works_after_remote_cleanup(monkeypatch):
    report = report_row(encrypted_asset_report_token=None, provider_removed_at=datetime.now(UTC))
    monkeypatch.setattr(plaid_lifecycle, "retained_report_pdf", AsyncMock(return_value=b"%PDF-archive"))
    remote = AsyncMock()
    monkeypatch.setattr(plaid_client, "asset_report_pdf", remote)
    assert await plaid_lifecycle.report_pdf(object(), report) == b"%PDF-archive"
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_removed_report_cannot_be_resurrected_by_late_webhook():
    report = report_row(status="removed", removed_at=datetime.now(UTC), ingested_at=None)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: report)))
    for code in ("PRODUCT_READY", "ERROR"):
        assert await plaid_webhook.handle(db, {
            "webhook_type": "ASSETS", "webhook_code": code,
            "asset_report_id": "report-1", "environment": "production",
        }) == "ignored: asset report removed"
    assert report.status == "removed"


@pytest.mark.asyncio
async def test_cleanup_never_discards_another_environment_token(monkeypatch):
    report = report_row(environment="sandbox")
    monkeypatch.setenv("DEALER_OS_PLAID_ENV", "production")
    remote = AsyncMock()
    monkeypatch.setattr(plaid_client, "asset_report_remove", remote)
    await plaid_lifecycle.remove_asset_report(report, strict=False)
    assert report.encrypted_asset_report_token == "cipher"
    remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_revocation_stops_mandates_and_unclaimed_intents(monkeypatch):
    source = SimpleNamespace(id=uuid4(), status="verified", revoked_at=None, access_token_ciphertext="cipher")
    mandate = SimpleNamespace(status="active", revoked_at=None, terminated_at=None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[rows_result([source]), rows_result([mandate])]))
    cancel = AsyncMock()
    retain = AsyncMock()
    monkeypatch.setattr(plaid_payment_lifecycle.payments, "cancel_unclaimed_transfer_intents", cancel)
    monkeypatch.setattr(plaid_payment_lifecycle.ach_fee_workflow, "extend_mandate_retention", retain)
    assert await plaid_payment_lifecycle.handle_item_webhook(db, {
        "webhook_code": "USER_PERMISSION_REVOKED", "item_id": "item-1", "environment": "production",
    }) == "payment sources revoked"
    assert source.status == "revoked"
    assert source.access_token_ciphertext is None
    assert mandate.status == "revoked"
    cancel.assert_awaited_once()
    retain.assert_awaited_once()
    query = str(db.execute.await_args_list[0].args[0])
    assert "plaid_environment" in query


@pytest.mark.asyncio
async def test_payment_replay_cannot_cross_environment():
    source = SimpleNamespace(plaid_environment="production" if plaid_transfer.environment() == "sandbox" else "sandbox")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: source)))
    with pytest.raises(HTTPException) as exc:
        await public_payments._exchange_or_recover_payment_source(
            db, profile=SimpleNamespace(id=uuid4()), link=SimpleNamespace(id=uuid4()),
            payload=public_payments.RoomPaymentExchange(
                passcode="123456", owner_type="business", purpose="fee",
                business_account_attested=True, public_token="public-once", plaid_account_id="account-1",
            ),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_fails", [False, True])
async def test_abandoned_payment_source_expires_and_cleanup_retries(monkeypatch, remove_fails):
    source = SimpleNamespace(
        id=uuid4(), status="pending", revoked_at=None,
        access_token_ciphertext="cipher", metadata_json={},
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=rows_result([source])), commit=AsyncMock(),
        get=AsyncMock(return_value=source),
    )
    monkeypatch.setattr(plaid_payment_lifecycle, "SessionLocal", lambda: Session(db))
    monkeypatch.setattr(plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(plaid_transfer, "decrypt_access_token", lambda _: "access-token")

    async def remove(_token):
        assert source.status == "expired"
        assert db.commit.await_count >= 1
        if remove_fails:
            raise plaid_transfer.PlaidTransferError("Temporary failure", code="TEMPORARY")

    monkeypatch.setattr(plaid_transfer, "remove_item", remove)
    assert await plaid_payment_lifecycle.cleanup_payment_items() == (0 if remove_fails else 1)
    assert source.revoked_at is not None
    assert source.access_token_ciphertext == ("cipher" if remove_fails else None)
    assert source.metadata_json["plaid_cleanup_error"] == ("TEMPORARY" if remove_fails else None)
    query = str(db.execute.await_args.args[0])
    assert "plaid_environment" in query
    assert "created_at <=" in query


@pytest.mark.asyncio
async def test_revoked_application_item_never_pulls_or_changes_state():
    item = SimpleNamespace(status="revoked")
    db = SimpleNamespace(get=AsyncMock())
    assert await application_plaid_sync.sync_item(db, item) == {"pulled": 0, "skipped": 0, "failed": 0}
    db.get.assert_not_awaited()
    assert item.status == "revoked"
