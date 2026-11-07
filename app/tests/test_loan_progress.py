from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dealer_os.schemas import DebtRead, RefiDebtRead
from app.dealer_os.services.financial_debt_drafts import read_row
from app.schemas.financial_cockpit import AdvanceRead
from app.services import financial_cockpit as cockpit
from app.services import financial_publication as publication
from app.services import financial_statements as statements
from app.services import financial_workspace as workspace
from app.services import sheet_layout, sheets
from app.services.financial_debt_intake import _loan_row
from app.services.financial_plan import obligations
from app.services.loan_progress import (
    format_paid_down_pct,
    loan_progress_fields,
    originated_on,
    paid_down_pct,
)
from app.services.mca_advances import advance_read


@pytest.mark.parametrize(("original", "balance", "expected"), [
    (100, 75, 25), (100, 0, 100), (100, 100, 0), (100, 125, -25),
    (Decimal("300"), Decimal("200"), 100 / 3), (100, "99.995", 0.005),
    (None, 0, None), (0, 0, None), (-1, 0, None), (100, None, None),
    (100, -1, None), ("", 10, None), (True, 0, None), (100, False, None),
    ("$1,000.00", "500", 50), ("1,000", 500, 50), ("$100,000", "75,000", 25),
    (" $ 1 000 ", "750", 25), ("100", "($5)", None), ("($100)", "5", None),
    ("0x100", 500, None), ("1_000", 500, None), ("invalid", 500, None),
    ("-", 0, None), (".", 0, None), ("+", 0, None), ("10 dollars", 0, None),
    (float("nan"), 0, None), (100, float("inf"), None), (float("-inf"), 0, None),
    ("NaN", 10, None), (100, "Infinity", None), ("1e1000", 10, None),
])
def test_balance_reduction_is_finite_signed_and_unknown_safe(original, balance, expected):
    actual = paid_down_pct(original, balance)
    assert actual is None if expected is None else actual == pytest.approx(expected)


@pytest.mark.parametrize(("value", "expected"), [(0.15, "0.2%"), (-0.15, "-0.2%"),
    (0, "0.0%"), (-0.001, "0.0%"), (0.149, "0.1%"), (-0.149, "-0.1%"),
    (None, "—"), (float("nan"), "—")])
def test_display_rounding_is_half_up_without_negative_zero(value, expected):
    assert format_paid_down_pct(value) == expected


@pytest.mark.parametrize("alias", ["originated_on", "origination_date", "originated_date", "origin_date"])
def test_explicit_origination_aliases_survive_read_and_import_paths(alias):
    raw = {"lender": "Example", alias: "2025-03-09", "original_amount": 100, "current_balance": 75}
    assert originated_on(raw) == "2025-03-09"
    assert statements.document_debts_as_body({"debts": [raw]})["debts"][0]["originated_on"] == "2025-03-09"
    assert workspace._body_from_facts("debt_schedule", {"debts": [raw]})["debts"][0]["originated_on"] == "2025-03-09"
    proposed, _warnings = _loan_row(raw)
    assert proposed["originated_on"] == "2025-03-09"
    assert "paid_down_pct" not in proposed  # Calculations are not extracted/writable fields.


def test_no_date_is_inferred_from_upload_maturity_or_purchase_and_canonical_date_wins():
    assert originated_on({"created_at": "2026-01-01", "maturity_on": "2027-01-01", "purchased_on": "2025-01-01"}) is None
    assert originated_on({"originated_on": "not a date", "origination_date": "2025-01-01"}) is None
    assert originated_on({"originated_on": date(2025, 1, 2)}) == "2025-01-02"


@pytest.mark.parametrize("cleared", [None, ""])
def test_explicit_canonical_date_clear_does_not_resurrect_a_historical_alias(cleared):
    raw = {"lender": "Example", "originated_on": cleared, "originated_date": "2025-01-02"}
    assert originated_on(raw) is None
    assert statements.document_debts_as_body({"debts": [raw]})["debts"][0]["originated_on"] is None
    assert workspace._body_from_facts("debt_schedule", {"debts": [raw]})["debts"][0]["originated_on"] is None
    assert _loan_row(raw)[0]["originated_on"] is None


