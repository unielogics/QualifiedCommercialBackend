from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import fitz
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.schemas.submission_batch import LenderOfferingCreate, SubmissionBatchCreate
from app.services import submission_batches as service


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


def source():
    return {"readiness": NS(selections=[], programs=[], requirements=[]), "files": {}, "analyses": {}, "context": {"annual_revenue": 400000}, "states": {}}


def offering(**values):
    return NS(id=uuid4(), lender_id=uuid4(), program_key="*", version=1, status="active", checklist_confirmed=True,
              requirements=values.pop("requirements", []), fit_rules=values.pop("fit_rules", {}),
              official_form_file_ids=[], official_forms_required=values.pop("official_forms_required", False), **values)


def recipient(**values):
    return NS(id=uuid4(), name="Institution One", is_active=True, submission_email="review@institution.example", **values)


def profile():
    return NS(id=uuid4())


def child(**overrides):
    data = dict(id=uuid4(), status="prepared", revoked_at=None, expires_at=datetime.now(UTC) + timedelta(days=2),
                lender_id=uuid4(), offering_id=uuid4(), source_fingerprint="current", manifest={"files": []},
                fit_status="recommended", warning_acknowledged=False, blockers=[], queued_at=None, last_error=None)
    return NS(**(data | overrides))


def test_underwriting_approval_is_server_enforced():
    for role in (Role.CLIENT, Role.BROKER, Role.FIELD_REP, Role.LENDER):
        with pytest.raises(HTTPException) as error:
            service.require_underwriter(NS(role=role))
        assert error.value.status_code == 403
    service.require_underwriter(NS(role=Role.SUPER_ADMIN))
    service.require_underwriter(NS(role=Role.LOAN_EXEC))


def test_nonmatching_and_unknown_lenders_remain_advisory():
    assert service.fit_for_offering(None, {}, set())["status"] == "lender_clarification_required"
    configured = offering(fit_rules={"fit": {"field": "annual_revenue", "op": "gte", "value": 300000}})
    assert service.fit_for_offering(configured, {"annual_revenue": 400000}, set())["status"] == "recommended"
    assert service.fit_for_offering(configured, {"annual_revenue": 100000}, set())["status"] == "outside_published_criteria"
    assert service.fit_for_offering(configured, {}, set())["status"] == "needs_information"


def test_same_lender_cannot_be_repeated_and_confirmed_checklist_needs_source():
    lender_id = uuid4()
    with pytest.raises(ValidationError):
        SubmissionBatchCreate(idempotency_key="request-123", targets=[{"lender_id": lender_id}, {"lender_id": lender_id}])
    with pytest.raises(ValidationError):
        LenderOfferingCreate(checklist_confirmed=True)


def test_sharing_authorizations_are_isolated_per_lender():
    one, two = service.sharing_requirement(uuid4()), service.sharing_requirement(uuid4())
    assert one["canonical_key"] != two["canonical_key"]
    assert one["collection_config"]["scope"] != two["collection_config"]["scope"]


def test_snapshot_changes_when_document_or_offering_changes():
    state = source()
    item = offering()
    before = service.source_fingerprint(profile(), state, item)
    file_id = uuid4()
    state["files"][str(file_id)] = NS(id=file_id, content_hash="abc", s3_version_id="v1", updated_at=None)
    after = service.source_fingerprint(profile(), state, item)
    assert before != after
    assert after != service.source_fingerprint(profile(), state, offering())


@pytest.mark.asyncio
async def test_tailored_package_includes_only_this_offerings_evidence():
    first, other = uuid4(), uuid4()
    files = {str(key): NS(id=key, file_name=f"{key}.pdf", content_type="application/pdf", size_bytes=20, status="uploaded") for key in (first, other)}
    state = source()
    institution = recipient()
    bank_key = service.lender_requirement_key(institution.id, "bank")
    state["files"] = files
    state["readiness"].requirements = [NS(requirement_key=bank_key, evidence_files=[NS(file_id=first, verified=True, ai_decision="accepted")]), NS(requirement_key="tax", evidence_files=[NS(file_id=other, verified=True, ai_decision="accepted")])]
    state["states"] = {bank_key: NS(requirement_key=bank_key, status="verified", evidence_file_id=first, provenance={}), "tax": NS(requirement_key="tax", status="verified", evidence_file_id=other, provenance={})}
    configured = offering(requirements=[{"requirement_key": "bank", "label": "Bank statements"}])
    with patch.object(service, "sharing_is_authorized", AsyncMock(return_value=True)):
        result = await service.package_material(NS(), profile(), institution, configured, state, [])
    assert [item["id"] for item in result["manifest"]["files"]] == [str(first)]
    assert "context" not in result["manifest"]


