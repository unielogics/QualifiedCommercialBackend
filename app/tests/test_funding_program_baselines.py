from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.routers import funding_programs as routes
from app.schemas.funding_program import FundingProgramVersionCreate
from app.schemas.funding_program_baseline import FundingProgramBaselineRead
from app.services.funding_program_baselines import CATALOG, suggested_baseline
from app.services.program_rules import validate_rules


@pytest.mark.parametrize("key", CATALOG)
def test_every_baseline_is_a_valid_detached_reviewable_draft(key):
    data = suggested_baseline(key)
    baseline = FundingProgramBaselineRead.model_validate(data)
    draft = FundingProgramVersionCreate(
        rules=baseline.rules,
        requirements=baseline.requirements,
        confirmed=True,
        reason="Administrator reviews suggested baseline",
    )
    validate_rules(draft.rules, enforce_supported_fields=True)
    assert baseline.needs_review
    assert baseline.source_urls[0].startswith("https://qualifiedcommercial.com/")
    assert all(
        note.startswith(("Website:", "Proposed QC policy:", "Review:"))
        for note in baseline.source_notes
    )
    assert len({row.requirement_key for row in baseline.requirements}) == len(baseline.requirements)
    assert all(row.verification_required for row in baseline.requirements)
    assert all(row.completion_mode == "requires_human_verify" for row in baseline.requirements)
    assert all({"5221", "524"}.issubset(scope.excluded_naics_prefixes) for scope in baseline.scopes)
    assert baseline.rules["baseline"]["source_urls"] == baseline.source_urls
    data["requirements"][0]["label"] = "Changed by user"
    data["scopes"][0]["excluded_naics_prefixes"].append("11")
    fresh = suggested_baseline(key)
    assert fresh["requirements"][0]["label"] != "Changed by user"
    assert "11" not in fresh["scopes"][0]["excluded_naics_prefixes"]


def test_custom_baseline_preserves_routes_and_explains_missing_source():
    scopes = [
        {
            "vertical": "dealer",
            "scope_key": "bespoke",
            "intent_keys": ["expansion"],
            "naics_prefixes": ["4411"],
            "excluded_naics_prefixes": ["441110"],
        }
    ]
    before = deepcopy(scopes)
    baseline = FundingProgramBaselineRead.model_validate(
        suggested_baseline("custom_product", name="Special product", current_scopes=scopes)
    )
    assert scopes == before
    assert baseline.scopes[0].scope_key == "bespoke"
    assert baseline.scopes[0].naics_prefixes == ["4411"]
    assert baseline.scopes[0].excluded_naics_prefixes == ["441110", "5221", "524"]
    assert "fit" not in baseline.rules
    assert any("no verified" in note for note in baseline.source_notes)


def test_mca_and_insurance_proposals_are_not_claimed_as_universal_sba_rules():
    data = suggested_baseline("sba_7a")
    assert not any(
        rule["field"] == "mca_obligations_present" for rule in data["rules"]["fit"]["all"]
    )
    assert any("broader house restriction" in note for note in data["source_notes"])
    assert any("/sba/auto" in note for note in data["source_notes"])
    assert any("October 1, 2026" in note for note in data["source_notes"])


def test_sba_504_majority_preference_is_ordering_policy_not_eligibility():
    data = suggested_baseline("sba_504")
    assert data["rules"]["fit"] == {
        "all": [
            {"field": "business_age_years", "op": "gte", "value": 2},
            {"field": "credit_score", "op": "gte", "value": 680},
            {"field": "dscr", "op": "gte", "value": 1.2},
        ]
    }
    assert data["rules"]["recommendation_preferences"] == [
        {
            "key": "real_estate_equipment_majority",
            "label": "QC preference: at least 51% real estate or equipment",
            "when": {"field": "real_estate_equipment_pct", "op": "gte", "value": 51},
            "score": 50,
        }
    ]
    assert any("recommendation ordering only" in note for note in data["source_notes"])
    assert any("not eligibility, approval, or an SBA occupancy test" in note for note in data["source_notes"])
    assert any("separate program and structure review" in note for note in data["source_notes"])


