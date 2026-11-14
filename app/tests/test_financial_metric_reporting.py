import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import application_profiles, financial_cockpit
from app.services.ai import context as ai_context
from app.services.financial_metric_reporting import (
    attach_review_context,
    metric_brief,
    validated_annual_pair,
)
from app.tests.test_financial_metric_review import build, correction, pnl, tax


def test_filed_return_supplies_primary_and_annual_pair_and_a_pl_does_without_one():
    cockpit = build([tax(tax_form="1120-S", interest=None, depreciation=None), pnl()])
    brief = metric_brief(cockpit)
    assert brief["primary_dscr"]["key"] == cockpit["dscr"]["headline"]["key"] == "tax_return"
    assert brief["primary_dscr"]["value"] == cockpit["dscr"]["headline"]["value"]
    annual = brief["validated_annual"]
    # Add-backs the reader did not find are left out: the figure can only understate.
    assert annual["ebitda_annual"] == 100000 and annual["dscr"] == 8.33
    assert annual["dscr_label"] == "Filed return EBITDA-based DSCR"
    assert brief["primary_dscr"]["value"] == annual["dscr"]
    assert not next(row for row in brief["ebitda"]["candidates"] if row["kind"] == "tax_estimate")["provisional"]
    # A return whose currency is not established is not a validated annual basis.
    assert metric_brief(build([tax(interest=None, depreciation=None), pnl()]))["validated_annual"] is None
    alone = metric_brief(build([pnl()]))
    assert alone["primary_dscr"]["key"] == "profit_and_loss"
    assert alone["validated_annual"]["ebitda_annual"] == 52000 and alone["validated_annual"]["dscr"] == 4.33
    assert alone["validated_annual"]["dscr_label"] == "P&L EBITDA-based DSCR"


def test_current_correction_reaches_review_without_a_hypothetical_plan_or_private_audit_text():
    original = build()
    state = correction(original)
    state["events"][0].update(reason="PRIVATE STAFF REASON", actor_name="PRIVATE ACTOR", idempotency_key="PRIVATE RECEIPT")
    cockpit = build(state=state)
    assert not cockpit["plan"]["evaluation"]["active"]
    context = {"desk_financial_plan": {"active": True, "stale_cached_plan": True}}
    attach_review_context(context, cockpit)
    assert "desk_financial_plan" not in context
    brief = context["financial_calculation_review"]
    assert brief["corrections"][0]["status"] == "current_applied"
    assert brief["validated_annual"]["ebitda_annual"] == 72000
    serialized = json.dumps(brief)
    assert all(text not in serialized for text in ("PRIVATE STAFF REASON", "PRIVATE ACTOR", "PRIVATE RECEIPT"))
    assert "history" not in brief


def test_tax_correction_remains_in_primary_ai_brief_without_becoming_strict_annual_ebitda():
    original = build([tax(interest=None, depreciation=None)])
    target = next(row for row in original["metric_review"]["inputs"] if row["key"].endswith(".income"))
    state = correction(original, key=target["key"], value=150000)
    current = build([tax(interest=None, depreciation=None)], state=state)
    brief = metric_brief(current)
    assert brief["validated_annual"] is None
    assert brief["primary_dscr"]["numerator"] == 150000
    assert brief["corrections"][0]["effective_value"] == 150000
    assert brief["ebitda"]["headline"]["kind"] == "tax_estimate"


def test_new_analysis_marks_correction_stale_and_reports_current_original_values():
    original = build()
    state = correction(original)
    updated = pnl()
    updated["analysis_id"] = "new-reading"
    cockpit = build([updated], state=state)
    brief = metric_brief(cockpit)
    assert brief["corrections"] == []
    assert brief["stale_corrections"][0] == {"input_key": "profit_and_loss.net_income", "period": "Jan–Jun 2026", "status": "stale_not_applied"}
    assert brief["validated_annual"]["ebitda_annual"] == 52000
    assert "value" not in brief["stale_corrections"][0]


@pytest.mark.parametrize("changes", [
    {"period_end": "2026-04-30"}, {"period_start": None}, {"currency": None}, {"basis": None},
    {"business_name": "Different LLC"}, {"ebitda": 999999},
])
def test_period_unknown_mismatched_or_unreconciled_earnings_do_not_become_strict_annual(changes):
    cockpit = build([pnl(**changes)])
    assert validated_annual_pair(cockpit) is None


def test_unaccepted_source_and_bank_proxy_do_not_supply_validated_annual_ebitda():
    source = pnl()
    source["accepted"] = False
    assert validated_annual_pair(build([source])) is None
    assert validated_annual_pair(build([])) is None
    assert validated_annual_pair(build([tax()])) is None


