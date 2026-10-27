from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.routers import dealer_ai_intake as routes
from app.services import bucket_ai
from app.services import mca_refinance_policy as policy


def make_intake(count=2, amount=350_000, term=120, *, details=None, typed=None):
    state = {"mca_qualification": {"active_mca_count": count, "requested_amount": amount, "requested_term_months": term}}
    if details is not None:
        state["mca_details"] = details
    if typed is not None:
        state["mca_terms_summary"] = typed
    bucket = SimpleNamespace(id=uuid4(), bucket_type="mca_refinance_intake", ai_context={}, requested_documents=[], files=[])
    return SimpleNamespace(id=uuid4(), variant="mca_refi_v1", intake_state=state, bucket=bucket, bucket_id=bucket.id,
                           requested_loan_amount=amount, status="collecting", preferred_language="en", full_name="Test Owner", email="owner@example.com")


def request():
    return Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234), "method": "POST", "path": "/"})


@pytest.mark.parametrize("count", [1, 2])
def test_exact_standard_boundaries_allow_screening_not_approval(count):
    result = policy.evaluate(make_intake(count))
    assert result["auto_underwriting_allowed"] is True
    assert result["review_mode"] == "automated_review"
    assert (result["max_amount"], result["max_term_months"], result["max_active_mcas"]) == (350_000, 120, 2)


@pytest.mark.parametrize("changes,reason", [
    ({"count": 3}, "more_than_two_active_mcas"),
    ({"count": 50}, "more_than_two_active_mcas"),
    ({"amount": 350_000.01}, "amount_above_program_maximum"),
    ({"term": 121}, "term_above_program_maximum"),
])
def test_out_of_scope_is_manual_not_declined(changes, reason):
    result = policy.evaluate(make_intake(**changes))
    assert result["review_mode"] == "manual_review"
    assert result["auto_underwriting_allowed"] is False
    assert reason in result["reason_codes"]


@pytest.mark.parametrize("count,amount", [(None, 100_000), (0, 100_000), ("2", 100_000), (True, 100_000), (1.5, 100_000), (2, None), (2, 0), (2, float("inf")), (2, float("nan"))])
def test_unknown_or_malformed_facts_never_pass(count, amount):
    result = policy.evaluate(make_intake(count=count, amount=amount))
    assert result["auto_underwriting_allowed"] is False
    assert result["review_mode"] == "information_required"


def test_unknown_term_does_not_invent_a_120_month_offer():
    result = policy.evaluate(make_intake(term=None))
    assert result["auto_underwriting_allowed"] is True
    assert result["requested_term_months"] is None


def test_manual_count_takes_precedence_over_missing_amount():
    result = policy.evaluate(make_intake(count=3, amount=None))
    assert result["review_mode"] == "manual_review"
    assert "requested_amount_required" in result["reason_codes"]


def test_typed_obligations_and_larger_payoff_cannot_be_hidden_by_declaration():
    result = policy.evaluate(make_intake(count=2, amount=300_000, typed={"active_mca_count": 3, "remaining_payback": 400_000}))
    assert result["review_mode"] == "manual_review"
    assert result["active_mca_count"] == 3
    assert result["requested_amount"] == 400_000


def test_ai_proposed_facts_can_tighten_but_not_open_eligibility():
    intake = make_intake(count=None, amount=None, details={"advance_count": 2, "requested_amount": 100_000})
    assert policy.evaluate(intake)["auto_underwriting_allowed"] is False
    intake = make_intake(details={"advance_count": 3})
    assert policy.evaluate(intake)["review_mode"] == "manual_review"


def test_borrower_cannot_clear_manual_hold_by_lowering_declaration():
    intake = make_intake(count=3, amount=400_000, term=121)
    policy.persist(intake)
    intake.intake_state["mca_qualification"] = {"active_mca_count": 2, "requested_amount": 300_000, "requested_term_months": 120}
    intake.requested_loan_amount = 300_000
    result = policy.persist(intake)
    assert result["review_mode"] == "manual_review"
    assert result["active_mca_count"] == 3
    assert result["requested_amount"] == 400_000
    assert result["requested_term_months"] == 121
    assert intake.intake_state["mca_manual_review_hold"]["observed_at"]


def evidence(funder, *, identity=None, balance=50_000):
    row = {"lender": funder, "current_balance": balance}
    if identity:
        row["contract_id"] = identity
    return {"ai_classification": "debt_schedule", "key_facts": {"debts": [row]}}