def test_mca_payoff_factor_and_credit_limit_never_substitute_for_original_or_balance():
    row = {"category": "mca", "balance": 125, "original_amount": 100, "payoff_amount": 80,
           "credit_limit": 200, "factor_rate": 1.35, "originated_on": "2025-01-02"}
    before = deepcopy(row)
    assert loan_progress_fields(row)["paid_down_pct"] == -25
    assert loan_progress_fields({**row, "original_amount": None})["paid_down_pct"] is None
    assert loan_progress_fields({**row, "balance": None})["paid_down_pct"] is None
    assert row == before


def test_same_lender_saved_loans_keep_exact_original_date_and_progress_without_payee_joins():
    raw = [{"id": str(uuid4()), "lender": "Same SBA", "category": "sba", "original_amount": 100,
            "balance": 25, "originated_on": "2024-01-01", "monthly_payment": 10},
           {"id": str(uuid4()), "lender": "Same SBA", "category": "sba", "original_amount": 200,
            "balance": 150, "originated_on": "2025-02-03", "monthly_payment": 20}]
    before = deepcopy(raw)
    rows = cockpit.debt_rows(raw)
    planned = obligations({"rows": rows, "service": {"source": "accepted_debt_schedule", "schedule_authoritative": True}},
        {"payees": [{"key": "same", "payee": "Same SBA", "is_debt": True, "monthly_average": 9000, "cadence": "daily"}]})
    assert [(row["original_amount"], row["originated_on"], row["paid_down_pct"]) for row in rows] == [
        (100, "2024-01-01", 75), (200, "2025-02-03", 25)]
    assert [(row["original_amount"], row["originated_on"], row["paid_down_pct"]) for row in planned] == [
        (100, "2024-01-01", 75), (200, "2025-02-03", 25)]
    assert [row["monthly"] for row in planned] == [10, 20]
    assert [row["balance"] for row in planned] == [25, 150]
    assert raw == before


def test_source_document_progress_is_local_and_legacy_lender_aggregation_cannot_invent_pairs():
    source = {"file_id": str(uuid4()), "classification": "advance_agreement", "key_facts": {"debts": [
        {"lender": "Funder", "original_amount": 100, "current_balance": 75, "originated_date": "2025-01-01"}]}}
    one = cockpit.documented_debts([source])[0]
    assert (one["paid_down_pct"], one["originated_on"]) == (25, "2025-01-01")
    second = deepcopy(source)
    second["file_id"] = str(uuid4())
    second["key_facts"]["debts"][0].update(original_amount=200, current_balance=25, originated_date="2026-01-01")
    merged = cockpit.documented_debts([source, second])[0]
    assert merged["original_amount"] == 200 and merged["balance"] == 75  # Legacy figures unchanged.
    assert merged["paid_down_pct"] is None and merged["originated_on"] is None
    assert cockpit.documented_debts([{**source, "key_facts": {"debts": [{"lender": "Funder", "original_amount": 0, "funded_amount": 100, "current_balance": 0}]}}])[0]["paid_down_pct"] is None


@pytest.mark.parametrize("schema", [DebtRead, RefiDebtRead])
def test_dealer_and_refinance_reads_compute_from_orm_and_ignore_supplied_percent(schema):
    row = SimpleNamespace(id=uuid4(), lender="Example", category="sba", original_amount=Decimal("100"),
        balance=Decimal("25"), originated_on=date(2025, 1, 2), origin="admin", status="active", paid_down_pct=999)
    result = schema.model_validate(row)
    assert result.original_amount == 100 and result.originated_on == date(2025, 1, 2)
    assert result.paid_down_pct == 75
    assert result.balance == 25 and row.paid_down_pct == 999


def test_advance_read_preserves_existing_date_and_uses_recorded_original_only():
    row = SimpleNamespace(id=uuid4(), lender="Example", original_amount=100, balance=25,
        originated_on=date(2025, 1, 2), payment_amount=10, payment_frequency="daily", monthly_payment=210,
        factor_rate=1.35, payoff_amount=99, maturity_on=None, notes=None, count_in_dscr=True, vendor_key=None, origin="admin")
    result = AdvanceRead(**advance_read(row))
    assert result.originated_on == date(2025, 1, 2) and result.paid_down_pct == 75
    assert result.monthly_payment == 210 and result.payoff_amount == 99


def test_managed_legacy_read_adapter_cleans_formatted_original_and_blank_date_for_new_fields():
    raw = {"id": str(uuid4()), "lender": "Example", "original_amount": "$100,000",
           "balance": "75,000", "originated_on": "", "origin": "admin"}
    snapshot = {"family_id": str(uuid4()), "draft": {"dirty": True, "revision": 2}}
    result = DebtRead.model_validate(read_row(raw, snapshot))
    assert result.original_amount == 100000 and result.balance == 75000
    assert result.originated_on is None and result.paid_down_pct == 25


