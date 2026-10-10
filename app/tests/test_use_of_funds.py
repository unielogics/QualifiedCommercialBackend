from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.use_of_funds import UseOfFundsItem, UseOfFundsPatch
from app.services import application_programs, use_of_funds
from app.services.program_rules import (
    ProgramRuleError,
    evaluate_recommendation_preferences,
    validate_rules,
)


def profile(**values):
    return SimpleNamespace(**{
        "id": uuid4(), "intake_id": None, "dealer_id": None, "loan_id": None,
        "deal_id": None, "use_of_funds": [], "use_of_funds_revision": 0,
        "use_of_funds_updated_at": None, "use_of_funds_updated_by_user_id": None,
        "underwriting_status": "collecting_docs", "program_selection_mode": "manual",
        **values,
    })


def item(category="real_estate", amount="510.00", key="item_1"):
    return UseOfFundsItem(id=key, category=category, label="Verified planned use", amount=amount)


@pytest.mark.parametrize("amount", [-1, "NaN", "Infinity", True, "1.001", "1000000000000"])
def test_budget_rejects_invalid_money(amount):
    with pytest.raises(ValidationError):
        item(amount=amount)


def test_budget_validates_stable_ids_categories_and_forbids_client_ratio():
    with pytest.raises(ValidationError):
        UseOfFundsPatch(items=[item(), item()], expected_revision=0)
    with pytest.raises(ValidationError):
        UseOfFundsPatch(items=[], expected_revision=0, real_estate_equipment_pct=100)
    with pytest.raises(ValidationError):
        item(category="AI inferred real estate")
    with pytest.raises(ValidationError):
        UseOfFundsPatch(items=[], expected_revision=-1)
    with pytest.raises(ValidationError):
        item(key=" ")
    with pytest.raises(ValidationError):
        UseOfFundsPatch(items=[item(key=f"row_{n}") for n in range(51)], expected_revision=0)


@pytest.mark.parametrize("re_amount,expected", [("509.90", 50.99), ("510.00", 51.0)])
def test_budget_uses_exact_cents_and_does_not_round_up_to_51(re_amount, expected):
    remaining = Decimal("1000") - Decimal(re_amount)
    result = use_of_funds.summarize(profile(), Decimal("1000"), "intake.requested_loan_amount", items=[
        item(amount=re_amount), item("working_capital", remaining, "item_2"),
    ])
    assert result.complete is True
    assert result.total == 1000
    assert result.unallocated_amount == 0
    assert result.real_estate_equipment_pct == expected
    assert result.category_totals["working_capital"] == float(remaining)


def test_partial_missing_zero_and_overallocated_budgets_have_no_routing_percentage():
    for amount in (None, Decimal(0), Decimal(100), Decimal(1000)):
        result = use_of_funds.summarize(profile(), amount, None, items=[item(amount=500)])
        assert result.complete is False
        assert result.real_estate_equipment_pct is None


def test_ai_budget_snapshot_excludes_free_text_and_changes_package_fingerprint():
    from app.routers.dealer_ai_intake import _package_input_fingerprint

    private_item = item(amount=1000).model_copy(update={"label": "Private hostile instructions"})
    saved = profile(use_of_funds_updated_by_user_id=uuid4())
    summary = use_of_funds.summarize(saved, Decimal(1000), "loan.amount", items=[private_item])
    context = use_of_funds.ai_context(summary)
    assert "Private hostile instructions" not in str(context)
    assert str(saved.use_of_funds_updated_by_user_id) not in str(context)
    assert context["basis"] == "declared_budget_not_verified_evidence"
    fingerprint = _package_input_fingerprint(source_metadata={}, context_snapshot={"use_of_funds": context}, financials={})
    context["revision"] += 1
    assert fingerprint != _package_input_fingerprint(source_metadata={}, context_snapshot={"use_of_funds": context}, financials={})


def test_legacy_prose_cannot_be_inferred_and_cleared_profile_never_resurrects_legacy():
    dealer = SimpleNamespace(use_of_proceeds=[{"label": "Buy a building", "amount": 1000}])
    result = use_of_funds.summarize(profile(), Decimal(1000), "dealer.funding_goal", dealer=dealer)
    assert result.items == []
    assert result.warnings
    dealer.use_of_proceeds = [item(amount=1000).model_dump(mode="json")]
    result = use_of_funds.summarize(profile(), Decimal(1000), "dealer.funding_goal", dealer=dealer)
    assert result.source == "legacy_dealer"
    assert result.complete
    result = use_of_funds.summarize(profile(use_of_funds_revision=1), Decimal(1000), None, dealer=dealer)
    assert result.items == []
    assert not result.complete