def test_unvalidated_or_other_currency_debt_never_promotes_a_pl_dscr_pair():
    cockpit = build()
    cockpit["debt"]["service"].update(schedule_authoritative=False, observed_additions_suppressed=False)
    assert validated_annual_pair(cockpit)["dscr"] is None
    other_currency = build([pnl(currency="EUR")])
    assert validated_annual_pair(other_currency)["ebitda_annual"] == 52000
    assert validated_annual_pair(other_currency)["dscr"] is None


@pytest.mark.parametrize("currency,status", [("EUR", "mismatch"), (None, "unknown")])
def test_brief_blocks_known_currency_mismatch_and_preserves_calculation_evidence(currency, status):
    cockpit = build([pnl(currency=currency)])
    brief = metric_brief(cockpit)
    primary = brief["primary_dscr"]
    calculation = next(row for row in brief["calculations"] if row["key"] == "dscr.profit_and_loss")
    if status == "mismatch":
        assert primary is None
        assert cockpit["dscr"]["headline"] is None
        assert calculation["value"] is None and calculation["numerator"] is None
        assert cockpit["dscr"]["candidates"][0]["source_numerator"] == 52000
    else:
        assert primary["value"] == cockpit["dscr"]["headline"]["value"] == 4.33
        assert primary["source_currencies"] == []
        assert primary["currency_note"] in primary["note"]
    for result in ((calculation,) if primary is None else (primary, calculation)):
        assert result["currency_status"] == status and not result["currency_comparable"]
        assert result["denominator_currency"] == "USD" and result["currency_note"]
        assert "without currency conversion" in result["currency_note"]
        assert result["source_currencies"] == ([currency] if currency else [])


@pytest.mark.asyncio
async def test_internal_loan_ai_context_uses_current_correction_even_without_a_plan():
    cockpit = build(state=correction(build()))
    with patch.object(application_profiles, "find_profile", AsyncMock(return_value=SimpleNamespace(id=uuid4()))), \
         patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)), \
         patch("app.services.bucket_ai._published_program_screen", AsyncMock(return_value={})):
        result = await ai_context._financial_calculation_block(None, SimpleNamespace(id=uuid4()), audience="super_admin")
    assert "current_applied" in result and "72000" in result and "primary_dscr" in result


@pytest.mark.asyncio
async def test_borrower_prompt_never_loads_internal_correction_context():
    with patch.object(application_profiles, "find_profile", AsyncMock()) as lookup:
        assert await ai_context._financial_calculation_block(None, None, audience="client") == ""
    lookup.assert_not_awaited()


def profile_fixture():
    intake = SimpleNamespace(result_snapshot={"key_metrics": {"bankable_ebitda": 999999, "annual_debt_service": 888888,
                                                              "estimated_dscr": 99}}, requested_loan_amount=None, asset_rows=[])
    profile = SimpleNamespace(id=uuid4(), intake_id=uuid4(), vertical="general")
    db = SimpleNamespace(get=AsyncMock(return_value=intake), execute=AsyncMock())
    return db, profile, intake


@pytest.mark.asyncio
async def test_intelligence_uses_current_primary_dscr_and_validated_annual_pl_ebitda():
    db, profile, intake = profile_fixture()
    before = deepcopy(intake.result_snapshot)
    cockpit = build([pnl()])
    with patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)):
        result = await application_profiles.intelligence_state(db, profile)
    assert next(row for row in result.metrics if row.key == "ebitda").value == 52000
    dscr = next(row for row in result.metrics if row.key == "dscr")
    assert dscr.value == cockpit["dscr"]["headline"]["value"] == 4.33
    assert "Current financial cockpit" in dscr.source and "Profit and loss" in dscr.source
    assert result.dscr_inputs["bankable_ebitda"] == 52000 and result.dscr_inputs["annual_debt_service"] == 12000
    assert result.metric_review["primary_dscr"]["key"] == "profit_and_loss"
    assert result.dscr_inputs["basis"] == "current_cockpit" and result.dscr_inputs["cash_flow_value"] == 52000
    assert result.effective_calculation["dscr"] == 4.33
    assert result.effective_calculation["basis"] == "validated_profit_and_loss_annual_ebitda"
    assert intake.result_snapshot == before


@pytest.mark.asyncio
async def test_live_unavailable_coverage_and_short_period_ebitda_do_not_resurrect_cached_ai():
    db, profile, _ = profile_fixture()
    cockpit = build([pnl(period_end="2026-04-30")])
    with patch("app.services.financial_version_selection.accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), \
         patch.object(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit)):
        result = await application_profiles.intelligence_state(db, profile)
    assert next(row for row in result.metrics if row.key == "ebitda").value is None
    dscr = next(row for row in result.metrics if row.key == "dscr")
    assert dscr.value is None and dscr.status == "needs_evidence"
    assert "Current financial cockpit" in dscr.source and "unavailable" in dscr.source
    assert result.dscr_inputs["basis"] == "current_cockpit"
    assert result.effective_calculation is None
    assert result.metric_review["ebitda"]["headline"]["value"] == 26000