def test_three_distinct_obligations_across_documents_require_manual_review():
    bounds = policy.evidence_bounds([evidence("Funder A"), evidence("Funder B"), evidence("Funder C")])
    assert bounds == {"active_mca_count": 3, "remaining_payback": 150_000}


def test_duplicate_agreement_and_payoff_do_not_count_as_another_mca():
    bounds = policy.evidence_bounds([evidence("Funder A", identity="abc"), evidence("FUNDER A", identity="abc", balance=45_000), evidence("Funder A", balance=44_000), evidence("Funder B")])
    assert bounds == {"active_mca_count": 2, "remaining_payback": 100_000}
    assert policy.evidence_bounds([evidence("Funder A", identity="abc"), evidence("Funder A", identity="def"), evidence("Funder A", identity="ghi")])["active_mca_count"] == 3


def test_manual_preference_preserves_application_without_inventing_exact_count():
    intake = make_intake(count=None, amount=None)
    result = policy.persist(intake, request_manual=True)
    assert result["review_mode"] == "manual_review"
    assert result["manual_review_requested"] is True
    assert result["active_mca_count"] is None
    assert "manual_review_requested" in result["reason_codes"]


@pytest.mark.parametrize("context", [{"funding_intent": "mca_refinance"}, {"selected_programs": ["mca_refinance"]}, {"selected_programs": [{"program_key": "mca_refinance"}]}])
def test_alternate_mca_contexts_cannot_bypass_detection(context):
    bucket = SimpleNamespace(bucket_type="general", ai_context=context)
    assert policy.is_mca_refinance(bucket=bucket)


def test_new_mca_funding_is_a_separate_product():
    bucket = SimpleNamespace(bucket_type="general", ai_context={"funding_intent": "merchant_cash_advance", "source": "mca_funding"})
    assert policy.is_mca_refinance(bucket=bucket) is False


def test_start_and_typed_terms_accept_more_than_two():
    payload = routes.McaRefiStart(full_name="Test Owner", email="owner@example.com", phone="+12025550123", active_mca_count=3, requested_amount=500_000)
    assert payload.active_mca_count == 3
    row = {"funder": "Example", "remaining_payback": 100_000, "payment_amount": 1000, "payment_frequency": "weekly"}
    assert len(routes.McaTermsSubmission(business_name="Test Business", advances=[row] * 50, acknowledgment=True).advances) == 50
    with pytest.raises(ValidationError):
        routes.McaRefiStart(full_name="Test Owner", email="owner@example.com", phone="+12025550123", active_mca_count=True)
    with pytest.raises(ValidationError):
        routes.McaRefiQualification(active_mca_count=2, requested_amount=True)


@pytest.mark.asyncio
async def test_start_saves_out_of_scope_application_instead_of_rejecting_it():
    bucket = SimpleNamespace(id=uuid4(), ai_context={}, bucket_type="mca_refinance_intake")
    client = SimpleNamespace(id=uuid4(), email="owner@example.com")
    created = None

    def construct(**fields):
        nonlocal created
        created = SimpleNamespace(**fields, id=uuid4(), bucket=None, status="collecting")
        return created

    async def load(*args):
        return created

    db = Mock(commit=AsyncMock())
    payload = routes.McaRefiStart(full_name="Test Owner", email="owner@example.com", phone="+12025550123", active_mca_count=3,
                                  requested_amount=450_000, terms_accepted=True, privacy_accepted=True)
    with patch.object(routes, "PublicUnderwritingIntake", side_effect=construct), patch.object(routes, "_throttle_or_429"), patch.object(routes, "_latest_active_intake_by_email", AsyncMock(return_value=None)), patch.object(routes, "_find_or_create_mca_client", AsyncMock(return_value=client)), patch.object(routes, "_create_bucket_for_mca_refi", AsyncMock(return_value=(bucket, SimpleNamespace(id=uuid4())))), patch.object(routes, "_load_public_intake", side_effect=load), patch.object(routes, "_record_resume_email", AsyncMock()), patch.object(routes, "_response", AsyncMock(return_value="saved")):
        result = await routes.start_mca_refinance(payload, request(), db)
    assert result == "saved"
    db.add.assert_called_once_with(created)
    assert created.status == "manual_review"
    assert created.intake_state["mca_refinance_policy"]["review_mode"] == "manual_review"


