"""Exercise NOI, current earnings and sharing through the real cockpit pipeline."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.capital_partners.services.listings import partner_operating_earnings
from app.enums import Role
from app.services import financial_metric_review
from app.services.financial_metric_reporting import (
    current_intake_report,
    current_review_projection,
    operating_earnings_brief,
)
from app.services.public_underwriting_packet_pdf import _operating_earnings_lines
from app.tests.test_financial_metric_review import ACTOR, build, pnl, target


def annual(year=2025, *, key=None, **facts):
    row = pnl(**{"period_start": f"{year}-01-01", "period_end": f"{year}-12-31", "basis": "cash", "gross_revenue": 500000,
                 "cost_of_goods_sold": 100000, "total_operating_expenses": 380000, "operating_income": 20000,
                 "net_income": 15000, "interest": 5000, "income_taxes": 0, "depreciation_and_amortization": 10000, **facts})
    row["file_id"] = key or f"pl-{year}"
    row["file_name"] = f"Business-{year}.pdf"
    return row


def corrected(cockpit, key, value, *, state=None, mark_unknown=False):
    revision = (state or {}).get("revision", 0)
    return financial_metric_review.change(state, cockpit["metric_review"], input_key=key, action="correct", value=value,
        mark_unknown=mark_unknown, reason="PRIVATE_REVIEW_REASON", expected_revision=revision,
        source_stamp=target(cockpit, key)["source_stamp"], idempotency_key=f"PRIVATE_RECEIPT_{revision}", actor=ACTOR)[0]


def test_latest_year_alone_supplies_noi_ebitda_and_dscr_while_prior_pl_supplies_trend():
    prior = annual(2024, gross_revenue=1000000, cost_of_goods_sold=200000, total_operating_expenses=500000,
                   operating_income=300000, net_income=285000)
    current = annual(2025)
    cockpit = build([prior, current])
    assert cockpit["noi"]["headline"]["value"] == 20000
    assert cockpit["noi"]["headline"]["source_refs"][0]["file_id"] == "pl-2025"
    assert cockpit["ebitda"]["headline"]["value"] == 30000
    assert cockpit["dscr"]["headline"]["numerator"] == 30000
    assert cockpit["dscr"]["headline"]["value"] == 2.5
    assert cockpit["noi"]["trend"]["direction"] == "down"
    assert cockpit["ebitda"]["revenue_trend"]["comparison"]["direction"] == "down"
    assert [row["value"] for row in cockpit["noi"]["years"]] == [300000, 20000]
    assert cockpit["ebitda"]["potential"]["baseline"]["revenue"] == 500000


def test_latest_year_missing_operating_lines_stays_unavailable_even_when_book_earnings_are_known():
    cockpit = build([annual(2024), annual(2025, cost_of_goods_sold=None, total_operating_expenses=None,
                                         operating_income=None)])
    assert cockpit["noi"]["headline"] is None
    assert "latest business P&L has missing" in cockpit["noi"]["reason"]
    assert cockpit["ebitda"]["headline"]["value"] == 30000
    assert cockpit["dscr"]["headline"]["numerator"] == 30000
    assert cockpit["noi"]["years"][-1]["value"] is None
    assert not cockpit["noi"]["trend"]["comparable"]


def test_prepared_raw_operands_do_not_circularly_reconcile_source_reported_noi():
    cockpit = build([annual(total_operating_expenses=None)])
    assert target(cockpit, "profit_and_loss.total_operating_expenses")["value"] is None
    assert not target(cockpit, "profit_and_loss.total_operating_expenses")["source_refs"][0]["fact_present"]
    assert cockpit["noi"]["headline"]["reported_value"] == 20000
    assert cockpit["noi"]["headline"]["computed_value"] is None
    assert not cockpit["noi"]["headline"]["complete"]


def test_reported_noi_has_all_four_source_bound_editable_fundamentals_without_double_counting():
    cockpit = build([annual()])
    headline = cockpit["noi"]["headline"]
    expected = [f"profit_and_loss.{key}" for key in (
        "gross_revenue", "cost_of_goods_sold", "total_operating_expenses", "reported_operating_income")]
    assert headline["input_keys"] == expected
    assert headline["active_input_keys"] == ["profit_and_loss.reported_operating_income"]
    assert headline["value"] == headline["computed_value"] == 20000
    for key in expected:
        operand = target(cockpit, key)
        assert operand["editable"]
        assert operand["source_refs"][0]["file_id"] == "pl-2025"
        assert headline["key"] in operand["used_by"]
    correction = corrected(cockpit, "profit_and_loss.cost_of_goods_sold", 90000)
    current = build([annual()], state=correction)
    assert current["noi"]["headline"]["input_keys"] == expected
    assert current["noi"]["headline"]["active_input_keys"] == expected[:3]
    assert current["noi"]["headline"]["value"] == 30000
    assert current["noi"]["headline"]["reported_value"] == 20000


def test_corrected_fundamentals_change_working_noi_without_inventing_book_net_income_or_ebitda():
    source = annual()
    original = deepcopy(source)
    first = build([source])
    state = corrected(first, "profit_and_loss.total_operating_expenses", 360000)
    current = build([source], state=state)
    headline = current["noi"]["headline"]
    assert headline["value"] == headline["reviewed_value"] == 40000
    assert headline["reported_value"] == headline["source_value"] == 20000
    assert headline["staff_adjusted"]
    assert current["ebitda"]["headline"]["value"] == first["ebitda"]["headline"]["value"] == 30000
    assert current["dscr"]["headline"]["numerator"] == 30000
    assert source == original
    assert headline["key"] in target(current, "profit_and_loss.total_operating_expenses")["used_by"]


def test_revenue_correction_reaches_potential_and_export_without_changing_reported_earnings():
    source = annual()
    first = build([source])
    state = corrected(first, "profit_and_loss.gross_revenue", 600000)
    current = build([source], state=state)
    assert current["ebitda"]["headline"]["value"] == 30000
    potential = current["ebitda"]["potential"]
    assert potential["baseline"]["revenue"] == 600000
    assert potential["baseline"]["margin_pct"] == 5
    assert potential["current"]["targets"][0]["margin_pct"] == 2
    projected = current_review_projection({"key_metrics": {"estimated_dscr": 999}}, current)
    assert projected["operating_earnings"]["potential"]["baseline"]["revenue"] == 600000
    assert projected["key_metrics"]["estimated_dscr"] == current["dscr"]["headline"]["value"] == 2.5


def test_marked_unknown_operating_revenue_is_not_resurrected_in_noi_potential_or_packages():
    source = annual(operating_income=None)
    state = corrected(build([source]), "profit_and_loss.gross_revenue", None, mark_unknown=True)
    cockpit = build([source], state=state)
    assert cockpit["noi"]["headline"] is None
    assert not cockpit["ebitda"]["potential"]["available"]
    assert cockpit["ebitda"]["potential"]["baseline"]["revenue"] is None
    assert operating_earnings_brief(cockpit)["noi"]["historical"] is None


def test_new_source_analysis_invalidates_noi_correction_and_restores_current_source_figure():
    source = annual()
    state = corrected(build([source]), "profit_and_loss.total_operating_expenses", 360000)
    replacement = deepcopy(source)
    replacement["analysis_id"] = "new-current-reading"
    current = build([replacement], state=state)
    assert current["noi"]["headline"]["value"] == 20000
    assert not current["noi"]["headline"]["staff_adjusted"]
    assert target(current, "profit_and_loss.total_operating_expenses")["correction"] is None
    assert any(row["input_key"] == "profit_and_loss.total_operating_expenses" for row in current["metric_review"]["stale_corrections"])


def test_refinance_changes_residual_and_coverage_targets_without_generic_income_override_leaking_to_noi():
    cockpit = build([annual()], plan={"retire": ["row:debt"], "new_payment_monthly": 300,
                                     "overrides": {"income_annual": 999000, "income_note": "Separate DSCR assumption"}})
    scenario = cockpit["noi"]["scenario"]
    assert scenario["current"] == scenario["plan"] == 20000
    assert scenario["annual_debt_service_current"] == 12000
    assert scenario["annual_debt_service_plan"] == 3600
    assert scenario["after_debt_service_current"] == 8000
    assert scenario["after_debt_service_plan"] == 16400
    potential = cockpit["ebitda"]["potential"]
    assert potential["baseline"]["earnings"] == 30000
    assert potential["current"]["dscr"] == 2.5 and potential["plan"]["dscr"] == 8.33
    assert cockpit["plan"]["evaluation"]["headline_after"] != potential["plan"]["dscr"]
    assert any("not a complete cash-flow statement" in note for note in scenario["notes"])


def test_internal_package_retains_source_provenance_but_not_private_review_receipts():
    source = annual()
    source["file_name"] = "PRIVATE_SOURCE_DOCUMENT.pdf"
    source["analysis_id"] = "PRIVATE_ANALYSIS_ID"
    state = corrected(build([source]), "profit_and_loss.total_operating_expenses", 360000)
    state["events"][-1]["actor_name"] = "PRIVATE_ACTOR_NAME"
    cockpit = build([source], state=state)
    brief = operating_earnings_brief(cockpit)
    serialized = json.dumps(brief)
    assert "PRIVATE_SOURCE_DOCUMENT" in serialized and "PRIVATE_ANALYSIS_ID" in serialized
    assert all(value not in serialized for value in ("PRIVATE_REVIEW_REASON", "PRIVATE_RECEIPT", "PRIVATE_ACTOR_NAME"))
    assert brief["noi"]["historical"]["value"] == 40000
    assert brief["noi"]["historical"]["source_value"] == 20000
    assert brief["noi"]["historical"]["staff_adjusted"]
    shared = partner_operating_earnings(brief)
    assert "PRIVATE_" not in json.dumps(shared)
    assert shared["noi"]["historical"]["value"] == 40000
    assert shared["potential"]["baseline"]["earnings"] == 30000


@pytest.mark.asyncio
async def test_external_current_report_uses_latest_metrics_without_file_inventory_or_private_audit():
    source = annual()
    source["file_name"] = "PRIVATE_SOURCE_DOCUMENT.pdf"
    source["analysis_id"] = "PRIVATE_ANALYSIS_ID"
    state = corrected(build([source]), "profit_and_loss.total_operating_expenses", 360000)
    state["events"][-1]["actor_name"] = "PRIVATE_ACTOR_NAME"
    cockpit = build([source], state=state)
    cached = {"key_metrics": {"estimated_dscr": 99, "annual_ebitda": 999999}}
    before = deepcopy(cached)
    with (patch("app.services.application_profiles.find_profile", AsyncMock(return_value=SimpleNamespace(id=uuid4()))),
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=cockpit))):
        report = await current_intake_report(None, SimpleNamespace(id=uuid4(), variant="general"), cached, external=True)
    assert cached == before
    assert report["key_metrics"]["estimated_dscr"] == 2.5
    assert report["key_metrics"]["annual_ebitda"] == 30000
    assert report["operating_earnings"]["noi"]["historical"]["value"] == 40000
    assert "PRIVATE_" not in json.dumps(report)
    assert "financial_calculation_review" not in report and "financial_calculation_authority" not in report
    assert "dscr_source_refs" not in report["key_metrics"]


def test_client_metric_read_keeps_numeric_noi_correction_and_removes_private_audit():
    source = annual()
    state = corrected(build([source]), "profit_and_loss.total_operating_expenses", 360000)
    state["events"][-1]["actor_name"] = "PRIVATE_ACTOR_NAME"
    cockpit = build([source], state=state)
    client = financial_metric_review.redact_for_external(cockpit, SimpleNamespace(role=Role.CLIENT))
    assert client["noi"]["headline"]["value"] == 40000
    assert not target(client, "profit_and_loss.total_operating_expenses")["editable"]
    assert "PRIVATE_" not in json.dumps(client)
    assert cockpit["metric_review"]["history"]


def test_incomplete_latest_earnings_cannot_revive_prior_profitable_dscr_in_package():
    cockpit = build([annual(2024, net_income=1000000), annual(2025, net_income=None, operating_income=None,
                                                           cost_of_goods_sold=None, total_operating_expenses=None)])
    assert cockpit["ebitda"]["headline"] is None
    assert cockpit["noi"]["headline"] is None
    assert cockpit["dscr"]["headline"] is None
    assert not cockpit["ebitda"]["potential"]["available"]
    projected = current_review_projection({"key_metrics": {"estimated_dscr": 999, "annual_ebitda": 999999}}, cockpit)
    assert projected["key_metrics"]["estimated_dscr"] is None
    assert projected["key_metrics"]["annual_ebitda"] is None
    assert projected["operating_earnings"]["historical"] is None


def test_package_noi_section_reports_operating_income_without_renaming_it_ebitda():
    cockpit = build([annual()])
    earnings = operating_earnings_brief(cockpit)
    lines = _operating_earnings_lines({"operating_earnings": earnings})
    text = "\n".join(lines)
    assert "NOI" in text and "20,000" in text
    assert "30,000" in text
    assert earnings["historical"]["value"] == 30000
    assert earnings["noi"]["historical"]["value"] == 20000
