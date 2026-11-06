from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.models.funding_request import FundingRequestChange
from app.schemas.funding_request import FundingRequestPatch
from app.schemas.use_of_funds import UseOfFundsItem, UseOfFundsPatch
from app.services import funding_requests, use_of_funds


def profile(**values):
    return SimpleNamespace(**{
        "id": uuid4(), "intake_id": None, "dealer_id": None, "loan_id": None,
        "deal_id": None, "primary_bucket_id": None, "is_draft": False,
        "current_funding_request": {}, "use_of_funds_revision": 2,
        "use_of_funds": [], "underwriting_approved_amount": 65000,
        "underwriting_accepted_amount": 60000, **values,
    })


def payload(**values):
    return FundingRequestPatch(**{
        "requested_amount": "200000.00", "expected_revision": 0,
        "expected_use_of_funds_revision": 2, "idempotency_key": uuid4(),
        "reason": "Reviewed client request for eligible debt and working capital",
        "items": [
            {"id": "payoff", "category": "debt_refinance", "label": "Reviewed payoff budget", "amount": "150000.00"},
            {"id": "capital", "category": "working_capital", "label": "Remaining working capital", "amount": "50000.00"},
        ],
        "payoff_only_alternative": True, **values,
    })


async def source(_db, saved, **_kwargs):
    current = funding_requests.current_for_profile(saved)
    return current or Decimal("70000"), "profile.current_funding_request" if current else "intake.requested_loan_amount", "refinance", None


def db_for(saved, receipt=None):
    return SimpleNamespace(
        execute=AsyncMock(side_effect=[SimpleNamespace(scalar_one=lambda: saved), SimpleNamespace(scalar_one_or_none=lambda: receipt)]),
        add=Mock(), flush=AsyncMock(),
    )


@pytest.mark.parametrize("role", set(Role) - funding_requests.EDIT_ROLES)
def test_current_request_restricts_amount_editors(role):
    with pytest.raises(HTTPException) as error:
        funding_requests.require_editor(SimpleNamespace(role=role))
    assert error.value.status_code == 403


@pytest.mark.parametrize("value", [True, 0, -1, "1.001", "NaN", "Infinity"])
def test_request_money_rejects_invalid_or_fractional_cents(value):
    with pytest.raises(ValidationError):
        payload(requested_amount=value)


@pytest.mark.asyncio
async def test_correction_is_atomic_retains_original_and_does_not_resize_approved_terms():
    saved = profile()
    db = db_for(saved)
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    audit = AsyncMock()
    with patch.object(use_of_funds, "source_funding_data", source), patch("app.services.application_profiles.log_profile_action", audit):
        result = await funding_requests.update_request(db, saved, payload(), user)
    assert result.requested_amount == 200000
    assert result.original_requested_amount == 70000
    assert result.complete and result.payoff_only_alternative
    assert result.revision == 1 and result.use_of_funds_revision == 3
    assert saved.underwriting_approved_amount == 65000
    assert saved.underwriting_accepted_amount == 60000
    receipt = db.add.call_args.args[0]
    assert isinstance(receipt, FundingRequestChange)
    assert receipt.snapshot["original"]["requested_amount"] == "70000"
    assert receipt.snapshot["items"] == saved.use_of_funds
    assert audit.call_args.kwargs["metadata"]["reason"] == result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["199999.99", "200000.01"])
async def test_correction_rejects_unbalanced_budget_before_any_write(amount):
    saved = profile()
    db = db_for(saved)
    with pytest.raises(HTTPException) as error:
        await funding_requests.update_request(db, saved, payload(requested_amount=amount), SimpleNamespace(role=Role.SUPER_ADMIN))
    assert error.value.status_code == 422
    db.execute.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"expected_revision": 1}, {"expected_use_of_funds_revision": 1}])
async def test_stale_request_or_budget_revision_does_not_mutate(changes):
    saved = profile()
    db = db_for(saved)
    with pytest.raises(HTTPException) as error:
        await funding_requests.update_request(db, saved, payload(**changes), SimpleNamespace(role=Role.LOAN_EXEC))
    assert error.value.status_code == 409
    assert saved.current_funding_request == {} and saved.use_of_funds_revision == 2
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_durable_retry_returns_current_request_without_duplicate_history_or_revision():
    request = payload()
    saved = profile(current_funding_request={"requested_amount": "200000", "revision": 1}, use_of_funds=request.items)
    # Persisted budget is JSON, not schema objects.
    saved.use_of_funds = [item.model_dump(mode="json") for item in request.items]
    receipt = SimpleNamespace(payload_hash=funding_requests.payload_hash(request))
    db = db_for(saved, receipt)
    with patch.object(use_of_funds, "source_funding_data", source):
        result = await funding_requests.update_request(db, saved, request, SimpleNamespace(role=Role.LOAN_EXEC))
    assert result.revision == 1 and saved.use_of_funds_revision == 2
    db.add.assert_not_called()
    db.flush.assert_not_called()
    changed = request.model_copy(update={"reason": "A different reviewed request"})
    with pytest.raises(HTTPException) as error:
        await funding_requests.update_request(db_for(saved, receipt), saved, changed, SimpleNamespace(role=Role.LOAN_EXEC))
    assert error.value.status_code == 409


