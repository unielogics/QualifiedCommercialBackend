from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.bucket import BucketFile
from app.models.file_event import FileEvent
from app.schemas.earnings_improvement import empty_body, validate_body
from app.services import earnings_improvement as math
from app.services import financial_workspace as ws
from app.services import operating_plan_evidence as evidence
from app.services import operating_plan_service as service
from app.tests.test_financial_workspace import Memory, Result, context


def proposal():
    body = {**empty_body(), "business_name": "Test LLC", "period_start": "2025-01-01", "period_end": "2025-12-31",
        "currency": "USD", "basis": "cash", "projection_start": "2027-01-01"}
    expense, action = str(uuid4()), str(uuid4())
    body["expenses"] = [{"id": expense, "label": "Rent", "category": "rent", "current_amount": 2000}]
    body["adjustments"] = [{"id": action, "label": "New lease", "driver_key": "lease", "expense_row_id": expense,
        "current_amount": 2000, "proposed_amount": 1500, "effective_date": "2027-01-01", "explanation": "Signed new lease",
        "source_note": "New lease terms", "implementation_cost": 0}]
    return validate_body(body)


def approved():
    body = proposal()
    body["adjustments"][0].update(status="approved", reviewed_by_user_id=str(uuid4()), reviewed_at=datetime.now(UTC).isoformat(),
        review_reason="Private review reason", review_source_stamp="a" * 64, baseline_treatment="included_in_baseline", baseline_reconciliation_note="Private note")
    return body


def cockpit():
    return {"ebitda": {"headline": {"annualized_value": -10000, "value": -10000, "currency": "USD", "basis": "cash", "period": "2025", "provisional": True}},
        "noi": {}, "debt": {"service": {"annual": 20000, "currency": "USD"}}, "plan": {}, "operating_evidence": {"files": []}}


def test_ordinary_writers_cannot_forge_or_change_staff_approval():
    old = proposal()
    incoming = deepcopy(old)
    incoming["adjustments"][0].update(status="approved", reviewed_by_user_id=str(uuid4()), review_source_stamp="a"*64,
        baseline_treatment="incremental_to_baseline", baseline_reconciliation_note="Forged")
    clean = service.protect_reviews(incoming, old)["adjustments"][0]
    assert clean["status"] == "proposed" and clean["review_source_stamp"] is None
    assert clean["baseline_treatment"] == "unreconciled"


def test_unchanged_review_retained_but_expense_and_scope_changes_clear_it():
    old = approved()
    assert service.protect_reviews(deepcopy(old), old) == validate_body(old)
    for block, key, value in (("expenses", "current_amount", 3000), ("adjustments", "proposed_amount", 1000)):
        changed = deepcopy(old)
        changed[block][0][key] = value
        assert service.protect_reviews(changed, old)["adjustments"][0]["status"] == "proposed"
    changed = {**old, "period_end": "2026-12-31"}
    assert service.protect_reviews(changed, old)["adjustments"][0]["status"] == "proposed"


def test_removing_survivor_preserves_excluded_rows_and_resolves_duplicate_reference():
    old = proposal()
    duplicate = {**deepcopy(old["expenses"][0]), "id": str(uuid4()), "excluded": True, "duplicate_of": old["expenses"][0]["id"]}
    old["expenses"].append(duplicate)
    incoming = deepcopy(old)
    incoming["expenses"] = [duplicate]
    result = service.protect_reviews(incoming, old)
    assert len(result["expenses"]) == 2
    assert all(row["excluded"] and row["duplicate_of"] is None for row in result["expenses"])


@pytest.mark.asyncio
async def test_cross_file_evidence_rejected_and_old_history_can_be_retained():
    body = proposal()
    body["expenses"][0]["source_file_ids"] = [str(uuid4())]
    with patch.object(ws, "source_files_for_profile", AsyncMock(return_value=[])), patch.object(ws, "filter_current_files", AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as error:
            await service.prepare_draft(Memory(), SimpleNamespace(), incoming=body, current=empty_body())
        assert error.value.status_code == 422
        assert await service.prepare_draft(Memory(), SimpleNamespace(), incoming=body, current=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [True, False])
async def test_publication_creates_immutable_proposal_pdf_without_actual_projections(client):
    db = Memory()
    profile, family, draft = context("operating_plan")
    draft.body = proposal()
    actor = SimpleNamespace(id=None if client else uuid4(), name="Client" if client else "Desk", role=Role.CLIENT if client else Role.LOAN_EXEC)
    slot = SimpleNamespace(id=uuid4(), status="requested")
    async def project(*args, **kwargs):
        assert args[2].kind == "operating_plan"
        # The registered projection is explicitly a no-op for this form.
        assert not any(isinstance(row, BucketFile) for row in db.added if getattr(row, "source_kind", None) != "drafted_form")
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), \
         patch.object(ws, "_render", return_value=b"%PDF immutable") as render, patch.object(ws, "_write_pdf") as storage, \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)), \
         patch("app.services.financial_publication.after_publication", AsyncMock()), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit())), patch.object(ws, "_project", project):
        result = await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="plan-save-1", actor=actor, submit=client)
        replay = await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, idempotency_key="plan-save-1", actor=actor, submit=client)
    assert replay["saved_version"]["id"] == result["saved_version"]["id"]
    assert db.versions[0].status == ("client_submitted" if client else "published")
    assert storage.call_count == 1
    assert render.call_args.args[2]["improvement_plan"]["baseline"]["earnings"] == -10000
    assert "annual_ebitda" not in db.versions[0].key_facts
    assert family.current_version_id == draft.base_version_id
    assert slot.status == ("uploaded" if client else "requested")


