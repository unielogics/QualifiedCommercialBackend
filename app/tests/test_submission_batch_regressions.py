"""Delivery uncertainty, recipient policy isolation and approved byte snapshots."""
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services import submission_batches as service
from app.services.email.ses_client import SesSendResult


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *args):
        return False


def delivery_fixture(*, account_status="active", deleted_at=None):
    row = NS(id=uuid4(), batch_id=uuid4(), lender_id=uuid4(), offering_id=None,
        status="queued", manifest={"files": []}, lender_name="Institution One",
        recipient_email="review@institution.example", encrypted_token="encrypted",
        expires_at=datetime.now(UTC) + timedelta(days=7), attempt_count=0,
        provider_message_id=None, last_error=None, revoked_at=None)
    approver = NS(id=uuid4(), role=Role.LOAN_EXEC, account_status=account_status, deleted_at=deleted_at)
    batch = NS(id=row.batch_id, profile_id=uuid4(), approved_by_user_id=approver.id)
    profile = NS(id=batch.profile_id)
    lender = NS(id=row.lender_id, is_active=True, submission_email=row.recipient_email)
    models = {service.SubmissionBatch: batch, service.ApplicationProfile: profile, service.User: approver, service.Lender: lender}
    db = NS(scalars=AsyncMock(return_value=Rows([row])), get=AsyncMock(side_effect=lambda model, key: models[model]),
        commit=AsyncMock(), add=Mock())
    return db, row, profile


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_detail", "expected"), [
    ("gmail_send_failed: read timeout after send", "delivery_unknown"),
    ("send_failed: endpoint closed connection", "delivery_unknown"),
    ("not_configured", "failed"),
])
async def test_provider_uncertainty_never_enables_automatic_resend(provider_detail, expected):
    db, row, profile = delivery_fixture()
    sender = AsyncMock(return_value=SesSendResult(False, None, provider_detail))
    with (
        patch("app.db.SessionLocal", return_value=SessionContext(db)),
        patch("app.services.email.user_mailer.send_as_user", sender),
        patch.object(service.application_profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(service, "assert_current", AsyncMock(return_value={"files": {}})),
        patch.object(service, "package_material", AsyncMock(return_value={"blockers": []})),
        patch.object(service, "_decrypt_fernet", return_value="recipient-only-token"),
    ):
        assert await service.deliver_one(row.id) == expected
        assert await service.deliver_one(row.id) == "skipped"
    sender.assert_awaited_once()
    assert row.attempt_count == 1
    assert sender.await_args.kwargs["to_emails"] == [row.recipient_email]
    assert "cc_emails" not in sender.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(("account_status", "deleted_at"), [
    ("suspended", None), ("active", datetime(2026, 1, 1, tzinfo=UTC)),
])
async def test_queued_package_does_not_send_for_disabled_approver(account_status, deleted_at):
    db, row, profile = delivery_fixture(account_status=account_status, deleted_at=deleted_at)
    sender = AsyncMock(return_value=SesSendResult(True, "message-id", "sent"))
    with (
        patch("app.db.SessionLocal", return_value=SessionContext(db)),
        patch("app.services.email.user_mailer.send_as_user", sender),
        patch.object(service.application_profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(service, "assert_current", AsyncMock(return_value={"files": {}})),
        patch.object(service, "package_material", AsyncMock(return_value={"blockers": []})),
        patch.object(service, "_decrypt_fernet", return_value="token"),
    ):
        assert await service.deliver_one(row.id) == "failed"
    sender.assert_not_awaited()
    assert row.attempt_count == 0


def test_package_download_rechecks_actual_bytes_against_approved_hash():
    file = NS(s3_key="official/lender-application.pdf")
    client = NS(get_object=Mock(return_value={"Body": BytesIO(b"changed after approval")}))
    with (
        patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)),
        patch("app.routers.buckets._s3_client", return_value=client),
        pytest.raises(HTTPException) as caught,
    ):
        service._read_file(file, {"content_hash": service.hashlib.sha256(b"approved bytes").hexdigest(), "s3_version_id": "version-approved"})
    assert caught.value.status_code == 409
    assert client.get_object.call_args.kwargs["VersionId"] == "version-approved"


@pytest.mark.asyncio
async def test_lender_requirements_keep_independent_policy_rows_with_shared_collection_identity():
    lenders = [NS(id=uuid4(), name=name) for name in ("Institution A", "Institution B")]
    offerings = [NS(id=uuid4(), version=1, checklist_confirmed=True, requirements=[{
        "requirement_key": "business_bank_history", "requirement_kind": "document",
        "label": "Business bank history", "collection_config": {"required_months": count},
    }]) for count in (3, 6)]
    materialize = AsyncMock()
    with patch("app.services.collection_tasks.materialize_external_requirements", materialize):
        for lender, offering in zip(lenders, offerings, strict=True):
            await service.materialize_lender_requirements(NS(), NS(), offering, lender)
    definition_a = materialize.await_args_list[1].kwargs["requirements"][0]
    definition_b = materialize.await_args_list[3].kwargs["requirements"][0]
    assert definition_a["requirement_key"] != definition_b["requirement_key"]
    assert definition_a["canonical_key"] == definition_b["canonical_key"] == "business_bank_history"
    assert definition_a["collection_config"]["required_months"] == 3
    assert definition_b["collection_config"]["required_months"] == 6


def test_digest_normalizes_sets_as_sorted_values_across_worker_processes():
    # Evidence availability is a set in profile_fit_context. Its repr ordering
    # varies across interpreter hash seeds; compare its stable JSON equivalent.
    assert service.digest({"evidence_available": {"tax_return", "bank_statement"}}) == service.digest({"evidence_available": ["bank_statement", "tax_return"]})


def test_collection_reminder_activity_does_not_make_approved_documents_stale():
    state = NS(requirement_key="bank_history", status="verified", evidence_file_id=None,
        provenance={"source_kind": "lender", "collection_status": "requested", "collection_assignee": "borrower", "collection_attempts": 0,
                    "collection_config": {"required_months": 6}, "accepted_file_ids": ["file-1"]})
    source = {"context": {}, "states": {state.requirement_key: state}, "files": {}, "readiness": NS(selections=[])}
    profile = NS(id=uuid4())
    before = service.source_fingerprint(profile, source, None)
    state.provenance = {**state.provenance, "collection_status": "in_review", "collection_assignee": "internal", "collection_attempts": 1}
    assert service.source_fingerprint(profile, source, None) == before