def test_idempotency_hash_normalizes_equivalent_money_but_not_intent():
    original = payload()
    equivalent = original.model_copy(update={"requested_amount": Decimal("200000"), "expected_revision": 9})
    assert funding_requests.payload_hash(original) == funding_requests.payload_hash(equivalent)
    assert funding_requests.payload_hash(original) != funding_requests.payload_hash(original.model_copy(update={"payoff_only_alternative": False}))


@pytest.mark.asyncio
async def test_profile_request_wins_over_original_intake_without_mutating_it():
    saved = profile(intake_id=uuid4(), current_funding_request={"requested_amount": "200000", "revision": 1})
    intake = SimpleNamespace(requested_loan_amount=Decimal("70000"), loan_purpose="Refinance")
    amount, source_name, _, _ = await use_of_funds.source_funding_data(SimpleNamespace(get=AsyncMock(return_value=intake)), saved)
    assert amount == Decimal("200000") and source_name == "profile.current_funding_request"
    assert intake.requested_loan_amount == Decimal("70000")


@pytest.mark.asyncio
@pytest.mark.parametrize("room", [False, True])
async def test_submitted_budget_cannot_bypass_exactness_through_room_or_staff(room):
    saved = profile()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: saved)), flush=AsyncMock())
    patch_budget = UseOfFundsPatch(items=[UseOfFundsItem(id="partial", category="working_capital", label="Partial", amount=1)], expected_revision=2)
    with patch.object(use_of_funds, "source_funding_data", source):
        with pytest.raises(HTTPException) as error:
            await use_of_funds._persist_budget(db, saved, patch_budget, user=None if room else SimpleNamespace(id=uuid4()))
    assert error.value.status_code == 422
    assert saved.use_of_funds_revision == 2
    db.flush.assert_not_called()


def test_chat_corrections_become_proposals_and_client_patch_cannot_change_submission():
    intake = SimpleNamespace(requested_loan_amount=70000, intake_state={})
    assert not funding_requests.accept_initial_amount_fact(intake, 200000, source="client_chat")
    assert intake.requested_loan_amount == 70000
    assert intake.intake_state["requested_amount_change_proposal"]["requires_underwriter_review"]
    with pytest.raises(HTTPException) as error:
        funding_requests.require_initial_or_unchanged_amount(intake, 200000)
    assert error.value.status_code == 403
    intake.intake_state["current_funding_request"] = {"requested_amount": "200000", "revision": 1}
    assert funding_requests.intake_requested_amount(intake) == Decimal("200000")
    funding_requests.require_initial_or_unchanged_amount(intake, 200000)
    assert not funding_requests.accept_initial_amount_fact(intake, 250000, source="internal_ai_chat")
    assert funding_requests.intake_requested_amount(intake) == Decimal("200000")


def test_current_ai_context_uses_reviewed_request_without_rewriting_debt_or_leaking_reason():
    original = {"dealer_details": {"requested_loan_amount": 70000}, "mca_details": {"requested_amount": 70000, "remaining_payback": 250000}}
    result = funding_requests.project_request_context(original, {"requested_amount": "200000", "revision": 1, "reason": "Private staff note", "payoff_only_alternative": True})
    assert result["dealer_details"]["requested_loan_amount"] == 200000
    assert result["mca_details"] == {"requested_amount": 200000, "remaining_payback": 250000}
    assert original["dealer_details"]["requested_loan_amount"] == 70000
    assert "Private staff note" not in str(result)


@pytest.mark.asyncio
async def test_inflight_review_checks_and_locks_current_request_revision_before_publication():
    saved = profile(current_funding_request={"requested_amount": "200000", "revision": 2})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: saved)))
    assert not await funding_requests.lock_review_revision(db, saved, 1)
    query = db.execute.await_args.args[0]
    assert query._for_update_arg is not None
    assert query.get_execution_options()["populate_existing"] is True
    assert await funding_requests.lock_review_revision(db, saved, 2)
    assert not await funding_requests.lock_review_revision(db, saved, 2, 1)
    assert await funding_requests.lock_review_revision(db, saved, 2, 2)