@pytest.mark.asyncio
async def test_three_typed_advances_are_saved_and_tighten_a_two_advance_declaration():
    intake = make_intake(count=2, amount=300_000)
    intake.bucket.requested_documents = [SimpleNamespace(category="Debts", status="requested")]
    row = {"funder": "Example", "remaining_payback": 150_000, "payment_amount": 1000, "payment_frequency": "weekly"}
    payload = routes.McaTermsSubmission(business_name="Test Business", advances=[row] * 3, acknowledgment=True)
    db = Mock(commit=AsyncMock())
    with patch.object(routes, "_load_public_intake", AsyncMock(return_value=intake)), patch.object(routes, "_store_drafted_form_pdf", AsyncMock(return_value="saved-document")), patch("app.services.dealer_forms_pdf.render_debt_schedule_pdf", return_value=b"mock-pdf"):
        result = await routes.submit_mca_refinance_terms("token", payload, request(), db)
    assert result == "saved-document"
    assert intake.intake_state["mca_terms_summary"] == {"active_mca_count": 3, "remaining_payback": 450_000}
    assert intake.intake_state["mca_refinance_policy"]["review_mode"] == "manual_review"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_manual_submission_is_saved_and_never_creates_ai_job():
    intake = make_intake(count=3, amount=None)
    db = Mock(commit=AsyncMock())
    with patch.object(routes, "_load_public_intake", AsyncMock(return_value=intake)), patch.object(routes, "_response", AsyncMock(return_value="response")), patch.object(routes, "_log", AsyncMock()) as audit, patch.object(routes, "run_bucket_ai_review", AsyncMock()) as ai:
        result = await routes.run_mca_refinance_review("token", request(), db)
    assert result == "response"
    assert intake.status == "manual_review"
    assert intake.intake_state["mca_refinance_policy"]["manual_review_requested"] is True
    assert audit.await_args.args[2] == "mca_refinance_manual_review_requested"
    db.add.assert_not_called()
    ai.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_eligibility_does_not_create_ai_job_or_manual_receipt():
    intake = make_intake(count=None)
    db = Mock(commit=AsyncMock())
    with patch.object(routes, "_log", AsyncMock()), patch.object(routes, "run_bucket_ai_review", AsyncMock()) as ai:
        result = await routes._execute_intake_review(db, intake, request=request(), actor_name="Owner", actor_email=None, actor_role="public_lead", log_event="test", detail="test")
    assert result is None
    assert intake.intake_state["mca_refinance_policy"]["manual_review_requested"] is False
    db.add.assert_not_called()
    ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_admin_queue_is_rejected_before_any_ai_row():
    intake = make_intake(count=3)
    db = Mock()
    with pytest.raises(HTTPException) as error:
        await routes._create_queued_review(db, intake, request=request(), actor_name="Desk", actor_email=None, actor_role="super_admin", log_event="test", detail="test")
    assert error.value.status_code == 409
    assert error.value.detail["mca_refinance_policy"]["review_mode"] == "manual_review"
    db.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [3, None])
async def test_manual_and_unknown_public_credit_requests_never_call_bureau(count):
    intake = make_intake(count=count)
    db = Mock(commit=AsyncMock())
    with patch.object(routes, "_load_public_intake", AsyncMock(return_value=intake)), patch.object(routes, "_response", AsyncMock(return_value="response")), patch.object(routes, "_throttle_or_429"), patch("app.services.credit_pull_core.run_soft_pull", AsyncMock()) as bureau:
        result = await routes.mca_refinance_credit_pull("token", routes.PublicCreditPullRequest(), request(), db)
    assert result == "response"
    bureau.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_qualification_update_saves_manual_route_without_ai():
    intake = make_intake(count=None, amount=None)
    db = Mock(commit=AsyncMock())
    with patch.object(routes, "_load_public_intake", AsyncMock(return_value=intake)), patch.object(routes, "_response", AsyncMock(return_value="response")), patch.object(routes, "_log", AsyncMock()):
        await routes.update_mca_refinance_qualification("token", routes.McaRefiQualification(active_mca_count=3, requested_amount=450_000), request(), db)
    assert intake.intake_state["mca_refinance_policy"]["review_mode"] == "manual_review"
    assert intake.requested_loan_amount == 450_000
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_resumed_eligible_file_can_request_manual_review_without_changing_known_facts():
    intake = make_intake(count=2, amount=300_000)
    db = Mock(commit=AsyncMock())
    with patch.object(routes, "_load_public_intake", AsyncMock(return_value=intake)), patch.object(routes, "_response", AsyncMock(return_value="response")), patch.object(routes, "_log", AsyncMock()) as audit:
        await routes.update_mca_refinance_qualification("token", routes.McaRefiQualification(manual_review_requested=True), request(), db)
    result = intake.intake_state["mca_refinance_policy"]
    assert result["review_mode"] == "manual_review"
    assert result["manual_review_requested"] is True
    assert result["active_mca_count"] == 2
    assert intake.requested_loan_amount == 300_000
    assert "manual_review_requested" not in intake.intake_state["mca_qualification"]
    assert audit.await_args.args[2] == "mca_refinance_manual_review_requested"
    db.commit.assert_awaited_once()


