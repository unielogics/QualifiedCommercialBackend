from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.bucket import Bucket
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.funding_program import FundingProgramCommercialTermsCreate
from app.services import application_programs, funding_programs, mca_refinance_policy
from app.services.funding_program_baselines import suggested_baseline
from app.services.program_rules import evaluate_rules, validate_rules


@pytest.mark.parametrize("amount,count,term,matched", [
    (350_000, 2, 120, True), (350_000, 2, None, True),
    (350_001, 2, 120, False), (100_000, 3, 120, False),
    (100_000, None, 120, False), (None, 2, 120, False),
    (100_000, 2, 121, False), (0, 2, 120, False), (100_000, 0, 120, False),
])
def test_suggested_mca_criteria_use_real_program_boundaries(amount, count, term, matched):
    baseline = suggested_baseline("mca_refinance")
    validate_rules(baseline["rules"], enforce_supported_fields=True)
    context = {"requested_amount": amount, "active_mca_count": count, "requested_term_months": term}
    result = evaluate_rules(baseline["rules"], context)
    policy = mca_refinance_policy.evaluate_fit_context(context)
    assert (result.matched and policy["auto_underwriting_allowed"]) is matched
    assert baseline["rules"]["intrinsic_limits"]["maximum_term_months"] == 120
    assert any("manual desk review" in note for note in baseline["source_notes"])


@pytest.mark.asyncio
@pytest.mark.parametrize("facts,status", [
    ({"active_mca_count": 2, "requested_amount": 350_000}, "recommended"),
    ({"active_mca_count": 3, "requested_amount": 100_000}, "custom_review"),
    ({"active_mca_count": 2, "requested_amount": 350_001}, "custom_review"),
    ({"active_mca_count": 2, "requested_amount": 100_000, "requested_term_months": 121}, "custom_review"),
    ({"active_mca_count": None, "requested_amount": 100_000}, "needs_information"),
    ({"active_mca_count": 2, "requested_amount": None}, "needs_information"),
    ({"active_mca_count": 0, "requested_amount": 100_000}, "needs_information"),
])
async def test_permissive_legacy_published_mca_rules_cannot_bypass_intrinsic_policy(facts, status):
    program = SimpleNamespace(id=uuid4(), program_key="mca_refinance", name="MCA refinance", public_slug="mca-refinance")
    version = SimpleNamespace(id=uuid4(), version=1, rules={"fit": {"field": "loan_purpose", "op": "present"}})
    context = {"vertical": "main_street", "loan_purpose": "refinance", **facts}
    with (
        patch.object(application_programs, "profile_fit_context", AsyncMock(return_value=context)),
        patch.object(application_programs.program_catalog, "catalog_rows", AsyncMock(return_value=[program])),
        patch.object(application_programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
        patch.object(application_programs.program_catalog, "published_versions_by_program", AsyncMock(return_value={program.id: version})),
        patch.object(application_programs, "_catalog_scope_match", return_value=(True, [])),
    ):
        candidate = (await application_programs.published_candidates(SimpleNamespace(), SimpleNamespace()))[0]
    assert candidate.recommendation_status == status
    assert candidate.eligible is (status == "recommended")
    if status != "recommended":
        assert candidate.requires_review
        assert candidate.confidence == 0


@pytest.mark.asyncio
async def test_manual_intake_cannot_escape_its_route_by_lowering_an_alternate_requested_amount():
    program = SimpleNamespace(id=uuid4(), program_key="mca_refinance", name="MCA refinance", public_slug="mca-refinance")
    version = SimpleNamespace(id=uuid4(), version=1, rules={"fit": {"field": "loan_purpose", "op": "present"}})
    context = {"vertical": "main_street", "loan_purpose": "refinance", "active_mca_count": 2, "requested_amount": 100_000,
               "mca_refinance_policy": {"review_mode": "manual_review", "auto_underwriting_allowed": False, "reason_codes": ["amount_above_program_maximum"]}}
    with (
        patch.object(application_programs, "profile_fit_context", AsyncMock(return_value=context)),
        patch.object(application_programs.program_catalog, "catalog_rows", AsyncMock(return_value=[program])),
        patch.object(application_programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
        patch.object(application_programs.program_catalog, "published_versions_by_program", AsyncMock(return_value={program.id: version})),
        patch.object(application_programs, "_catalog_scope_match", return_value=(True, [])),
    ):
        candidate = (await application_programs.published_candidates(SimpleNamespace(), SimpleNamespace()))[0]
    assert candidate.recommendation_status == "custom_review"
    assert not candidate.eligible


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected_count,expected_term", [
    ({}, None, None),
    ({"mca_details": {"advance_count": 1}}, None, None),
    ({"mca_qualification": {"active_mca_count": 2, "requested_amount": 100_000, "requested_term_months": 120}}, 2, 120),
    ({"mca_terms_summary": {"active_mca_count": 3, "remaining_payback": 100_000}}, 3, None),
])
async def test_fit_context_materializes_declarations_without_guessing_from_upload_count(state, expected_count, expected_term):
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), variant="mca_refi_v1", loan_purpose="mca_refinance", requested_loan_amount=100_000,
                             estimated_credit_score=None, intake_state=state, result_snapshot={}, asset_rows=[])
    bucket = SimpleNamespace(id=intake.bucket_id, ai_context={})
    profile = SimpleNamespace(id=uuid4(), intake_id=intake.id, primary_bucket_id=bucket.id, vertical="main_street", funding_category="mca_refinance",
                              entity_type=None, industry=None, subindustry=None, naics_code=None)
    result = Mock()
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(side_effect=lambda model, _id: intake if model is PublicUnderwritingIntake else bucket if model is Bucket else None))
    budget = SimpleNamespace(items=[], total=None, real_estate_equipment_amount=None, real_estate_equipment_pct=None, complete=False, category_totals={"equipment": 0})
    with (
        patch.object(application_programs, "source_funding_data", AsyncMock(return_value=(100_000, "intake", "mca_refinance", None))),
        patch.object(application_programs, "summarize_use_of_funds", return_value=budget),
        patch.object(application_programs, "_evidence_inventory", AsyncMock(return_value=([SimpleNamespace(id=uuid4()) for _ in range(6)], {}, set()))),
        patch.object(application_programs, "_latest_evidence_decisions", AsyncMock(return_value={})),
    ):
        context = await application_programs.profile_fit_context(db, profile)
    assert context["evidence_count"] == 6
    assert context["active_mca_count"] == expected_count
    assert context["requested_term_months"] == expected_term