@pytest.mark.asyncio
async def test_source_amount_and_purpose_support_dealer_without_intake_and_loan():
    dealer = SimpleNamespace(client_requested_amount=None, funding_goal=Decimal(1000), funding_purpose="equipment")
    db = SimpleNamespace(get=AsyncMock(return_value=dealer))
    amount, source, purpose, _ = await use_of_funds.source_funding_data(db, profile(dealer_id=uuid4()))
    assert (amount, source, purpose) == (Decimal(1000), "dealer.funding_goal", "equipment")
    dealer.client_requested_amount = Decimal(2000)
    amount, source, _, _ = await use_of_funds.source_funding_data(db, profile(dealer_id=uuid4()))
    assert (amount, source) == (Decimal(2000), "dealer.client_requested_amount")
    db.get.return_value = SimpleNamespace(amount=Decimal(3000), purpose="purchase")
    amount, source, _, _ = await use_of_funds.source_funding_data(db, profile(loan_id=uuid4()))
    assert (amount, source) == (Decimal(3000), "loan.amount")
    db.get.return_value = SimpleNamespace(promoted_loan_id=None, target_price=999999)
    amount, source, _, _ = await use_of_funds.source_funding_data(db, profile(deal_id=uuid4()))
    assert amount is None and source is None


@pytest.mark.asyncio
async def test_linked_intake_request_takes_precedence_without_altering_sources():
    intake = SimpleNamespace(requested_loan_amount=Decimal(1000), loan_purpose="purchase")
    dealer = SimpleNamespace(client_requested_amount=500, funding_goal=2000)
    async def get(model, _id):
        return intake if model is PublicUnderwritingIntake else dealer
    db = SimpleNamespace(get=AsyncMock(side_effect=get))
    amount, source, purpose, _ = await use_of_funds.source_funding_data(db, profile(intake_id=uuid4(), dealer_id=uuid4()))
    assert (amount, source, purpose) == (Decimal(1000), "intake.requested_loan_amount", "purchase")
    assert dealer.client_requested_amount == 500


@pytest.mark.asyncio
async def test_dealer_only_fit_context_exposes_shared_budget_and_source_purpose():
    saved = ApplicationProfile(
        id=uuid4(), dealer_id=uuid4(), vertical="dealer",
        use_of_funds=[item("equipment", "510", "equipment").model_dump(mode="json"),
                      item("working_capital", "490", "working_capital").model_dump(mode="json")],
    )
    dealer = SimpleNamespace(client_requested_amount=None, funding_goal=Decimal(1000), funding_purpose="equipment")
    db = SimpleNamespace(
        get=AsyncMock(return_value=dealer),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: None,
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )
        ),
    )
    with patch.object(application_programs, "_evidence_inventory", AsyncMock(return_value=([], {}, set()))):
        context = await application_programs.profile_fit_context(db, saved)
    assert context["requested_amount"] == 1000
    assert context["loan_purpose"] == "equipment"
    assert context["equipment_financing_intent"] is True
    assert context["use_of_funds_total"] == 1000
    assert context["real_estate_equipment_amount"] == 510
    assert context["real_estate_equipment_pct"] == 51
    assert context["use_of_funds_complete"] is True


@pytest.mark.asyncio
async def test_update_rejects_overallocation_and_stale_revision_without_mutation():
    saved = profile()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: saved)), flush=AsyncMock())
    user = SimpleNamespace(id=uuid4(), role=Role.FIELD_REP)
    with patch.object(use_of_funds, "source_funding_data", AsyncMock(return_value=(Decimal(100), "loan.amount", None, None))):
        with pytest.raises(HTTPException) as error:
            await use_of_funds.update_budget(db, saved, UseOfFundsPatch(items=[item(amount=101)], expected_revision=0), user)
        assert error.value.status_code == 422
        with pytest.raises(HTTPException) as error:
            await use_of_funds.update_budget(db, saved, UseOfFundsPatch(items=[], expected_revision=1), user)
        assert error.value.status_code == 409
    assert saved.use_of_funds == []
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_audits_budget_without_advancing_status_or_changing_manual_selection():
    saved = profile()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: saved)), flush=AsyncMock())
    user = SimpleNamespace(id=uuid4(), role=Role.FIELD_REP)
    with (
        patch.object(use_of_funds, "source_funding_data", AsyncMock(return_value=(Decimal(1000), "loan.amount", None, None))),
        patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit,
    ):
        result = await use_of_funds.update_budget(db, saved, UseOfFundsPatch(items=[item(amount=1000)], expected_revision=0), user)
    assert result.complete
    assert result.revision == 1
    assert saved.use_of_funds_updated_by_user_id == user.id
    assert saved.underwriting_status == "collecting_docs"
    assert saved.program_selection_mode == "manual"
    audit.assert_awaited_once()


