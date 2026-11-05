from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest

from app.dealer_os import router
from app.dealer_os.models import DealerCashEvent, DealerDebt, DealerFinancialPeriod
from app.dealer_os.services import engines, extract
from app.dealer_os.services import financial_debt_drafts as drafts
from app.services import application_profiles, capital_readiness
from app.services import financial_version_selection as selection


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


def obligation(**changes):
    return {"id": str(uuid4()), "lender": "Shared Lender", "debt_type": "loan", "origin": "client_form",
            "status": "active", "balance": "10000", "monthly_payment": "100", "count_in_dscr": True,
            "vendor_key": "SHARED LENDER", **changes}


def published(raw, **changes):
    return SimpleNamespace(id=UUID(raw["id"]), lender=raw["lender"], monthly_payment=Decimal(raw["monthly_payment"]),
                           vendor_key=raw.get("vendor_key"), evidence={}, **changes)


def period(**changes):
    return SimpleNamespace(**{"id": uuid4(), "account_id": None, "period": date(2026, 10, 1),
        "debt_service": 60000, "ebitda_reported": 10000, "deposits": 20000, "withdrawals": 15000,
        "avg_daily_balance": 8000, "starting_balance": 8000, "ending_balance": 8000,
        "low_balance": 4000, "nsf_count": 0, **changes})