@pytest.mark.asyncio
async def test_consent_and_missing_official_forms_are_hard_blockers():
    with patch.object(service, "sharing_is_authorized", AsyncMock(return_value=False)):
        result = await service.package_material(NS(), profile(), recipient(), offering(official_forms_required=True), source(), [])
    assert {b["code"] for b in result["blockers"]} >= {"sharing_consent_required", "official_form_required"}


@pytest.mark.asyncio
async def test_nonwaivable_lender_requirement_cannot_be_bypassed():
    state = source()
    institution = recipient()
    key = service.lender_requirement_key(institution.id, "application")
    state["states"][key] = NS(requirement_key=key, status="waived", evidence_file_id=None, provenance={})
    configured = offering(requirements=[{"requirement_key": "application", "label": "Lender application", "waivable": False}])
    with patch.object(service, "sharing_is_authorized", AsyncMock(return_value=True)):
        result = await service.package_material(NS(), profile(), institution, configured, state, [])
    assert any(b["code"] == "missing_lender_requirement" for b in result["blockers"])


@pytest.mark.asyncio
async def test_cross_application_extra_document_is_rejected():
    with patch.object(service, "sharing_is_authorized", AsyncMock(return_value=True)), pytest.raises(HTTPException) as error:
        await service.package_material(NS(), profile(), recipient(), offering(), source(), [uuid4()])
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_approval_queues_ready_children_only_and_never_resends_successes():
    ready, blocked, sent = child(), child(), child(status="sent")
    db = NS(execute=AsyncMock(), scalars=AsyncMock(return_value=Rows([ready, blocked, sent])), get=AsyncMock(), flush=AsyncMock(), add=Mock())
    batch = NS(id=uuid4(), approved_at=None, approved_by_user_id=None)
    material = {"blockers": [], "manifest": {}, "requirements": []}
    with (
        patch.object(service, "source_state", AsyncMock(return_value=source())),
        patch.object(service, "source_fingerprint", return_value="current"),
        patch.object(service, "package_material", AsyncMock(side_effect=[material, {"blockers": [{"code": "missing", "message": "Missing form"}]}])),
        patch.object(service, "read_batch", AsyncMock(return_value="result")),
    ):
        assert await service.approve_batch(db, profile(), batch, NS(id=uuid4(), role=Role.LOAN_EXEC)) == "result"
    assert ready.status == "queued"
    assert blocked.status == "prepared"
    assert sent.status == "sent"


@pytest.mark.asyncio
async def test_stale_package_and_unacknowledged_fit_do_not_queue():
    stale, warning = child(source_fingerprint="old"), child(fit_status="outside_published_criteria")
    db = NS(execute=AsyncMock(), scalars=AsyncMock(return_value=Rows([stale, warning])), get=AsyncMock(), flush=AsyncMock())
    with (
        patch.object(service, "source_state", AsyncMock(return_value=source())),
        patch.object(service, "source_fingerprint", return_value="current"),
        patch.object(service, "package_material", AsyncMock(return_value={"blockers": []})),
        patch.object(service, "read_batch", AsyncMock()),
    ):
        await service.approve_batch(db, profile(), NS(id=uuid4(), approved_at=None, approved_by_user_id=None), NS(id=uuid4(), role=Role.SUPER_ADMIN))
    assert stale.status == "package_stale"
    assert warning.status == "prepared"
    assert warning.blockers[0]["code"] == "fit_warning_acknowledgement"


@pytest.mark.asyncio
async def test_revoked_and_expired_recipient_cannot_open():
    for row in (child(revoked_at=datetime.now(UTC)), child(expires_at=datetime.now(UTC) - timedelta(seconds=1))):
        with pytest.raises(HTTPException) as error:
            await service.assert_current(NS(), row)
        assert error.value.status_code == 410


@pytest.mark.asyncio
async def test_prepare_idempotency_rejects_changed_request():
    payload = SubmissionBatchCreate(idempotency_key="request-1234", targets=[{"lender_id": uuid4()}])
    existing = NS(request_fingerprint=service.digest(payload.model_dump(mode="json")))
    db = NS(execute=AsyncMock(), scalars=AsyncMock(return_value=Rows([existing])))
    user = NS(id=uuid4(), role=Role.LOAN_EXEC)
    assert await service.prepare_batch(db, profile(), user, payload) is existing
    existing.request_fingerprint = "different"
    with pytest.raises(HTTPException) as error:
        await service.prepare_batch(db, profile(), user, payload)
    assert error.value.status_code == 409


def test_export_cover_marks_configuration_incomplete_without_private_notes():
    raw = service.render_cover({"lender": "Institution One", "summary": "Application review", "facts": {"requested_amount": 25000}, "files": []}, preview=True)
    doc = fitz.open(stream=raw, filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    assert "CONFIGURATION INCOMPLETE" in text
    assert "25000" in text
    doc.close()