def test_sba_express_uses_current_500k_cap_not_outdated_350k_threshold():
    data = suggested_baseline("sba_express")
    assert {
        "field": "requested_amount",
        "op": "lte",
        "value": 500_000,
    } in data["rules"]["fit"]["all"]
    assert not any(
        item.get("field") == "requested_amount" and item.get("value") == 350_000
        for item in data["rules"]["fit"]["all"]
    )
    assert any("$500,000" in note for note in data["source_notes"])


def test_product_specific_website_differences_are_preserved():
    ez = suggested_baseline("ez_term")
    micro = suggested_baseline("microcap")
    rbf = suggested_baseline("revenue_based_financing")
    assert "4561" in ez["scopes"][0]["excluded_naics_prefixes"]
    assert "4461" not in ez["scopes"][0]["excluded_naics_prefixes"]
    assert "441222" in micro["scopes"][0]["excluded_naics_prefixes"]
    assert "4412" not in micro["scopes"][0]["excluded_naics_prefixes"]
    assert not any(rule["field"] == "annual_revenue" for rule in rbf["rules"]["fit"]["all"])
    assert any(
        check["key"] == "custom_monthly_deposits"
        for doc in rbf["requirements"]
        for check in doc["review_checks"]
    )
    assert (
        next(
            row
            for row in ez["requirements"]
            if row["requirement_key"] == "business_tax_returns_1_years"
        )["blocks_stage"]
        == "closing"
    )


def test_requested_tax_patterns_are_explicit_proposed_policy_not_auto_acceptance():
    row = next(
        item
        for item in suggested_baseline("line_of_credit")["requirements"]
        if item["requirement_key"] == "business_tax_returns_2_years"
    )
    assert {item["key"] for item in row["review_checks"]} == {
        "net_income_nonnegative",
        "net_income_not_declining",
    }
    assert all(
        item["instructions"].startswith("Proposed QC policy:") for item in row["review_checks"]
    )
    assert row["verification_required"]


@pytest.mark.asyncio
async def test_reading_baseline_never_writes_or_commits(monkeypatch):
    program = SimpleNamespace(id=uuid4(), name="SBA 7(a)")
    monkeypatch.setattr(
        routes.funding_programs, "catalog_item_or_404", AsyncMock(return_value=program)
    )
    monkeypatch.setattr(routes.funding_programs, "scopes_by_program", AsyncMock(return_value={}))
    db = SimpleNamespace(add=AsyncMock(), flush=AsyncMock(), commit=AsyncMock())
    response = await routes.get_funding_program_baseline("sba_7a", _=SimpleNamespace(), db=db)
    assert response.program_key == "sba_7a"
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(Role))
async def test_baseline_http_endpoint_is_super_admin_only(monkeypatch, role):
    program = SimpleNamespace(id=uuid4(), name="SBA 7(a)")
    lookup = AsyncMock(return_value=program)
    monkeypatch.setattr(routes.funding_programs, "catalog_item_or_404", lookup)
    monkeypatch.setattr(routes.funding_programs, "scopes_by_program", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(routes.admin_router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=role)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/admin/funding-programs/sba_7a/baseline")
    assert result.status_code == (200 if role == Role.SUPER_ADMIN else 403)
    if role == Role.SUPER_ADMIN:
        assert result.json()["needs_review"] is True
    else:
        lookup.assert_not_called()


def test_staged_documents_and_nonwaivable_sba_eligibility():
    def by_key(program):
        return {row["requirement_key"]: row for row in suggested_baseline(program)["requirements"]}

    micro = by_key("microcap")
    assert micro["micro_commitment_package"]["blocks_stage"] == "underwriting"
    assert micro["micro_closing_package"]["blocks_stage"] == "closing"
    assert micro["sba_eligibility_and_proceeds"]["can_underwriter_waive"] is False
    assert any(
        check["key"] == "custom_micro_working_capital_only"
        for row in micro.values()
        for check in row["review_checks"]
    )
    assert (
        by_key("revenue_based_financing")["owner_identification"]["blocks_stage"] == "underwriting"
    )
    assert "ez_bank_verification_payoffs" in by_key("ez_term")