def _terms(**changes):
    return SimpleNamespace(id=uuid4(), version=1, status="published", maximum_amount=None, maximum_term_months=None, disclosures=[], **changes)


def test_public_terms_apply_ceilings_without_rewriting_historical_values():
    original = _terms()
    public = funding_programs._commercial_terms_read(original, program_key="mca_refinance")
    assert public.maximum_amount == 350_000
    assert public.maximum_term_months == 120
    assert public.id == original.id and public.version == 1
    assert original.maximum_amount is None and original.maximum_term_months is None
    assert original.disclosures == []
    assert "manual review" in public.disclosures[-1]
    assert funding_programs._commercial_terms_read(original, program_key="revenue_based_financing").maximum_amount is None


def test_public_terms_preserve_explicit_stricter_published_limits():
    original = SimpleNamespace(id=uuid4(), version=2, status="published", maximum_amount=250_000, maximum_term_months=84, disclosures=[])
    public = funding_programs._commercial_terms_read(original, program_key="mca_refinance")
    assert public.maximum_amount == 250_000
    assert public.maximum_term_months == 84
    assert original.maximum_amount == 250_000 and original.maximum_term_months == 84


@pytest.mark.asyncio
async def test_new_mca_draft_without_maxima_defaults_to_intrinsic_ceilings():
    result = Mock()
    result.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=result), add=Mock(), flush=AsyncMock())
    draft = await funding_programs.create_commercial_terms(db, SimpleNamespace(id=uuid4(), program_key="mca_refinance", name="MCA refinance"),
                                                         FundingProgramCommercialTermsCreate(confirmed=True), SimpleNamespace(id=uuid4()))
    assert draft.maximum_amount == 350_000
    assert draft.maximum_term_months == 120
    assert draft.status == "draft"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"maximum_amount": 350_001}, {"minimum_amount": 350_001}, {"maximum_term_months": 121}, {"minimum_term_months": 121}])
async def test_new_mca_terms_cannot_exceed_policy_ceilings_before_writes(changes):
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), flush=AsyncMock())
    payload = FundingProgramCommercialTermsCreate(confirmed=True, **changes)
    with pytest.raises(HTTPException) as error:
        await funding_programs.create_commercial_terms(db, SimpleNamespace(program_key="mca_refinance"), payload, SimpleNamespace())
    assert error.value.status_code == 422
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"maximum_amount": 350_001}, {"maximum_term_months": 121}])
async def test_legacy_over_limit_drafts_cannot_be_published(changes):
    values = {"maximum_amount": None, "maximum_term_months": None, "minimum_amount": None, "minimum_term_months": None, **changes}
    draft = SimpleNamespace(id=uuid4(), status="draft", **values)
    result = Mock()
    result.scalar_one_or_none.return_value = draft
    db = SimpleNamespace(execute=AsyncMock(return_value=result), add=Mock(), flush=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await funding_programs.publish_commercial_terms(db, SimpleNamespace(id=uuid4(), program_key="mca_refinance"), draft.id, None, SimpleNamespace())
    assert error.value.status_code == 422
    assert draft.status == "draft"
    db.add.assert_not_called()
