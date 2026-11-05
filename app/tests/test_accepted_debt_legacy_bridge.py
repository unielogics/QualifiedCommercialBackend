from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.dealer_os import router
from app.dealer_os.models import DealerApplicationProfile, DealerBusiness, DealerDebt, DealerOwner
from app.dealer_os.schemas import DebtScheduleConfirmationRequest
from app.dealer_os.services import (
    contract_fill,
    contract_packages,
    qc_master_application,
    workflow_readiness,
)
from app.dealer_os.services.engines import MetricInputs
from app.services import financial_version_selection as selection
from app.tests.test_accepted_debt_precedence import Result, obligation, published
from app.tests.test_qc_master_application_pdf import _sample_context


def version(*rows, no_debt=False):
    return SimpleNamespace(id=uuid4(), version_number=4, actor_user_id=uuid4(),
        created_at=datetime(2026, 11, 5, 10, tzinfo=UTC), body_sha256="a" * 64,
        body={"debts": list(rows), "no_debt": no_debt})


@pytest.mark.asyncio
@pytest.mark.parametrize("no_debt", [False, True])
async def test_saved_schedule_reads_as_confirmed_without_manual_confirmation_or_extra_projected_lenders(no_debt):
    rows = [] if no_debt else [obligation(monthly_payment=str(100 * value)) for value in range(1, 6)]
    accepted = version(*rows, no_debt=no_debt)
    projected = [published(row) for row in rows] + [published(obligation(lender=f"Other bank vendor {index}")) for index in range(20)]
    db = SimpleNamespace(execute=AsyncMock(return_value=Result(projected)), add=Mock(), flush=AsyncMock())
    dealer = SimpleNamespace(id=uuid4())
    before = deepcopy(accepted.body)
    with patch.object(router, "require_team_or_dealer_or_rep"), patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=dealer)), patch.object(
        selection, "accepted_debt_schedule_snapshot_for_dealer", AsyncMock(return_value=accepted)
    ):
        result = await router.get_debt_schedule_confirmation(dealer.id, SimpleNamespace(), db)
    assert result.confirmed and not result.stale
    assert result.status == ("no_business_debt" if no_debt else "schedule_confirmed")
    assert result.confirmed_by_user_id == accepted.actor_user_id and result.confirmed_at == accepted.created_at
    assert result.financial_version_id == accepted.id and result.financial_version_number == 4
    assert result.financial_version_content_hash == accepted.body_sha256 and result.source_sha256
    assert accepted.body == before
    # Accepted version reads do not consult or rewrite legacy manual confirmations.
    db.execute.assert_awaited_once()
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_redundant_legacy_confirm_action_does_not_change_saved_authority():
    accepted = version(obligation())
    db = SimpleNamespace(execute=AsyncMock(return_value=Result([])), add=Mock(), commit=AsyncMock())
    dealer = SimpleNamespace(id=uuid4())
    with patch.object(router, "require_team_or_rep"), patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=dealer)), patch.object(
        selection, "accepted_debt_schedule_snapshot_for_dealer", AsyncMock(return_value=accepted)
    ):
        result = await router.put_debt_schedule_confirmation(dealer.id,
            DebtScheduleConfirmationRequest(status="no_business_debt"), SimpleNamespace(), db)
    assert result.confirmed and result.status == "schedule_confirmed" and result.financial_version_id == accepted.id
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("no_debt", [False, True])
async def test_master_context_uses_saved_cohort_and_monthly_even_over_manual_stale_debt(no_debt):
    saved = obligation(monthly_payment="1000", maturity_on="2027-12-31")
    accepted = version(*([] if no_debt else [saved]), no_debt=no_debt)
    dealer = DealerBusiness(id=uuid4(), name="Example LLC", funding_goal=100000)
    profile = DealerApplicationProfile(dealer_id=dealer.id, annual_cash_flow_available_for_debt=120000,
        monthly_debt_payments=99999, annual_sales=500000, field_confirmations={"annual_sales": {"value": 500000}})
    original = deepcopy(profile.field_confirmations)
    async def execute(statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is DealerApplicationProfile:
            return Result(scalar=profile)
        if entity is DealerDebt:
            return Result([published(saved)] + [published(obligation(lender=f"Extra observed vendor {i}")) for i in range(30)])
        return Result()
    db = SimpleNamespace(execute=AsyncMock(side_effect=execute), add=Mock(), flush=AsyncMock())
    with patch.object(selection, "accepted_debt_schedule_snapshot_for_dealer", AsyncMock(return_value=accepted)):
        context = await qc_master_application.build_context(db, dealer,
            routing_result={"calculated_metrics": {"dscr": 99}},
            financial_snapshot={"monthly_debt_payments": 88888, "dscr": 88,
                "sources": {"dscr": {"source": "identified_bank_activity", "label": "Old observed estimate", "status": "estimated"}}})
    assert context["debt_schedule_authoritative"]
    assert len(context["debts"]) == (0 if no_debt else 1)
    assert context["financial"]["monthly_debt_payments"] == (0 if no_debt else 1000)
    assert context["financial"]["annual_cash_flow_available_for_debt"] == 120000
    assert context["financial"]["dscr"] == (None if no_debt else 10)
    provenance = context["financial"]["sources"]["dscr"]
    assert provenance["source"] == "accepted_debt_schedule"
    assert provenance["label"] == context["financial"]["dscr_source"]
    assert provenance["version_id"] == str(accepted.id) and provenance["version_number"] == 4
    assert provenance["content_hash"] == accepted.body_sha256
    assert provenance["status"] == ("unavailable" if no_debt else "estimated")
    assert context["debt_schedule_confirmation"]["financial_version_id"] == str(accepted.id)
    assert profile.monthly_debt_payments == 99999 and profile.field_confirmations == original
    if not no_debt:
        assert context["debts"][0]["maturity"] == "December 31, 2027"
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.parametrize("no_debt", [False, True])
def test_master_readiness_accepts_saved_debt_without_separate_confirmation(no_debt):
    context = _sample_context()
    context["profile"].field_confirmations.pop("monthly_debt_payments")
    context["profile"].field_confirmations.pop("debt_schedule")
    context["debt_schedule_authoritative"] = True
    if no_debt:
        context["debts"] = []
        context["financial"]["monthly_debt_payments"] = 0
    readiness = qc_master_application.build_readiness(context)
    debt = next(row for row in readiness["items"] if row["requirement"] == "Debt schedule for refinance")
    financial = next(row for row in readiness["items"] if row["requirement"] == "Confirmed cash flow and monthly debt service")
    assert debt["status"] == financial["status"] == "complete"
    context["profile"].field_confirmations.pop("annual_cash_flow_available_for_debt")
    missing = qc_master_application.build_readiness(context)
    assert next(row for row in missing["items"] if row["requirement"] == financial["requirement"])["status"] == "missing"


def test_package_and_workflow_gates_accept_only_debt_confirmation_from_saved_version():
    profile = SimpleNamespace(field_confirmations={"annual_sales": {}, "annual_cash_flow_available_for_debt": {}},
                              annual_sales=500000, annual_cash_flow_available_for_debt=120000, monthly_debt_payments=None)
    assert contract_packages._financial_confirmation_missing(profile, accepted_debt_schedule=True) == set()
    assert workflow_readiness.financial_confirmation_blockers(profile, accepted_debt_schedule=True) == []
    assert "monthly debt payment confirmation" in contract_packages._financial_confirmation_missing(profile)
    profile.field_confirmations = {}
    assert contract_packages._financial_confirmation_missing(profile, accepted_debt_schedule=True) == {
        "annual sales confirmation", "annual cash flow confirmation",
    }
    assert workflow_readiness.financial_confirmation_blockers(profile, accepted_debt_schedule=True) == [
        "Confirm annual sales.", "Confirm annual cash flow available for debt.",
    ]


@pytest.mark.parametrize("accepted,ratio,expected", [(True, 10, 10), (True, None, None), (False, 10, 1)])
def test_contract_population_cannot_replace_saved_schedule_dscr_with_stale_manual_monthly(accepted, ratio, expected):
    profile = SimpleNamespace(annual_cash_flow_available_for_debt=Decimal("120000"), monthly_debt_payments=Decimal("10000"))
    assert contract_fill._application_dscr(profile, {"debt_schedule_authoritative": accepted, "dscr": ratio}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("no_debt", [False, True])
async def test_live_field_desk_routing_uses_saved_monthly_and_ignores_outside_mca_and_ucc_rows(no_debt):
    saved = obligation(monthly_payment="1000", evidence={"ucc": True})
    dealer = DealerBusiness(id=uuid4(), name="Example LLC", funding_goal=100000)
    profile = DealerApplicationProfile(dealer_id=dealer.id, annual_cash_flow_available_for_debt=120000,
        monthly_debt_payments=99999, annual_sales=500000, field_confirmations={})
    owner = DealerOwner(id=uuid4(), dealer_id=dealer.id, credit_score=742, ownership_pct=100)
    owner_state = {"owners": [owner], "required": [owner]}
    rows = [] if no_debt else [saved]
    inputs = MetricInputs([], [], [], [], {}, {"debt_schedule_authoritative": True,
        "debt_schedule_monthly": 0 if no_debt else 1000}, rows)
    projected = [published(saved)]
    for index in range(30):
        row = published(obligation(lender=f"Other vendor {index}", debt_type="mca", monthly_payment="9000"))
        row.category, row.evidence = "mca", {"ucc": True}
        projected.append(row)
    async def execute(statement):
        entity = statement.column_descriptions[0].get("entity")
        return Result(scalar=profile) if entity is DealerApplicationProfile else Result(projected) if entity is DealerDebt else Result()
    with patch.object(router, "load_metric_inputs", AsyncMock(return_value=inputs)), patch.object(
        router, "_statement_month_coverage", AsyncMock(return_value=([], [], None))
    ), patch.object(router.application_prescreen, "owner_answer_complete", return_value=True), patch.object(
        router.application_prescreen, "screen_application", return_value={"programs": []}
    ) as screen, patch.object(router.application_prescreen, "applicable_business_questions", return_value=[]), patch.object(
        router.application_prescreen, "business_answer_blockers", return_value=[]
    ):
        state = await router._application_pre_screen_state(SimpleNamespace(execute=AsyncMock(side_effect=execute)), dealer, owner_state)
    facts = screen.call_args.kwargs["application_facts"]
    assert facts["monthly_debt_payments"] == (0 if no_debt else 1000)
    assert facts["mca_count"] == 0 and facts["active_ucc_count"] == (0 if no_debt else 1)
    assert state["financial_snapshot"]["monthly_debt_payments"] == facts["monthly_debt_payments"]
    assert profile.monthly_debt_payments == 99999 and profile.field_confirmations == {}
