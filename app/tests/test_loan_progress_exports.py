"""Future exports preserve accepted loan identity and derive balance reduction only."""

from copy import deepcopy
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from app.services import dealer_forms_pdf as pdf
from app.services import debt_package_evidence as evidence
from app.services import financial_templates_xlsx as xlsx
from app.services.financial_statements import DEBT_COLUMNS
from app.services.loan_progress import format_paid_down_pct, paid_down_pct
from app.services.public_underwriting_packet_pdf import _debt_evidence_section


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.widths = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.current = []
            self.rows.append(self.current)
            self.widths.append(0)
        elif tag in {"th", "td"} and self.current is not None:
            self.current.append("")
            self.widths[-1] += int(dict(attrs).get("colspan", "1"))

    def handle_data(self, data):
        if self.current:
            self.current[-1] += data

    def handle_endtag(self, tag):
        if tag == "tr":
            self.current = None


def loan(**fields):
    return {"id": str(uuid4()), "lender": "SBA", "status": "active", "original_amount": "1000", "balance": "800", "monthly_payment": "100", "originated_on": "2024-01-15", **fields}


def test_future_schedule_pdf_columns_and_totals_align_without_an_aggregate_percentage():
    rows = [loan(notes="Reviewed <source>"), loan(original_amount="500", balance="600")]
    before = deepcopy(rows)
    html = pdf.build_debt_schedule_html(business_name="Synthetic", total_balance=1400, total_monthly=200, rows=rows)
    parsed = Rows()
    parsed.feed(html)
    assert "% paid down" in parsed.rows[0] and "Origination date" in parsed.rows[0]
    assert len(parsed.rows[0]) == 16
    assert all(width == 16 for width in parsed.widths)
    assert "20.0%" in parsed.rows[1] and "01/15/2024" in parsed.rows[1]
    assert any("-20.0%" in row for row in parsed.rows)
    assert not any("%" in cell for cell in parsed.rows[-1])
    assert "Balance reduction, not cumulative payments or fees" in html
    assert html.count('class="disclaimer"') == 1
    assert pdf.FORM_DISCLAIMER in html and "Submitted electronically" in html
    assert "&lt;source&gt;" in html
    assert rows == before
    legacy = pdf.build_debt_schedule_html(business_name="Synthetic", total_balance=800, total_monthly=100, debts=[("SBA", 800, 100)])
    assert "Balance reduction, not cumulative payments or fees" not in legacy
    assert pdf.FORM_DISCLAIMER in legacy and "Submitted electronically" in legacy


@pytest.mark.parametrize("original,balance,expected", [("1000", "0", "100.0%"), (None, "0", "—"), ("0", "100", "—"), ("-5", "1", "—"), ("1000", "-1", "—"), ("100000", "99851", "0.1%")])
def test_future_pdf_does_not_substitute_payoff_factor_or_payment_history(original, balance, expected):
    row = loan(original_amount=original, balance=balance, factor_rate="1.5", payoff_amount="999", originated_on="invalid", uploaded_at="2026-11-07")
    parsed = Rows()
    parsed.feed(pdf._schedule_row_html(row))
    assert parsed.rows[0][5] == expected
    assert parsed.rows[0][8] == "—"


def test_future_pdf_formats_the_same_known_money_values_used_by_the_percentage():
    parsed = Rows()
    parsed.feed(pdf._schedule_row_html(loan(original_amount="$100,000", balance="75,000")))
    assert parsed.rows[0][3:6] == ["$100,000.00", "$75,000.00", "25.0%"]