@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER, Role.LENDER, Role.VENDOR])
def test_external_roles_cannot_edit_budget(role):
    with pytest.raises(HTTPException) as error:
        use_of_funds.require_editor(SimpleNamespace(role=role))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_routes_enforce_profile_visibility_for_read_and_write():
    from app.routers.application_profiles import (
        get_application_use_of_funds,
        patch_application_use_of_funds,
    )
    user = SimpleNamespace(role=Role.FIELD_REP, id=uuid4())
    db = SimpleNamespace(commit=AsyncMock())
    with patch("app.services.application_profiles.load_profile", AsyncMock(side_effect=HTTPException(404, "Application file not found"))):
        with pytest.raises(HTTPException) as error:
            await get_application_use_of_funds(uuid4(), user, db)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await patch_application_use_of_funds(uuid4(), UseOfFundsPatch(items=[], expected_revision=0), user, db)
        assert error.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,has_pinned", [("manual", False), ("auto", True), ("manual", True)])
async def test_preference_changes_never_reselect_manual_or_pinned_programs(mode, has_pinned):
    pinned = [SimpleNamespace(id=uuid4(), playbook_version=1)] if has_pinned else []
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    with patch.object(application_programs, "active_selections", AsyncMock(return_value=pinned)):
        result = await application_programs._auto_select(db, profile(program_selection_mode=mode), [])
    assert result == pinned
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


def preference_rules():
    return {"fit": {"field": "requested_amount", "op": "lte", "value": 500000}, "recommendation_preferences": [{
        "key": "re_equipment", "label": "QC budget preference", "score": 50,
        "when": {"all": [{"field": "use_of_funds_complete", "op": "eq", "value": True},
                         {"field": "real_estate_equipment_pct", "op": "gte", "value": 51}]},
    }]}


@pytest.mark.parametrize("percentage,complete,score", [(50.99, True, 0), (51, True, 50), (None, False, 0), (100, False, 0)])
def test_preference_threshold_requires_complete_budget(percentage, complete, score):
    assert evaluate_recommendation_preferences(preference_rules(), {
        "real_estate_equipment_pct": percentage, "use_of_funds_complete": complete,
    })[0] == score


@pytest.mark.parametrize("change", [
    {"score": True}, {"score": 101}, {"score": 0}, {"score": 2.5},
    {"label": " "}, {"label": "x" * 201}, {"key": "Unsafe-KEY"},
    {"when": {"field": "made_up", "op": "eq", "value": 1}},
    {"when": {"field": "requested_amount", "op": "gte", "value": float("nan")}},
])
def test_preference_validation_is_bounded_even_without_fit(change):
    row = preference_rules()["recommendation_preferences"][0] | change
    with pytest.raises(ProgramRuleError):
        validate_rules({"recommendation_preferences": [row]})


def test_duplicate_preferences_and_unknown_negations_cannot_boost():
    row = preference_rules()["recommendation_preferences"][0]
    with pytest.raises(ProgramRuleError):
        validate_rules({"recommendation_preferences": [row, row]})
    with pytest.raises(ProgramRuleError):
        validate_rules({"recommendation_preferences": [row] * 11})
    row["when"] = {"not": {"field": "real_estate_equipment_pct", "op": "lt", "value": 51}}
    assert evaluate_recommendation_preferences({"recommendation_preferences": [row]}, {}) == (0, [])


@pytest.mark.asyncio
async def test_preference_orders_only_eligible_published_candidates_not_amount_failures():
    keys = ["standard", "preferred", "ineligible", "unpublished"]
    catalog = [SimpleNamespace(id=uuid4(), program_key=key, name=key, public_slug=key) for key in keys]
    versions = {}
    for row in catalog[:3]:
        rules = preference_rules()
        if row.program_key == "standard":
            rules.pop("recommendation_preferences")
            rules["priority"] = 9999
        elif row.program_key == "ineligible":
            rules["fit"]["value"] = 99
        versions[row.id] = SimpleNamespace(id=uuid4(), version=1, rules=rules)
    with (
        patch.object(application_programs, "profile_fit_context", AsyncMock(return_value={"requested_amount": 1000, "use_of_funds_complete": True, "real_estate_equipment_pct": 51})),
        patch.object(application_programs.program_catalog, "catalog_rows", AsyncMock(return_value=catalog)),
        patch.object(application_programs.program_catalog, "scopes_by_program", AsyncMock(return_value={})),
        patch.object(application_programs.program_catalog, "published_versions_by_program", AsyncMock(return_value=versions)),
        patch.object(application_programs, "_catalog_scope_match", Mock(return_value=(True, []))),
    ):
        candidates = await application_programs.published_candidates(SimpleNamespace(), profile())
    assert [row.program_key for row in candidates] == ["preferred", "standard", "unpublished", "ineligible"]
    assert candidates[0].preference_score == 50
    assert candidates[0].preference_reasons == ["QC budget preference"]
    assert candidates[-1].eligible is False
    assert candidates[-1].preference_score == 0
