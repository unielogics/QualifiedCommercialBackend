"""Saved recurring debt-service relief is allocated, never invented earnings."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.application_profile import ApplicationRequirementState
from app.routers.application_profiles import _client_form_response
from app.schemas.earnings_improvement import empty_body, validate_body
from app.services import (
    client_financial_forms,
    earnings_improvement,
    financial_workspace,
    investor_package,
    operating_plan_pdf,
    operating_plan_service,
    released_cash_flow,
    requirement_collection_completion,
)
from app.tests.test_earnings_improvement import body as operating_body
from app.tests.test_earnings_improvement import context as earnings_context
from app.tests.test_financial_workspace import Memory, context


def cockpit():
    result = earnings_context()
    result["debt"]["service"].update(monthly=4000, annual=48000, currency="USD", version_id=str(uuid4()))
    result["plan"] = {"record": {"updated_at": "2026-11-10T14:00:00+00:00", "retire": ["row:loan-1"],
        "new_loan_amount": 172488, "overrides": {}}, "evaluation": {"active": True, "retired_count": 1,
        "retired_ids": ["row:loan-1"], "debt_service_after_regular_monthly": 3000,
        "debt_service_after_monthly": 15000, "debt_service_after_annual": 180000,
        "new_regular_payment_monthly": 1000, "cash_kept": 27000, "ebitda": {}}}
    return result


def allocated(current, amount=1000):
    raw = operating_body()
    raw["released_cash_flow"] = {"source_fingerprint": released_cash_flow.source(current)["fingerprint"],
        "allocations": [{"id": str(uuid4()), "label": "Payroll reserve", "amount_monthly": amount,
                         "expense_row_id": None, "notes": "Recurring monthly payroll capacity"}]}
    return validate_body(raw)


def test_only_saved_regular_debt_reduction_is_allocatable_not_balloon_replay_or_cash_kept():
    current = cockpit()
    snapshot = released_cash_flow.evaluate(empty_body(), current)
    assert snapshot["required"] and snapshot["available"] and snapshot["amount_monthly"] == 1000
    assert snapshot["source"]["after_monthly"] == 3000
    assert snapshot["status"] == "incomplete" and not snapshot["complete"]
    current["plan"]["record"].pop("updated_at")
    unsaved = released_cash_flow.evaluate(empty_body(), current)
    assert not unsaved["required"] and not unsaved["available"] and unsaved["amount_monthly"] is None


@pytest.mark.parametrize("issue", ["unknown_current", "unknown_after", "replacement", "override", "identity", "retirement", "currency"])
def test_unknown_or_hypothetical_relief_is_not_reported_as_zero_or_allocatable(issue):
    current = cockpit()
    if issue == "unknown_current":
        current["debt"]["service"]["monthly"] = None
    elif issue == "unknown_after":
        current["plan"]["evaluation"]["debt_service_after_regular_monthly"] = None
    elif issue == "replacement":
        current["plan"]["evaluation"]["ebitda"]["replacement_payment_missing"] = True
    elif issue == "override":
        current["plan"]["record"]["overrides"]["debt_service_monthly"] = 4000
    elif issue == "identity":
        current["debt"]["service"]["identity_association_conflict"] = True
    elif issue == "retirement":
        current["plan"]["evaluation"]["retirement_replay_unknown"] = True
    else:
        current["debt"]["service"]["currency"] = "CAD"
    result = released_cash_flow.evaluate(allocated(current), current)
    assert not result["available"] and result["amount_monthly"] is None
    assert result["unallocated_monthly"] is None and not result["complete"] and result["missing"]
    blank = released_cash_flow.evaluate(empty_body(), current)
    assert blank["required"] and not blank["complete"]


@pytest.mark.parametrize("amount", [0, 500, 999.99, 1000.01, 1001, 2000])
def test_partial_overallocated_or_zero_monthly_uses_block_completion(amount):
    current = cockpit()
    result = released_cash_flow.evaluate(allocated(current, amount), current)
    assert not result["complete"] and result["missing"]


def test_balanced_allocation_changes_no_earnings_noi_or_operating_improvement_totals():
    current = cockpit()
    raw = allocated(current)
    before = earnings_improvement.evaluate(operating_body(), current)
    after = earnings_improvement.evaluate(raw, current)
    assert after["released_cash_flow"]["complete"] and after["released_cash_flow"]["allocated_monthly"] == 1000
    for key in ("baseline", "projection", "totals"):
        assert after[key] == before[key]
    assert operating_plan_service.completion(raw, current)["complete"]
    summary = earnings_improvement.package_summary(after)
    html = operating_plan_pdf.build_html(body=raw, financial_totals={"improvement_plan": summary})
    assert "Recurring released-cash-flow allocation" in html and "Payroll reserve" in html
    assert "$1,000.00" in html and "do not increase historical earnings" in html


@pytest.mark.parametrize("change", ["revision", "payment", "debt_source"])
def test_saved_plan_or_current_debt_changes_stale_the_allocation_and_missing_package_conditions(change):
    current = cockpit()
    raw = allocated(current)
    if change == "revision":
        current["plan"]["record"]["updated_at"] = "2026-11-10T15:00:00+00:00"
    elif change == "payment":
        current["plan"]["evaluation"]["debt_service_after_regular_monthly"] = 2500
    else:
        current["debt"]["service"]["version_id"] = str(uuid4())
    result = earnings_improvement.evaluate(raw, current)
    assert result["released_cash_flow"]["status"] == "stale" and not result["released_cash_flow"]["complete"]
    assert any("changed" in message for message in result["missing"])
    assert not operating_plan_service.completion(raw, current)["complete"]


def test_zero_or_increased_known_debt_service_needs_no_positive_cash_allocation():
    current = cockpit()
    current["plan"]["evaluation"]["debt_service_after_regular_monthly"] = 4500
    assert released_cash_flow.evaluate(empty_body(), current)["amount_monthly"] == 0
    assert not released_cash_flow.evaluate(empty_body(), current)["required"]


@pytest.mark.parametrize("change", ["removed", "inactive", "zero"])
def test_prior_allocations_can_be_explicitly_cleared_when_coverage_plan_releases_no_cash(change):
    current = cockpit()
    raw = allocated(current)
    if change == "removed":
        current["plan"]["record"] = {}
    elif change == "inactive":
        current["plan"]["evaluation"]["active"] = False
    else:
        current["plan"]["evaluation"]["debt_service_after_regular_monthly"] = 4000
    assert not released_cash_flow.evaluate(raw, current)["complete"]
    raw["released_cash_flow"] = {"source_fingerprint": None, "allocations": []}
    result = released_cash_flow.evaluate(raw, current)
    assert result["complete"] and not result["required"]
    if change == "zero":
        raw["released_cash_flow"]["source_fingerprint"] = result["source_fingerprint"]
        assert released_cash_flow.evaluate(raw, current)["complete"]


def test_repeated_unchanged_reads_have_stable_source_fingerprint():
    current = cockpit()
    assert released_cash_flow.source(deepcopy(current))["fingerprint"] == released_cash_flow.source(current)["fingerprint"]


def test_client_can_edit_allocations_but_cannot_supply_a_server_amount_or_source_status():
    current = cockpit()
    raw = allocated(current)
    merged = client_financial_forms.merge_client_body("operating_plan", empty_body(), raw)
    assert validate_body(merged)["released_cash_flow"] == raw["released_cash_flow"]
    merged["released_cash_flow"]["amount_monthly"] = 90000
    with pytest.raises(HTTPException):
        validate_body(merged)


@pytest.mark.parametrize("disposition", ["verified", "waived", "not_applicable", "removed"])
def test_current_allocation_controls_exact_requirement_without_overriding_staff_dispositions(disposition):
    current = cockpit()
    raw = allocated(current)
    current["plan"]["record"]["updated_at"] = "2026-11-10T16:00:00+00:00"
    current["operating_plan"] = {"evaluation": earnings_improvement.evaluate(raw, current)}
    metadata = {"coverage": {"complete": True}, "verified_coverage": {"complete": True},
        "collection_completion": {"source": "files", "complete": True, "can_complete": True, "fingerprint": "a" * 64},
        "qualification_review": {"status": "pending", "blocking": True, "reasons": ["Separate program check"]}}
    if disposition == "removed":
        metadata["staff_removal"] = {"removed_at": "2026-11-10T15:00:00+00:00"}
    state = ApplicationRequirementState(id=uuid4(), profile_id=uuid4(), requirement_key=released_cash_flow.REQUIREMENT_KEY,
        status="verified" if disposition == "removed" else disposition, provenance=metadata)
    old_status = state.status
    requested = SimpleNamespace(status="uploaded")
    released_cash_flow.apply_requirement(state, SimpleNamespace(requirement_key=state.requirement_key), requested, current)
    assert state.provenance["qualification_review"]["blocking"]
    if disposition == "verified":
        assert state.status == "stale" and requested.status == "requested"
        assert not state.provenance["collection_completion"]["complete"]
        assert not state.provenance["collection_completion"]["can_complete"]
        assert not state.provenance["coverage"]["complete"]
    else:
        assert state.status == old_status


@pytest.mark.asyncio
async def test_submit_rejects_unallocated_cash_before_pdf_storage_or_publication_and_keeps_draft():
    db = Memory()
    profile, family, draft = context("operating_plan")
    draft.body = operating_body()
    original = deepcopy(draft.body)
    with patch.object(financial_workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit())), \
         patch.object(financial_workspace, "_render") as render, \
         pytest.raises(HTTPException) as error:
        await financial_workspace.publish_version(db, profile, kind="operating_plan", expected_revision=4,
            idempotency_key="unallocated-submit", submit=True, actor=SimpleNamespace(role=Role.CLIENT, id=None))
    assert error.value.status_code == 422 and error.value.detail["code"] == "released_cash_flow_incomplete"
    render.assert_not_called()
    assert not db.versions and not db.added and family.current_version_id is None and draft.body == original


@pytest.mark.asyncio
async def test_old_pdf_collection_action_cannot_bypass_the_current_monthly_allocation():
    allocation = released_cash_flow.evaluate(empty_body(), cockpit())
    state = ApplicationRequirementState(id=uuid4(), requirement_key=released_cash_flow.REQUIREMENT_KEY,
        status="stale", provenance={"released_cash_flow": allocation})
    old_pdf_id = uuid4()
    snapshot = {"source": "files", "fingerprint": "a" * 64, "can_complete": False,
        "complete": False, "reason": allocation["missing"][0], "eligible_file_ids": [str(old_pdf_id)]}
    view = SimpleNamespace(requirement_key=state.requirement_key, collection_completion=snapshot)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: state)))
    payload = SimpleNamespace(reason="Accept prior operating plan PDF", idempotency_key="old-pdf-review",
        expected_fingerprint=snapshot["fingerprint"], file_ids=[old_pdf_id])
    with patch("app.services.owner_credit_review.lock_review_sources", AsyncMock()), \
         patch("app.services.application_programs.get_program_readiness", AsyncMock(return_value=SimpleNamespace(requirements=[view]))), \
         patch("app.services.requirement_collection_receipts.receipts", AsyncMock(return_value=[])), \
         patch("app.services.application_programs._evidence_inventory", AsyncMock()) as files, \
         pytest.raises(HTTPException) as error:
        await requirement_collection_completion.complete(db, SimpleNamespace(id=uuid4()), state.requirement_key,
            payload, SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC))
    assert error.value.status_code == 422
    assert error.value.detail == snapshot["reason"]
    files.assert_not_awaited()
    assert state.status == "stale"


@pytest.mark.asyncio
async def test_balanced_submission_publishes_allocation_in_immutable_proposal_without_historical_income_facts():
    db = Memory()
    profile, family, draft = context("operating_plan")
    current = cockpit()
    draft.body = allocated(current)
    slot = SimpleNamespace(id=uuid4(), status="requested")
    with patch.object(financial_workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(financial_workspace, "load_workspace", AsyncMock(return_value={})), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch.object(financial_workspace, "_render", return_value=b"%PDF allocation") as render, \
         patch.object(financial_workspace, "_write_pdf"), \
         patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), \
         patch("app.routers.application_profiles._requested_slot", AsyncMock(return_value=slot)), \
         patch("app.services.financial_publication.after_publication", AsyncMock()):
        await financial_workspace.publish_version(db, profile, kind="operating_plan", expected_revision=4,
            idempotency_key="balanced-submit", submit=True, actor=SimpleNamespace(role=Role.LOAN_EXEC, id=uuid4()))
    assert len(db.versions) == 1 and slot.status == "uploaded"
    facts = db.versions[0].key_facts
    assert facts["improvement_plan"]["released_cash_flow"]["complete"]
    assert not {"annual_ebitda", "net_income", "operating_income"}.intersection(facts)
    assert render.call_args.args[2]["improvement_plan"]["released_cash_flow"]["allocated_monthly"] == 1000


@pytest.mark.asyncio
async def test_no_operating_plan_yet_exposes_released_cash_obligation_to_summary_and_packages():
    current = cockpit()
    with patch.object(financial_workspace, "existing_family", AsyncMock(return_value=None)), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        summary = await operating_plan_service.summary(Memory(), SimpleNamespace(id=uuid4()))
        published = await operating_plan_service.published_context(Memory(), SimpleNamespace(id=uuid4()), current)
        package = await investor_package.operating_snapshot(Memory(), SimpleNamespace(id=uuid4()), current, {})
        hidden = await investor_package.operating_snapshot(Memory(), SimpleNamespace(id=uuid4()), current,
            {"include_operating_plan": False})
    assert not summary["has_plan"] and summary["evaluation"]["released_cash_flow"]["required"]
    assert not published["available"] and published["evaluation"]["released_cash_flow"]["required"]
    assert any("Allocate" in message for message in published["package"]["missing"])
    assert package["released_cash_flow"]["required"] and package["snapshot"]["state"] == "not_started"
    assert any("Allocate" in message for message in package["missing"])
    assert hidden is None


def test_public_response_exposes_current_allocation_source_without_private_staff_context():
    raw = allocated(cockpit())
    progress = operating_plan_service.completion(raw, cockpit())
    snapshot = {"body": raw, "family_id": str(uuid4()), "draft": {"revision": 2}, "versions": [], "completion": progress}
    result = _client_form_response(snapshot, SimpleNamespace(kind="operating_plan", permission="edit"), "scoped-token")
    assert result["released_cash_flow"] == progress["released_cash_flow"]
    assert "review_reason" not in str(result) and "operating_evidence" not in result