@pytest.mark.asyncio
async def test_failed_operating_pdf_keeps_prior_pointer_and_draft():
    db = Memory()
    profile, family, draft = context("operating_plan")
    draft.body = proposal()
    prior = uuid4()
    family.current_version_id = draft.base_version_id = prior
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit())), patch.object(ws, "_render", side_effect=RuntimeError("render failed")):
        with pytest.raises(HTTPException) as error:
            await ws.publish_version(db, profile, kind="operating_plan", expected_revision=4, base_version_id=prior, idempotency_key="retry-pdf")
    assert error.value.status_code == 502
    assert family.current_version_id == draft.base_version_id == prior and not db.versions


class ReviewMemory(Memory):
    async def execute(self, statement):
        if "file_events" in str(statement):
            return Result([row for row in self.added if isinstance(row, FileEvent)])
        return await super().execute(statement)


@pytest.mark.asyncio
async def test_review_revision_context_and_durable_retry_receipt():
    db = ReviewMemory()
    profile, family, draft = context("operating_plan")
    draft.body = proposal()
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    current = cockpit()
    kwargs = dict(family_id=family.id, expected_revision=4, action_ids=[draft.body["adjustments"][0]["id"]], decision="rejected",
        reason="Supplier quote missing", context_stamp=math.context_stamp(current), idempotency_key="review-once")
    async def stage(_db, _profile, _family, _draft, body, actor=None):
        assert db.info["operating_plan_review_write"] is True
        draft.body = body
        draft.revision += 1
    with patch.object(ws, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(ws, "load_workspace", AsyncMock(return_value={})), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), patch.object(ws, "stage_draft", stage):
        first = await service.review(db, profile, actor, **kwargs)
        second = await service.review(db, profile, actor, **kwargs)
        with pytest.raises(HTTPException) as error:
            await service.review(db, profile, actor, **{**kwargs, "reason": "Changed reason"})
    assert error.value.status_code == 409 and not first["replayed"] and second["replayed"]
    assert draft.revision == 5 and len(db.added) == 1 and db.added[0].notice_status == "none"
    assert "operating_plan_review_write" not in db.info


def test_evidence_uses_latest_account_period_excludes_debt_and_never_infers_card_volume():
    source_id, old_id = str(uuid4()), str(uuid4())
    def row(fid, end):
        return {"file_id": fid, "file_name": "Processor.pdf", "classification": "merchant_processing_statement", "accepted": True,
            "content_hash": "a"*64, "analysis_id": str(uuid4()), "identity_association": {"status": "matched", "allows_financial_use": True},
            "key_facts": {"merchant_processing": {"processor": "Processor", "merchant_account_last4": "1234", "currency": "USD",
                "period_end": end, "total_processing_fees": 300, "fees_complete": True}}}
    result = evidence.build_evidence([row(old_id, "2026-09-30"), row(source_id, "2026-10-31")], {"bank": {"recurring": {"payees": [
        {"key": "loan", "is_debt": True, "monthly_average": 3000}, {"key": "rent", "payee": "Landlord", "monthly_average": 2000}]}}})
    assert len(result["merchant_sources"]) == 1
    source = result["merchant_sources"][0]
    assert source["file_id"] == source_id and source["volume"] is None and source["effective_rate_pct"] is None
    assert not any(row["key"] == "bank:loan" for row in result["expense_candidates"])
    assert any(row["key"] == "bank:rent" for row in result["expense_candidates"])


def test_operating_pdf_is_not_a_supporting_source_and_package_omits_review_secrets():
    result = evidence.build_evidence([{"file_id": str(uuid4()), "classification": "operating_plan"}], {})
    assert result["files"] == []
    summary = service.shareable_package(math.evaluate(approved(), cockpit()), result)
    serialized = str(summary)
    assert "Private review reason" not in serialized and "reviewed_by_user_id" not in serialized
    from app.services.public_underwriting_packet_pdf import _operating_earnings_lines
    lines = _operating_earnings_lines({"operating_earnings": {"improvement_plan": summary}})
    assert any("New lease" in line for line in lines)
    assert any("Signed new lease" in line for line in lines)
    assert any("current amount: 2000" in line and "proposed amount: 1500" in line for line in lines)


def test_external_cockpit_uses_shareable_plan_and_hides_staff_picker_context():
    from app.services.financial_metric_review import redact_for_external

    current = cockpit()
    current["operating_evidence"] = {"files": [{"file_id": "private-id", "content_hash": "private-hash"}]}
    current["operating_plan"] = {"available": True, "state": "published", "version_number": 1,
        "evaluation": {"context_stamp": "private-context"},
        "package": {"adjustments": [{"label": "Renew lease", "review_reason": "Private reason", "reviewed_by_user_id": "private-actor"}]}}
    result = redact_for_external(current, SimpleNamespace(role=Role.CLIENT))
    assert result["operating_evidence"] == {}
    assert "private" not in str(result["operating_plan"]).lower()
    assert result["operating_plan"]["package"]["adjustments"][0]["label"] == "Renew lease"