def engine_db(periods, debts=(), events=()):
    async def execute(statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is DealerFinancialPeriod:
            return Result(periods)
        if entity is DealerDebt:
            return Result(debts if statement.column_descriptions[0]["name"] == "DealerDebt" else [])
        if entity is DealerCashEvent:
            return Result(events)
        return Result()
    return SimpleNamespace(execute=AsyncMock(side_effect=execute))


@pytest.mark.asyncio
async def test_saved_five_obligations_override_many_bank_vendors_and_period_debt():
    rows = [obligation(monthly_payment=str(value)) for value in (100, 200, 300, 400, 500)]
    rows += [obligation(status="duplicate", duplicate_of_id=rows[0]["id"], monthly_payment="99999"),
             obligation(status="dismissed", monthly_payment="99999"),
             obligation(monthly_payment="99999", count_in_dscr=False)]
    version = SimpleNamespace(body={"debts": rows})
    stale_projection = [published(row) for row in rows]
    stale_projection += [published(obligation(lender=f"Other lender {index}", monthly_payment="9999")) for index in range(30)]
    bank_events = [SimpleNamespace(description=f"ACH lender {index}", amount=-9900,
        period=date(2026, 10, 1), occurred_on=date(2026, 10, 5)) for index in range(30)]
    db = engine_db([period()], stale_projection, bank_events)
    with patch.object(selection, "accepted_debt_schedule_snapshot_for_dealer", AsyncMock(return_value=version)):
        inputs = await engines.load_metric_inputs(db, uuid4())
        removed = await engines.load_metric_inputs(db, uuid4(), exclude_debt_ids={UUID(rows[0]["id"])})
    metrics = engines.compute_metrics(inputs.periods, 0, {}, inputs.fallbacks)
    assert metrics["dscr"]["source"] == "accepted_debt_schedule"
    assert metrics["dscr"]["monthly_debt_service"] == 1500
    assert metrics["dscr"]["current"] == pytest.approx(6.4)
    assert metrics["dscr"]["draft"] is None
    assert removed.fallbacks["debt_schedule_monthly"] == 1400
    assert not any(call.args[0].column_descriptions[0].get("entity") is DealerCashEvent for call in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_completed_no_debt_preserves_zero_and_cannot_fall_through_to_observed_or_draft():
    with patch.object(selection, "accepted_debt_schedule_snapshot_for_dealer", AsyncMock(return_value=SimpleNamespace(body={"debts": [], "no_debt": True}))):
        inputs = await engines.load_metric_inputs(engine_db([period()]), uuid4())
    # Defend the pure engine too, even if another caller supplies old observations.
    inputs.fallbacks.update(debt_service_observed_monthly=50000, debt_service_draft_monthly=90000)
    result = engines.compute_metrics(inputs.periods, 0, {}, inputs.fallbacks)["dscr"]
    assert inputs.accepted_debt_rows == []
    assert result["monthly_debt_service"] == 0
    assert result["current"] is result["draft"] is result["cash_flow"] is None
    assert result["source"] == "accepted_debt_schedule"


@pytest.mark.parametrize("period_ds,expected,source", [(6000, 6000, "periods"), (None, 2500, "schedule+observed")])
def test_no_complete_save_retains_legacy_automation_precedence(period_ds, expected, source):
    result = engines.compute_metrics([{"ebitda_reported": 10000, "debt_service": period_ds}], 0, {}, {
        "debt_schedule_authoritative": False, "debt_schedule_monthly": 1000,
        "debt_service_observed_monthly": 1500, "debt_service_draft_monthly": 3000,
    })["dscr"]
    assert result["monthly_debt_service"] == expected and result["source"] == source
    assert result["draft"] is not None


@pytest.mark.asyncio
async def test_no_complete_save_keeps_observed_bank_vendor_automation_active():
    bank_events = [SimpleNamespace(description="ACH DEBIT NEW BANK LENDER", amount=-1500,
        period=date(2026, 10, 1), occurred_on=date(2026, 10, 5))]
    db = engine_db([period(debt_service=None)], events=bank_events)
    rolled = [SimpleNamespace(key="NEW BANK LENDER", debt_like=True, category="loan")]
    with patch.object(selection, "accepted_debt_schedule_snapshot_for_dealer", AsyncMock(return_value=None)), patch(
        "app.dealer_os.services.vendors.rollup_vendors", return_value=rolled
    ):
        inputs = await engines.load_metric_inputs(db, uuid4())
    assert inputs.accepted_debt_rows is None
    result = engines.compute_metrics(inputs.periods, 0, {}, inputs.fallbacks)["dscr"]
    assert result["monthly_debt_service"] == 1500 and result["source"] == "observed_ledger"
    assert result["draft"] is not None


@pytest.mark.asyncio
async def test_accepted_schedule_automatic_proposals_are_read_only_recoverable_evidence():
    saved = obligation()
    body = {"debts": [saved]}
    managed = drafts.ManagedDebtDraft(SimpleNamespace(id=uuid4()), {
        "body": deepcopy(body), "family_id": str(uuid4()), "draft": {"dirty": False, "revision": 7},
    })
    proposals = [{"lender": saved["lender"], "monthly_payment": 9999},
                 {"lender": "New observed lender", "monthly_payment": 8888}]
    with patch.object(selection, "accepted_debt_schedule_snapshot", AsyncMock(return_value=SimpleNamespace(body=body))), patch.object(
        drafts.financial_workspace, "save_draft", AsyncMock()
    ) as save:
        result = await drafts.stage_proposals(SimpleNamespace(), managed, proposals,
            fields=("monthly_payment",), document_id=uuid4())
    assert result["schedule_authoritative"] and result["created"] == result["updated"] == 0
    assert result["draft_revision"] == 7 and not result["draft_pending"]
    assert result["total_monthly"] == 100 and len(result["debts"]) == 1
    assert [item["monthly_payment"] for item in result["suggestions"]] == [9999, 8888]
    assert managed.snapshot["body"] == body
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_autosaved_incomplete_draft_still_receives_automatic_proposals_before_first_complete_save():
    managed = drafts.ManagedDebtDraft(SimpleNamespace(id=uuid4()), {
        "body": {"debts": []}, "family_id": str(uuid4()), "draft": {"dirty": True, "revision": 7},
    })
    async def save_draft(_db, context, body, **_kwargs):
        context.snapshot["body"] = body
    with patch.object(selection, "accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), patch.object(
        drafts, "save_body", AsyncMock(side_effect=save_draft)
    ) as save:
        result = await drafts.stage_proposals(SimpleNamespace(), managed, [{"lender": "Observed lender", "monthly_payment": 100}], fields=("monthly_payment",))
    assert result["created"] == 1 and managed.snapshot["body"]["debts"][0]["monthly_payment"] == 100
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_extraction_cannot_bypass_saved_authority_when_managed_adapter_unavailable():
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), flush=AsyncMock())
    notes = []
    with patch.object(drafts, "managed_context", AsyncMock(return_value=None)), patch.object(
        selection, "accepted_debt_schedule_authority_for_dealer", AsyncMock(return_value=True)
    ):
        assert await extract._route_debt_rows(db, uuid4(), uuid4(), [{"lender": "New lender", "monthly_payment": 100}], notes) == 0
        assert await extract._route_debt_schedule(db, uuid4(), [{"lender": "New lender", "monthly_payment": 100}], notes) == 0
    db.execute.assert_not_awaited()
    db.add.assert_not_called()
    assert all("saved debt schedule" in note for note in notes)