def test_xlsx_percent_is_a_locked_ratio_formula_after_the_17_canonical_inputs():
    wb = load_workbook(BytesIO(xlsx.build_workbook.__wrapped__("debt_schedule")))
    ws = wb.active
    first = wb.defined_names["ds.r1.lender"].attr_text.split("$")[-1]
    row = int(first)
    percent, monthly = ws.cell(row, 18), ws.cell(row, 19)
    assert ws.cell(row - 2, 18).value == "Paid down (%)"
    assert percent.protection.locked and monthly.protection.locked
    assert percent.number_format == "0.0%"
    assert percent.value == f'=IFERROR(IF(AND(ISNUMBER(C{row}),C{row}>0,ISNUMBER(D{row}),D{row}>=0),(C{row}-D{row})/C{row},""),"")'
    assert "ROUND" not in percent.value and "MAX(" not in percent.value
    assert "not cumulative payments" in percent.comment.text
    assert monthly.value.startswith("=IF(ISNUMBER(F")
    assert ws.cell(row - 2, 19).value == "Monthly equivalent (calculated)"
    assert ws.cell(row - 1, 18).value is None
    assert {name.removeprefix("ds.r1.") for name in wb.defined_names if name.startswith("ds.r1.")} == set(DEBT_COLUMNS)
    assert not any("paid_down" in name or name == "ds.None" for name in wb.defined_names)
    assert not ws.cell(row, 7).protection.locked and ws.cell(row, 7).number_format == "yyyy-mm-dd"
    assert ws.cell(row - 2, 7).value == "Origination date"
    total_ref = wb.defined_names["ds.total_balance"].attr_text.rsplit("!", 1)[1].replace("$", "")
    total_row = ws[total_ref].row
    assert ws.cell(total_row, 18).value is None
    cached = load_workbook(BytesIO(xlsx.build_workbook.__wrapped__("debt_schedule")), data_only=True)
    assert cached.active.cell(row, 18).value is None


def test_package_progress_and_evidence_stay_on_the_exact_saved_obligation():
    file_a, file_b = uuid4(), uuid4()
    files = [SimpleNamespace(id=key, file_name=f"{label}.png", content_type="image/png", content_hash=label * 64, status="uploaded", deleted_at=None, document_version_status="current") for key, label in [(file_a, "a"), (file_b, "b")]]
    first = loan(loan_reference="COV6002057805", evidence_links=[{"file_id": str(file_a), "role": "agreement"}])
    second = loan(loan_reference="COV6002057806", original_amount="500", balance="600", originated_on="2025-02-16", evidence_links=[{"file_id": str(file_b), "role": "payoff", "is_current": True, "payoff_amount": "1"}])
    alias = loan(status="duplicate", duplicate_of_id=first["id"], original_amount="999999", balance="1", originated_on="2000-01-01", evidence_links=[{"file_id": str(file_b), "role": "supporting"}])
    version = SimpleNamespace(id=uuid4(), version_number=4, created_at=datetime(2026, 11, 7, tzinfo=UTC), body={"debts": [first, second, alias]}, pdf_file_id=uuid4(), body_sha256="c" * 64)
    before = deepcopy(version.body)
    result = evidence.build_manifest(version, files, scoped_ids=[file_a, file_b], latest_ids=[file_a, file_b], reviewed_payoff_ids=[file_b])
    one, two = result["debts"]
    assert one["row_id"] == first["id"] and two["row_id"] == second["id"]
    assert (one["original_amount"], one["balance"], one["originated_on"], one["paid_down_pct"]) == (1000, "800", "2024-01-15", 20)
    assert (two["original_amount"], two["balance"], two["originated_on"], two["paid_down_pct"]) == (500, "600", "2025-02-16", -20)
    assert one["documents"][1]["duplicate_row_ids"] == [alias["id"]]
    assert two["documents"][0]["current_payoff"] is True
    assert version.body == before
    html = _debt_evidence_section({"debt_evidence": result})
    one_html, two_html = html.split("loan COV6002057805", 1)[1].split("loan COV6002057806", 1)
    assert "2024-01-15" in one_html and "$1,000.00" in one_html and "20.0%" in one_html
    assert "2025-02-16" in two_html and "$500.00" in two_html and "-20.0%" in two_html
    assert first["id"] not in html and alias["id"] not in html
    assert evidence.loan_summary_text(one) in evidence.index_text(result)
    assert "paid down: 20.0%" in evidence.index_html(result)


def test_unknown_package_original_never_uses_a_payoff_or_factor_as_principal():
    row = {"original_amount": None, "balance": "0", "monthly_payment": "0", "factor_rate": "1.49", "payoff_amount": "1000", "uploaded_at": "2026-11-07", "paid_down_pct": 100}
    summary = evidence.loan_summary_text(row)
    assert "Origination date: Not provided" in summary
    assert "original amount: Not provided" in summary and "current balance: $0.00" in summary
    assert "paid down: —" in summary and "monthly payment: $0.00" in summary
    assert format_paid_down_pct(paid_down_pct("100000", "99851")) == "0.1%"