def test_partial_qualification_requires_explicit_manual_request():
    with pytest.raises(ValidationError):
        routes.McaRefiQualification(active_mca_count=2)
    with pytest.raises(ValidationError):
        routes.McaRefiQualification(requested_amount=300_000)
    assert routes.McaRefiQualification(manual_review_requested=True).requested_amount is None


def test_manual_route_hides_stale_good_probability_result():
    intake = make_intake(count=3)
    intake.latest_review = SimpleNamespace(result={"probability_status": "Good probability - book call"})
    assert routes._lead_result(intake)["probability_status"] == "Manual review required"


@pytest.mark.asyncio
async def test_stale_queued_job_rechecks_live_facts_before_any_ai_work():
    intake = make_intake(count=3)
    review = SimpleNamespace(id=uuid4(), bucket=intake.bucket, bucket_id=intake.bucket_id, status="queued", context_snapshot={"review_type": "mca_refi_v1", "mca_refinance_policy": {"auto_underwriting_allowed": True}}, result={"old": "decision"})
    db = Mock(execute=AsyncMock(side_effect=[Mock(scalar_one_or_none=Mock(return_value=review)), Mock(scalar_one_or_none=Mock(return_value=intake))]), commit=AsyncMock())
    with patch.object(bucket_ai, "log_bucket_ai_activity", AsyncMock()), patch.object(bucket_ai, "analyze_bucket_file", AsyncMock()) as extraction, patch.object(bucket_ai, "tracked_messages_create", AsyncMock()) as model:
        result = await bucket_ai.run_bucket_ai_review(db, review.id)
    assert result is review
    assert review.status == "manual_review"
    assert review.result is None
    assert intake.status == "manual_review"
    extraction.assert_not_awaited()
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_documentary_third_position_stops_before_checks_or_synthesis():
    intake = make_intake(count=2, amount=150_000)
    intake.bucket.name = "MCA review"
    intake.bucket.client_name = "Test Business"
    intake.bucket.purpose = "refinance"
    intake.bucket.description = "Test"
    files = [SimpleNamespace(id=uuid4(), status="uploaded", deleted_at=None, requested_document_id=None, file_name=f"advance-{index}.pdf", content_type="application/pdf", size_bytes=100, uploaded_by_name="Test", parent_zip_file_id=None) for index in range(3)]
    intake.bucket.files = files
    analyses = [SimpleNamespace(id=uuid4(), status="completed", content_hash="hash", classification="debt_schedule", confidence="high", summary="Current advance", analysis={"key_facts": evidence(f"Funder {index}")["key_facts"]}) for index in range(3)]
    review = SimpleNamespace(id=uuid4(), bucket=intake.bucket, bucket_id=intake.bucket_id, status="queued", context_snapshot={"review_type": "mca_refi_v1"}, result=None)
    db = Mock(execute=AsyncMock(side_effect=[Mock(scalar_one_or_none=Mock(return_value=review)), Mock(scalar_one_or_none=Mock(return_value=intake))]), commit=AsyncMock(), refresh=AsyncMock())
    with patch.object(bucket_ai, "log_bucket_ai_activity", AsyncMock()), patch.object(bucket_ai, "analyze_bucket_file", AsyncMock(side_effect=analyses)), patch.object(bucket_ai.provenance, "is_internal_package_output", return_value=False), patch.object(bucket_ai.merchant_processing, "is_offer_document", return_value=False), patch("app.services.operator_file_links.selected_files_for_intake", AsyncMock(return_value=[])), patch("app.services.document_qualification_review.review_bucket_checks", AsyncMock()) as document_checks, patch.object(bucket_ai, "tracked_messages_create", AsyncMock()) as model:
        result = await bucket_ai.run_bucket_ai_review(db, review.id)
    assert result.status == "manual_review"
    assert intake.intake_state["mca_refinance_policy"]["active_mca_count"] == 3
    document_checks.assert_not_awaited()
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_linked_evidence_is_retained_without_queuing_manual_underwriting():
    from app.services.operator_file_links import queue_link_change_review
    intake = make_intake(count=3)
    db = Mock(get=AsyncMock(return_value=intake.bucket))
    assert await queue_link_change_review(db, intake=intake, requested_by_user_id=uuid4()) is None
    db.add.assert_not_called()