def test_worksheet_calculation_is_identity_keyed_readonly_and_never_saved_as_input():
    a, b = str(uuid4()), str(uuid4())
    body = {"debts": [{"id": a, "lender": "One", "original_amount": 100, "balance": 25},
                      {"id": b, "lender": "Two", "original_amount": None, "balance": 0}]}
    before = deepcopy(body)
    layout = sheet_layout.layout_for_body("debt_schedule", body)
    rows = [row for row in layout.rows if row.kind == "data"]
    assert len(statements.DEBT_COLUMNS) == 17
    for row in rows:
        computed = row.cells[-1]
        assert computed.c == 18 and computed.type == "formula" and computed.format == "percent"
        assert computed.compute == f"{row.row_key}.paid_down_pct"
        assert computed.key is computed.path is computed.xlsx_name is None and not computed.editable
        assert [cell.path[-1] for cell in row.cells if cell.editable] == list(statements.DEBT_COLUMNS)
        assert computed.compute not in sheet_layout.flatten("debt_schedule", body)
    values = sheets._computed("debt_schedule", body)
    assert values[f"{a}.paid_down_pct"] == 75 and values[f"{b}.paid_down_pct"] is None
    body["debts"].reverse()
    assert sheets._computed("debt_schedule", body)[f"{a}.paid_down_pct"] == 75
    body["debts"][1]["balance"] = 0
    assert sheets._computed("debt_schedule", body)[f"{a}.paid_down_pct"] == 100
    assert before["debts"][0]["balance"] == 25


def test_future_facts_add_canonical_date_and_progress_without_changing_balances_or_body():
    body = {"debts": [{"lender": "Example", "original_amount": "100", "balance": "25", "monthly_payment": "10", "originated_on": "2025-01-02"}]}
    before = deepcopy(body)
    facts = statements.debt_key_facts(statements.debt_rows_from_body(body))
    row = facts["debts"][0]
    assert row["originated_on"] == row["originated_date"] == "2025-01-02"
    assert row["paid_down_pct"] == 75
    assert facts["total_outstanding_balance"] == 25 and facts["total_monthly_debt_service"] == 10
    assert body == before


@pytest.mark.asyncio
async def test_ai_context_only_derives_exact_current_accepted_visible_rows_and_keeps_immutable_facts():
    profile_id, file_id, document_id, hidden_id = uuid4(), uuid4(), uuid4(), uuid4()
    a, b = str(uuid4()), str(uuid4())
    body = {"debts": [{"id": a, "lender": "Same SBA", "original_amount": 100, "balance": 25,
        "monthly_payment": 10, "originated_on": "2025-01-02", "evidence_links": [
            {"file_id": str(document_id), "role": "agreement"}, {"file_id": str(hidden_id), "role": "payoff"}]},
        {"id": b, "lender": "Same SBA", "original_amount": 200, "balance": 150,
         "monthly_payment": 20, "status": "dismissed"}]}
    version = SimpleNamespace(id=uuid4(), version_number=2, source="form", status="published", created_at=datetime.now(UTC),
        pdf_file_id=file_id, source_file_id=None, body=body, key_facts={"total_outstanding_balance": 25})
    family = SimpleNamespace(id=uuid4(), profile_id=profile_id, kind="debt_schedule", label="Schedule")
    before = deepcopy(version.__dict__)
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(family, version)])))
    result = await publication.current_version_context(db, profile_id=profile_id, visible_file_ids={file_id, document_id})
    rows = result[0]["debt_rows"]
    assert len(rows) == 1 and rows[0]["row_id"] == a and rows[0]["paid_down_pct"] == 75
    assert rows[0]["originated_on"] == "2025-01-02" and rows[0]["original_amount"] == 100
    assert rows[0]["documents"] == [{"file_id": str(document_id), "role": "agreement"}]
    assert str(hidden_id) not in str(result) and b not in str(rows)
    assert version.__dict__ == before
    assert await publication.current_version_context(db, visible_file_ids={document_id}) == []
    version.body["debts"][0]["monthly_payment"] = None
    partial = await publication.current_version_context(db, visible_file_ids={file_id})
    assert not partial[0]["authoritative"] and "debt_rows" not in partial[0]