def test_mca_chat_request_is_a_proposal_while_documented_debt_facts_remain_available():
    from app.routers.dealer_ai_intake import _merge_mca_details

    intake = SimpleNamespace(requested_loan_amount=70000, intake_state={})
    with patch("app.routers.dealer_ai_intake.mca_refinance_policy.persist"):
        _merge_mca_details(intake, {"requested_amount": 200000, "remaining_payback": 250000})
    assert intake.requested_loan_amount == 70000
    assert "requested_amount" not in intake.intake_state["mca_details"]
    assert intake.intake_state["mca_details"]["remaining_payback"] == 250000
    assert intake.intake_state["requested_amount_change_proposal"]["requested_amount"] == "200000.0"


@pytest.mark.asyncio
async def test_correction_keeps_room_and_staff_budget_equal_and_preserves_original_checklist_and_history():
    from app.routers import application_profiles as rooms

    saved = profile(intake_id=uuid4(), primary_bucket_id=uuid4())
    accepted = [{"name": "Bank statements", "status": "accepted"}]
    original_review = {"key_metrics": {"requested_amount": 70000, "annual_revenue": 900000}}
    intake = SimpleNamespace(requested_loan_amount=70000, intake_state={"accepted_checklist": accepted, "loan_program_fit": {"old": True}}, result_snapshot=original_review, latest_review_id=uuid4(), latest_review=object())
    bucket = SimpleNamespace(ai_context={"unrelated": {"keep": True}})
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(scalar_one=lambda: saved), SimpleNamespace(scalar_one_or_none=lambda: None),
            SimpleNamespace(scalar_one_or_none=lambda: intake), SimpleNamespace(scalar_one_or_none=lambda: bucket),
            None, None,
        ]), add=Mock(), flush=AsyncMock(), identity_map={},
    )
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    with patch.object(use_of_funds, "source_funding_data", source), patch("app.services.application_profiles.log_profile_action", AsyncMock()):
        staff = await funding_requests.update_request(db, saved, payload(), user)
        budget = await use_of_funds.read_budget(db, saved)
    assert intake.requested_loan_amount == 70000
    assert original_review == {"key_metrics": {"requested_amount": 70000, "annual_revenue": 900000}}
    assert intake.result_snapshot["_funding_request_stale"] is True
    assert intake.intake_state["accepted_checklist"] is accepted
    assert "loan_program_fit" not in intake.intake_state
    assert intake.intake_state["current_funding_request"]["requested_amount"] == "200000.00"
    assert bucket.ai_context["unrelated"] == {"keep": True}
    for call in db.execute.await_args_list[:4:2]:
        assert call.args[0]._for_update_arg is not None
    with patch.object(rooms, "_public_application_room", AsyncMock(return_value=(SimpleNamespace(), saved))), patch.object(use_of_funds, "read_budget", AsyncMock(return_value=budget)):
        room = await rooms.public_application_room_use_of_funds("token", SimpleNamespace(passcode="123456"), SimpleNamespace(), db)
    assert room.requested_amount == staff.requested_amount == 200000
    assert room.items == staff.items and room.revision == staff.use_of_funds_revision
    assert room.complete and room.payoff_only_alternative and room.updated_by_user_id is None


@pytest.mark.asyncio
async def test_old_contact_payload_can_save_unchanged_amount_but_cannot_bypass_correction():
    from app.routers import dealer_ai_intake as core
    from app.routers.dealer_ai_intake_routes.admin import update_lead_contact

    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), requested_loan_amount=70000, intake_state={"current_funding_request": {"requested_amount": "200000", "revision": 1}}, phone="old")
    db = SimpleNamespace(commit=AsyncMock())
    with patch.object(core, "_require_governance_admin"), patch.object(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake)), patch.object(core, "_log", AsyncMock()), patch.object(core, "_response", AsyncMock(return_value="saved")):
        result = await update_lead_contact(intake.id, core.AdminLeadContactUpdate(phone="2125550123", requested_loan_amount=200000), SimpleNamespace(), SimpleNamespace(), db)
        assert result == "saved" and intake.requested_loan_amount == 70000
        with pytest.raises(HTTPException) as error:
            await update_lead_contact(intake.id, core.AdminLeadContactUpdate(requested_loan_amount=250000), SimpleNamespace(), SimpleNamespace(), db)
        assert error.value.status_code == 422
    db.commit.assert_awaited_once()
