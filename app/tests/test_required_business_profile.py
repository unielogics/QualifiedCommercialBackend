"""New intake answers persist as declarations, never as verified NAICS facts."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.enums import Role
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import dealer_ai_intake as core
from app.routers.dealer_ai_intake_routes import admin, funding_review, mca_refinance, public_intake
from app.services import business_profile as service
from app.services import industry_suggestion
from app.services.application_profiles import _seed_primary_owner
from app.services.application_programs import _scope_match

ANSWERS = {
    "business_street": " 123 Business   Avenue Suite 2 ",
    "business_city": " Miami ",
    "business_state": "fl",
    "business_zip": "33101-1234",
    "business_website": "N/A",
    "business_description": "We provide physical therapy services.",
    "industry": "professional_practice",
}


@pytest.mark.asyncio
async def test_early_profile_does_not_seed_the_submitting_professional_as_an_owner():
    db = SimpleNamespace(execute=AsyncMock(), add=Mock())
    intake = SimpleNamespace(client_contact_suppressed=True, full_name="Submitting Attorney", email="lawyer@example.com")
    await _seed_primary_owner(db, SimpleNamespace(dealer_id=None), intake=intake)
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.parametrize("key", service.PROFILE_FIELDS)
@pytest.mark.parametrize("bad", [None, "", "   "])
def test_required_new_profile_rejects_missing_and_blank_answers(key, bad):
    with pytest.raises(ValidationError):
        service.RequiredBusinessProfileFields(**{**ANSWERS, key: bad})
    missing = {name: value for name, value in ANSWERS.items() if name != key}
    with pytest.raises(ValidationError):
        service.RequiredBusinessProfileFields(**missing)


@pytest.mark.parametrize(("key", "bad"), [
    ("business_state", "XX"), ("business_state", "Florida"),
    ("business_zip", "3310"), ("business_zip", "331011234"),
    ("business_zip", "٣٣١٠١"), ("industry", "guessed_category"),
    ("business_description", "N/A"), ("business_website", "javascript:alert(1)"),
])
def test_required_profile_rejects_invalid_answers(key, bad):
    with pytest.raises(ValidationError):
        service.RequiredBusinessProfileFields(**{**ANSWERS, key: bad})


def test_explicit_other_and_no_website_are_valid_answers():
    payload = service.RequiredBusinessProfileFields(**{**ANSWERS, "industry": "other"})
    kept = service.record(payload)
    assert kept["street"] == "123 Business Avenue Suite 2"
    assert kept["city"] == "Miami" and kept["state"] == "FL"
    assert kept["zip"] == "33101-1234"
    assert kept["website"] is None and kept["website_none"] is True
    assert kept["industry"] == "other" and kept["captured_at"]


def test_old_files_and_partial_contact_edits_have_no_required_new_answers():
    assert core.AdminLeadContactUpdate(phone="2025550123").model_dump(exclude_unset=True) == {"phone": "+12025550123"}
    edited = core.AdminLeadContactUpdate(business_state=" ny ", business_zip="10001", industry="other")
    assert edited.model_dump(exclude_unset=True) == {"business_state": "NY", "business_zip": "10001", "industry": "other"}
    assert service.of_intake(SimpleNamespace(intake_state=None))["street"] is None
    assert service.BusinessProfileFields().industry is None


def test_private_existing_record_adapter_preserves_true_blanks_and_real_contact_validation():
    legacy = service._legacy_adapter(core.AdminLeadCreate, full_name="Owner", email="owner@example.com", phone="2025550123", secure_room_pin="123456")
    assert legacy.business_street is None and legacy.industry is None
    assert legacy.business_website is None and service.record(legacy) is None
    with pytest.raises(ValidationError):
        service._legacy_adapter(core.AdminLeadCreate, full_name="Owner", email="bad-email", phone="2025550123", secure_room_pin="123456")
    source = SimpleNamespace(address="9 Existing Way", city="Boston", state="MA", zip="02108", website="existing.example", industry="72")
    fields = service.existing_source_fields(source)
    assert fields["business_street"] == "9 Existing Way"
    assert fields["business_zip"] == "02108" and "industry" not in fields
    assert "business_description" not in fields


def test_desk_projection_keeps_original_answers_and_distinguishes_explicit_clearing():
    original = service.record(service.RequiredBusinessProfileFields(**ANSWERS))
    intake = SimpleNamespace(intake_state={"business_profile": deepcopy(original), "desk_facts": {"business_city": "Tampa", "business_website": "https://desk.example", "business_website_none": False, "industry": "other", "business_zip": None}})
    read = service.apply_read(SimpleNamespace(), intake)
    assert read.business_city == "Tampa" and read.business_zip is None
    assert read.business_street == original["street"] and read.industry == "other"
    assert read.business_website == "https://desk.example"
    assert intake.intake_state["business_profile"] == original


@pytest.mark.asyncio
async def test_contact_patch_saves_only_supplied_business_fields_and_preserves_declaration():
    original = service.record(service.RequiredBusinessProfileFields(**ANSWERS))
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), intake_state={"business_profile": deepcopy(original)})
    db = SimpleNamespace(commit=AsyncMock())
    payload = core.AdminLeadContactUpdate(business_state="ny", business_city="New York", business_zip=None, business_website="example.com", industry="other")
    with patch.object(core, "_require_governance_admin"), patch.object(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake)), patch.object(core, "_log", AsyncMock()) as audit, patch.object(core, "_response", AsyncMock(return_value="saved")), patch.object(service, "rearm_unclassified_suggestion", AsyncMock()) as rearm:
        assert await admin.update_lead_contact(intake.id, payload, SimpleNamespace(), SimpleNamespace(), db) == "saved"
    assert intake.intake_state["business_profile"] == original
    assert intake.intake_state["desk_facts"] == {"business_city": "New York", "business_state": "NY", "business_zip": None, "business_website": "https://example.com", "business_website_none": False, "industry": "other"}
    assert "business_street" not in audit.call_args.kwargs["detail"]
    assert "New York" not in audit.call_args.kwargs["detail"]
    rearm.assert_awaited_once_with(db, intake)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["no_activity_fact", "no_candidates"])
async def test_new_activity_rearms_only_unclassified_attempt_and_preserves_history(outcome):
    profile = SimpleNamespace(naics_code=None, classified_by_user_id=None, classification_provenance={"suggestion_attempted_at": "old-time", "suggestion_outcome": outcome, "activity": "Old activity"})
    previous = deepcopy(profile.classification_provenance)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: profile)))
    await service.rearm_unclassified_suggestion(db, SimpleNamespace(id=uuid4()))
    assert profile.classification_provenance["suggestion_attempted_at"] is None
    assert profile.classification_provenance["suggestion_history"] == [previous]
    statement = db.execute.call_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True
    assert statement._for_update_arg is not None


def test_explicit_industry_clear_also_clears_the_legacy_ai_context():
    intake = SimpleNamespace(intake_state={"main_street_details": {"industry": "construction_trades", "intent": "working_capital"}, "desk_facts": {"industry": None}})
    assert service.of_intake(intake)["industry"] is None
    assert "industry" not in core._main_street_details(intake)


def test_suggested_naics_does_not_erase_declared_broad_program_routing():
    declared = {"industry": "construction_trades"}
    profile = SimpleNamespace(industry="Construction", naics_code="238220", classified_by_user_id=None)
    context = {"industry_key": service.routing_industry(profile, declared), "naics_code": profile.naics_code}
    scope = SimpleNamespace(intake_variants=[], intent_keys=[], industry_keys=["construction_trades"], naics_prefixes=[], excluded_naics_prefixes=[], required_fact_keys=[])
    assert context["industry_key"] == "construction_trades"
    assert _scope_match(scope, context) == (True, [])
    profile.classified_by_user_id = uuid4()
    profile.industry = "Retail Trade"
    assert service.routing_industry(profile, declared) == "Retail Trade"
    assert _scope_match(scope, {**context, "industry_key": service.routing_industry(profile, declared)})[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [{"naics_code": "621340"}, {"classified_by_user_id": uuid4()}, {"classification_provenance": {"suggestion_outcome": "provider_error", "suggestion_attempted_at": "old"}}])
async def test_contact_edit_never_reopens_or_overwrites_saved_classification(saved):
    profile = SimpleNamespace(**{"naics_code": None, "classified_by_user_id": None, "classification_provenance": {"suggestion_outcome": "no_activity_fact", "suggestion_attempted_at": "old"}, **saved})
    previous = deepcopy(vars(profile))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: profile)))
    await service.rearm_unclassified_suggestion(db, SimpleNamespace(id=uuid4()))
    assert vars(profile) == previous


@pytest.mark.asyncio
async def test_declared_industry_and_description_feed_the_existing_naics_suggestion():
    intake = SimpleNamespace(intake_state=service.state_entry(service.RequiredBusinessProfileFields(**ANSWERS)))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))), get=AsyncMock(return_value=intake))
    text = await industry_suggestion._activity_text(db, SimpleNamespace(id=uuid4(), intake_id=uuid4()))
    assert text == "Professional and medical practices — We provide physical therapy services."
    assert intake.intake_state["business_profile"]["industry"] == "professional_practice"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["dealer", "real_estate", "main_street", "mca_refinance"])
@pytest.mark.parametrize("existing_source", [False, True])
async def test_admin_variant_creation_preserves_full_profile_and_provisions_without_calling_ai(variant, existing_source):
    created = []
    client = SimpleNamespace(id=uuid4(), email="owner@example.com", lead_intake={})
    bucket, link = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())

    def add(row):
        if isinstance(row, PublicUnderwritingIntake):
            row.id = uuid4()
            created.append(row)

    db = SimpleNamespace(add=add, flush=AsyncMock(), commit=AsyncMock())
    values = {"variant": variant, "full_name": "Owner", "email": client.email, "phone": "2025550123", "secure_room_pin": "123456", "intent": "working_capital"}
    payload = service._legacy_adapter(core.AdminLeadCreate, **values) if existing_source else core.AdminLeadCreate(**ANSWERS, **values)
    with ExitStack() as stack:
        for name in ("_find_or_create_client", "_find_or_create_funding_client", "_find_or_create_mca_client"):
            stack.enter_context(patch.object(core, name, AsyncMock(return_value=client)))
        for name in ("_create_bucket_for_intake", "_create_bucket_for_funding_review", "_create_bucket_for_main_street", "_create_bucket_for_mca_refi"):
            stack.enter_context(patch.object(core, name, AsyncMock(return_value=(bucket, link))))
        stack.enter_context(patch.object(core, "_latest_active_intake_by_email", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(core, "_load_admin_dealer_lead", AsyncMock(side_effect=lambda *_: created[0])))
        stack.enter_context(patch.object(core, "_log", AsyncMock()))
        stack.enter_context(patch.object(core, "_response", AsyncMock(return_value="created")))
        provision = stack.enter_context(patch.object(core.profiles_service, "provision_profile_for_intake", AsyncMock()))
        ai = stack.enter_context(patch.object(industry_suggestion, "suggest_for_profile", AsyncMock(side_effect=RuntimeError("provider unavailable"))))
        assert await core._create_admin_ai_lead_core(payload, SimpleNamespace(), SimpleNamespace(role=Role.SUPER_ADMIN, id=uuid4(), name="Staff", email="staff@example.com"), db) == "created"
    if existing_source:
        assert "business_profile" not in created[0].intake_state
        assert service.of_intake(created[0])["industry"] is None
        if variant == "main_street":
            assert created[0].intake_state["main_street_details"]["industry"] is None
    else:
        assert created[0].intake_state["business_profile"]["street"] == "123 Business Avenue Suite 2"
        assert created[0].intake_state["business_profile"]["website_none"] is True
        assert created[0].intake_state["business_profile"]["industry"] == "professional_practice"
    provision.assert_awaited_once_with(db, created[0])
    ai.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "handler"), [(core.DealerIntakeStart, public_intake.start_dealer_intake), (core.FundingReviewStart, funding_review.start_funding_review), (core.McaRefiStart, mca_refinance.start_mca_refinance)])
async def test_public_create_persists_the_full_profile_before_provisioning(model, handler):
    created = []
    client = SimpleNamespace(id=uuid4(), email="owner@example.com")
    bucket = SimpleNamespace(id=uuid4(), bucket_type="application", ai_context={}, files=[], requested_documents=[])
    link = SimpleNamespace(id=uuid4())

    def add(row):
        if isinstance(row, PublicUnderwritingIntake):
            row.id = uuid4()
            created.append(row)

    db = SimpleNamespace(add=add, flush=AsyncMock(), commit=AsyncMock())
    payload = model(**ANSWERS, full_name="Owner", email=client.email, phone="2025550123", terms_accepted=True, privacy_accepted=True)
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    with ExitStack() as stack:
        for name in ("_find_or_create_client", "_find_or_create_funding_client", "_find_or_create_mca_client"):
            stack.enter_context(patch.object(core, name, AsyncMock(return_value=client)))
        for name in ("_create_bucket_for_intake", "_create_bucket_for_funding_review", "_create_bucket_for_main_street", "_create_bucket_for_mca_refi"):
            stack.enter_context(patch.object(core, name, AsyncMock(return_value=(bucket, link))))
        for name in ("_latest_active_intake_by_email", "_record_resume_email", "_record_super_admin_intake_notification"):
            stack.enter_context(patch.object(core, name, AsyncMock(return_value=None if name == "_latest_active_intake_by_email" else {"ok": True})))
        stack.enter_context(patch.object(core, "_load_public_intake", AsyncMock(side_effect=lambda *_: created[0])))
        stack.enter_context(patch.object(core, "_response", AsyncMock(return_value="created")))
        stack.enter_context(patch.object(core, "_throttle_or_429", Mock()))
        stack.enter_context(patch("app.services.onboarding_consent.record_for_start", AsyncMock()))
        provision = stack.enter_context(patch.object(core.profiles_service, "provision_profile_for_intake", AsyncMock()))
        assert await handler(payload, request, db) == "created"
    assert created[0].intake_state["business_profile"]["city"] == "Miami"
    assert created[0].intake_state["business_profile"]["website_none"] is True
    provision.assert_awaited_once_with(db, created[0])