@pytest.mark.asyncio
async def test_legacy_financial_debt_list_is_saved_only_with_live_payee_annotation():
    saved = obligation()
    draft_only = obligation(lender="Unsaved extra", monthly_payment="9999")
    managed = drafts.ManagedDebtDraft(SimpleNamespace(id=uuid4()), {
        "body": {"debts": [draft_only]}, "family_id": str(uuid4()), "draft": {"dirty": True, "revision": 9},
    })
    live = published(saved)
    live.monthly_payment = 777
    live.vendor_key = "BANKING PAYEE"
    db = SimpleNamespace(execute=AsyncMock(return_value=Result([live, published(draft_only)])))
    with patch.object(router, "require_team_or_dealer_or_rep"), patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=SimpleNamespace(id=uuid4()))), patch.object(
        drafts, "managed_context", AsyncMock(return_value=managed)
    ), patch.object(selection, "accepted_debt_schedule_snapshot", AsyncMock(return_value=SimpleNamespace(body={"debts": [saved]}))):
        result = await router.list_debts(uuid4(), SimpleNamespace(), db)
    assert len(result) == 1 and result[0].id == UUID(saved["id"])
    assert result[0].monthly_payment == 100 and result[0].vendor_key == "BANKING PAYEE"
    assert not result[0].draft_pending


@pytest.mark.asyncio
async def test_dscr_composition_shows_saved_amount_with_observations_separate_and_no_vendor_additions():
    saved = obligation()
    live = published(saved, category="loan", origin="client_form", document_id=None, count_in_dscr=True)
    outsider = published(obligation(lender="Bank suggested extra", monthly_payment="9000"), category="loan", origin="ai_draft", document_id=None, count_in_dscr=True)
    dealer = SimpleNamespace(id=uuid4(), funding_goal=None)
    inputs = engines.MetricInputs([], [{"period": date(2026, 10, 1), "ebitda_reported": 10000}], [], [], {},
        {"debt_schedule_authoritative": True, "debt_schedule_monthly": 100}, [saved])
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result([live, outsider]), Result([])]))
    vendor = SimpleNamespace(key="NEW BANK LENDER", debt_like=True, category="loan", is_recurring=True,
        sample_description="New Bank Lender", monthly_average=9000, months=6, count=6)
    with patch.object(router, "require_team_or_dealer_or_rep"), patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=dealer)), patch.object(
        router, "load_metric_inputs", AsyncMock(return_value=inputs)
    ), patch.object(router, "_load_vendor_inputs", AsyncMock(return_value=([], {}, []))), patch.object(
        router.refinance_svc, "observed_monthly", return_value={live.id: {date(2026, 10, 1): 9999}}
    ), patch.object(router, "_rollup_from_inputs", return_value=([vendor], 1)):
        result = await router.dscr_composition(dealer.id, SimpleNamespace(), db)
    assert len(result.components) == 1 and result.suggestions == []
    component = result.components[0]
    assert component.observed_monthly == 9999 and component.effective_monthly == 100
    assert component.source == "accepted_debt_schedule" and result.results.monthly_debt_service == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("body,annual,dscr", [({"debts": [obligation(monthly_payment="1000")]}, 12000, 10), ({"debts": [], "no_debt": True}, 0, None)])
async def test_intelligence_ignores_stale_ai_debt_and_ratio_once_schedule_is_complete(body, annual, dscr):
    profile = SimpleNamespace(id=uuid4(), intake_id=uuid4(), vertical="general")
    intake = SimpleNamespace(result_snapshot={"key_metrics": {"bankable_ebitda": 120000, "annual_debt_service": 999999, "estimated_dscr": 99}},
                             requested_loan_amount=None, asset_rows=[])
    with patch.object(selection, "accepted_debt_schedule_snapshot", AsyncMock(return_value=SimpleNamespace(body=body))):
        result = await application_profiles.intelligence_state(SimpleNamespace(get=AsyncMock(return_value=intake)), profile)
    assert result.dscr_inputs["annual_debt_service"] == annual
    metric = next(row for row in result.metrics if row.key == "dscr")
    assert metric.value == dscr and metric.source == "completed saved debt schedule"
    if annual == 0:
        assert metric.status == "not_applicable" and metric.action is None


def test_readiness_fingerprint_changes_with_accepted_version_without_changing_manual_ratio():
    profile = SimpleNamespace(id=uuid4(), vertical="general", underwriting_current_dscr=Decimal("1.75"),
                              use_of_funds_revision=1, use_of_funds=[])
    args = (profile, [], [], SimpleNamespace(id=uuid4(), version=1, policy_key="test", metric_thresholds={}), [], [], [], [], set(), {})
    first = capital_readiness._fingerprint(*args, accepted_debt_version_id=uuid4())
    second = capital_readiness._fingerprint(*args, accepted_debt_version_id=uuid4())
    assert first != second
    assert profile.underwriting_current_dscr == Decimal("1.75")
