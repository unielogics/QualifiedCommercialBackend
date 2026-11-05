"""Contract terms stay distinct from APR and produce traceable monthly payments."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from app.services import (
    dealer_forms_pdf,
    financial_statements,
    financial_templates_xlsx,
    financial_workspace,
    sheet_layout,
)
from app.services.debt_terms import DebtTermsError, monthly_equivalent, normalize_terms


@pytest.mark.parametrize(("amount", "frequency", "expected"), [
    ("420", "daily", "8820.00"), ("420", "weekly", "1818.60"),
    ("420", "biweekly", "909.30"), ("420", "monthly", "420.00"),
    ("1", "biweekly", "2.17"), ("0", "daily", "0.00"),
])
def test_monthly_conversion_uses_existing_business_day_factors_and_half_up(amount, frequency, expected):
    assert monthly_equivalent(amount, frequency) == Decimal(expected)


@pytest.mark.parametrize(("amount", "frequency"), [(None, "daily"), ("420", None), ("0", ""), (None, None)])
def test_incomplete_native_terms_stay_unknown(amount, frequency):
    assert monthly_equivalent(amount, frequency) is None


@pytest.mark.parametrize("fields", [
    {"factor_rate": "0"}, {"factor_rate": "-1.3"}, {"factor_rate": "1.32%"},
    {"factor_rate": "1.1234"}, {"payment_amount": "0.001"}, {"payment_amount": "-5"},
    {"payment_amount": "NaN"}, {"payment_amount": "Infinity"},
    {"payment_frequency": "quarterly"}, {"payment_frequency": "Daily"},
])
def test_native_terms_enforce_precision_finite_values_positive_factor_and_strict_frequency(fields):
    with pytest.raises(DebtTermsError):
        normalize_terms(fields)


def test_factor_and_interest_rate_are_separate_and_do_not_invent_each_other():
    terms = normalize_terms({"factor_rate": "1.32×", "payment_amount": "$420.00", "payment_frequency": "daily"})
    assert terms["factor_rate"] == Decimal("1.320")
    assert terms["monthly_payment"] == Decimal("8820.00")
    assert "rate" not in terms
    with_interest = normalize_terms({**terms, "rate": "12.125%"})
    assert with_interest["rate"] == Decimal("12.125") and with_interest["factor_rate"] == Decimal("1.320")


def test_stateless_validation_preserves_old_explicit_monthly_without_a_source_marker():
    terms = normalize_terms({"monthly_payment": "100", "payment_amount": "20", "payment_frequency": "daily"})
    assert terms["monthly_payment"] == Decimal("100.00")
    assert terms["monthly_payment_source"] == "stated"
    body = financial_workspace.validate_body("debt_schedule", {"debts": [{"id": str(uuid4()), "lender": "Existing", **terms}]})
    assert body["debts"][0]["monthly_payment"] == "100.00"


def test_explicit_monthly_edit_wins_when_native_terms_are_unchanged():
    previous = {"monthly_payment": "420", "payment_amount": "20", "payment_frequency": "daily", "monthly_payment_source": "native_terms"}
    terms = normalize_terms({**previous, "monthly_payment": "150"}, previous)
    assert terms["monthly_payment"] == Decimal("150.00") and terms["monthly_payment_source"] == "stated"


def test_intentional_native_edits_recompute_monthly_and_clearing_derived_terms_clears_it():
    previous = {"monthly_payment": "100", "payment_amount": None, "payment_frequency": None}
    daily = normalize_terms({**previous, "payment_amount": "20", "payment_frequency": "daily"}, previous)
    assert daily["monthly_payment"] == Decimal("420.00") and daily["monthly_payment_source"] == "native_terms"
    weekly = normalize_terms({**daily, "payment_frequency": "weekly"}, daily)
    assert weekly["monthly_payment"] == Decimal("86.60")
    cleared = normalize_terms({**weekly, "payment_frequency": None}, weekly)
    assert cleared["monthly_payment"] is None and cleared["monthly_payment_source"] is None


def test_clearing_native_terms_does_not_remove_a_separately_stated_monthly_amount():
    previous = {"monthly_payment": "100", "payment_amount": "20", "payment_frequency": "daily", "monthly_payment_source": "stated"}
    terms = normalize_terms({**previous, "payment_frequency": None}, previous)
    assert terms["monthly_payment"] == Decimal("100.00") and terms["monthly_payment_source"] == "stated"


def test_legacy_row_cleaning_and_key_facts_retain_native_terms_zero_and_unknowns():
    rows = financial_statements.debt_rows_from_body({"debts": [{"lender": "Advance", "original_amount": "0", "balance": "0", "rate": "0", "factor_rate": "1.32", "payment_amount": "0", "payment_frequency": "daily"}]})
    facts = financial_statements.debt_key_facts(rows)
    debt = facts["debts"][0]
    assert debt["factor_rate"] == 1.32 and debt["rate"] == 0
    assert debt["payment_amount"] == debt["monthly_payment"] == debt["original_amount"] == 0
    assert debt["payment_frequency"] == "daily" and debt["monthly_payment_source"] == "native_terms"
    missing = financial_statements.debt_rows_from_body({"debts": [{"lender": "Unknown cadence", "payment_amount": "420"}]})
    assert missing[0]["monthly_payment"] is None
    assert financial_statements.debt_key_facts(missing)["total_monthly_debt_service"] is None


def test_existing_projection_retains_omitted_native_terms_for_an_old_client():
    target = SimpleNamespace(lender="Advance", monthly_payment=Decimal("100"), payment_amount=Decimal("20"), payment_frequency="daily", factor_rate=Decimal("1.32"), evidence={"old_source": "kept"})
    row = financial_statements.debt_rows_from_body({"debts": [{"lender": "Advance", "monthly_payment": "150", "balance": "4000"}]})[0]
    financial_statements._apply_debt_form_fields(target, row)
    assert target.monthly_payment == Decimal("150.00")
    assert target.payment_amount == 20 and target.payment_frequency == "daily" and target.factor_rate == Decimal("1.32")
    assert target.evidence["old_source"] == "kept" and target.evidence["monthly_payment_source"] == "stated"


@pytest.mark.asyncio
async def test_draft_native_edit_updates_calculated_monthly_facts_and_provenance_together():
    row_id = str(uuid4())
    previous = {"id": row_id, "lender": "Advance", "monthly_payment": "100", "payment_amount": None, "payment_frequency": None}
    draft = SimpleNamespace(body={"debts": [previous]}, revision=0, updated_at=None, updated_by_user_id=None)
    family = SimpleNamespace(kind="debt_schedule")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))), flush=AsyncMock())
    profile = SimpleNamespace(id=uuid4(), dealer_id=None)
    await financial_workspace.stage_draft(db, profile, family, draft, {"debts": [{**previous, "payment_amount": "20", "payment_frequency": "daily", "factor_rate": "1.3"}]})
    row = draft.body["debts"][0]
    assert row["monthly_payment"] == "420.00" and row["monthly_payment_source"] == "native_terms"
    assert draft.body["field_sources"][f"{row_id}.monthly_payment"]["calculation"] == "native_monthly_equivalent"
    facts = financial_workspace.facts_for_body("debt_schedule", draft.body)
    assert facts["debts"][0]["monthly_payment"] == 420 and facts["total_monthly_debt_service"] == 420


def _named(wb, name):
    sheet, reference = wb.defined_names[name].attr_text.rsplit("!", 1)
    return wb[sheet.strip("'")][reference.replace("$", "")]


def test_xlsx_keeps_old_monthly_input_and_adds_locked_native_equivalent_formula():
    workbook = load_workbook(BytesIO(financial_templates_xlsx.build_workbook("debt_schedule")))
    assert _named(workbook, "ds.r1.monthly_payment").coordinate == "F7"
    factor = _named(workbook, "ds.r1.factor_rate")
    assert factor.coordinate == "O7" and "%" not in factor.number_format
    assert _named(workbook, "ds.r1.payment_amount").coordinate == "P7"
    frequency = _named(workbook, "ds.r1.payment_frequency")
    assert frequency.coordinate == "Q7"
    equivalent = workbook.active["R7"]
    assert equivalent.protection.locked and not _named(workbook, "ds.r1.monthly_payment").protection.locked
    assert all(value in equivalent.value for value in ("P7", "Q7", "F7", "21.0", "4.33", "2.165", "ROUND("))
    # A stated F value, including zero, wins over conflicting native terms.
    assert equivalent.value.startswith('=IF(ISNUMBER(F7),F7,IF(AND(')
    assert "overrides" in _named(workbook, "ds.r1.monthly_payment").comment.text
    assert _named(workbook, "ds.total_monthly_payment").value == "=SUM(R7:R21)"
    assert any(frequency.coordinate in validation.sqref for validation in frequency.parent.data_validations.dataValidation)


def test_schema_and_pdf_distinguish_factor_multiple_native_amount_cadence_and_apr():
    row = {"lender": "Advance", "rate": "12.125", "factor_rate": "1.32", "payment_amount": "20", "payment_frequency": "daily"}
    normalized = normalize_terms(row)
    rendered = dealer_forms_pdf._schedule_row_html(normalized)
    assert "1.320×" in rendered and "12.125%" in rendered
    assert "$20.00" in rendered and "$420.00" in rendered and "Daily" in rendered and "calculated" in rendered
    assert "1.320%" not in rendered
    cells = {cell.key: cell for row in sheet_layout.layout("debt_schedule").rows for cell in row.cells if cell.key}
    assert cells["r1.factor_rate"].format == "text"
    assert cells["r1.payment_frequency"].options == ("daily", "weekly", "biweekly", "monthly")
    assert sheet_layout.DEBT_SCHEMA_VERSION == "qc_ds.v2"


def test_main_mca_answers_and_native_contract_terms_round_trip_together():
    raw = {"debts": [{"lender": "Fundbox", "debt_type": "MCA", "balance": "40000",
        "factor_rate": "1.38", "payment_amount": "420", "payment_frequency": "daily",
        "mca_modified": False, "mca_modification_note": "No change", "notes": "Contract reviewed"}]}
    validated = financial_workspace.validate_body("debt_schedule", raw)
    rows = financial_statements.debt_rows_from_body(validated)
    facts = financial_statements.debt_key_facts(rows)
    assert rows[0]["mca_modified"] is False and rows[0]["mca_modification_note"] == "No change"
    assert facts["debts"][0]["factor_rate"] == 1.38 and facts["debts"][0]["payment_frequency"] == "daily"
    assert facts["total_monthly_debt_service"] == 8820
    rendered = dealer_forms_pdf.build_debt_schedule_html(business_name="Little Star Therapy", rows=rows,
        total_balance=40000, total_monthly=8820)
    assert "1.380×" in rendered and "$420.00" in rendered and "Daily" in rendered
    assert "Modified by the funder for lower sales or revenue: No" in rendered
    assert "Contract reviewed" in rendered and raw["debts"][0]["mca_modified"] is False
