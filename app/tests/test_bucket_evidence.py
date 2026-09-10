from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.bucket_evidence import (
    classifications_for_requested_doc,
    filename_evidence_classification,
    reconcile_uploaded_file,
    statement_months_from_filename,
)


def test_good_warranty_statement_names_are_recognized_with_months() -> None:
    name = (
        "Good Warranty Soluti-STATEMENT-02-06-2026-"
        "bf5f935c-2098-4ce6-a21e-7c28455e27e5.pdf"
    )

    assert filename_evidence_classification(name) == "bank_statement"
    assert statement_months_from_filename(name) == {"2026-02"}


def test_upload_uuid_does_not_add_a_second_statement_month() -> None:
    name = (
        "Good Warranty Soluti-STATEMENT-03-06-2026-"
        "07b0ef3b-524c-42e6-b9c1-4884906a4130.pdf"
    )

    assert statement_months_from_filename(name) == {"2026-03"}


def test_common_e_statement_names_support_iso_dates() -> None:
    assert filename_evidence_classification("eStmt_2026-01-30 (8).pdf") == "bank_statement"
    assert statement_months_from_filename("eStmt_2026-01-30 (8).pdf") == {"2026-01"}


def test_high_signal_financial_names_map_to_baseline_classes() -> None:
    assert filename_evidence_classification("2025_TaxReturn.pdf") == "tax_return"
    assert filename_evidence_classification("ProfitandLoss.pdf") == "current_p_and_l"
    assert filename_evidence_classification("BalanceSheet.pdf") == "balance_sheet"
    assert filename_evidence_classification("Business Debt Schedule.xlsx") == "debt_schedule"


def test_statement_keywords_map_to_their_own_classifications() -> None:
    assert classifications_for_requested_doc("Balance sheet", "Financials") == {"balance_sheet"}
    assert classifications_for_requested_doc("Income statement", "Financials") == {"current_p_and_l"}
    # The combined Main Street row accepts either document, so a single upload
    # of either kind still sees exactly one candidate row.
    combined = classifications_for_requested_doc("Year-to-date P&L and balance sheet", "Financials")
    assert combined == {"current_p_and_l", "balance_sheet"}
    # A P&L-only row is no longer satisfied by a balance sheet.
    assert "balance_sheet" not in classifications_for_requested_doc("Current year P&L", "Financials")
    assert "balance_sheet" not in classifications_for_requested_doc("Profit and loss", "Financials")


def _row(name: str, category: str = "Financials") -> SimpleNamespace:
    return SimpleNamespace(id=f"row-{name}", name=name, category=category, allow_multiple_files=True, status="pending")


def _upload(file_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="file-1",
        deleted_at=None,
        status="uploaded",
        source_detail=None,
        file_name=file_name,
        statement_period=None,
        requested_document_id=None,
        bucket_id="00000000-0000-0000-0000-000000000001",
    )


async def _reconcile(file_name: str, rows: list[SimpleNamespace]):
    db = AsyncMock()
    scalars = MagicMock()
    scalars.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=scalars)
    return await reconcile_uploaded_file(db, _upload(file_name))


@pytest.mark.asyncio
async def test_the_combined_main_street_row_reconciles_either_statement_once() -> None:
    combined = _row("Year-to-date P&L and balance sheet")
    rows = [combined, _row("Last 2 years business tax returns"), _row("Business debt schedule", "Debts")]
    assert (await _reconcile("BalanceSheet.pdf", rows)) is combined
    assert (await _reconcile("ProfitandLoss.pdf", rows)) is combined
    assert combined.status == "uploaded"


@pytest.mark.asyncio
async def test_a_pl_only_row_no_longer_takes_a_balance_sheet() -> None:
    pl_only = _row("Current year P&L")
    assert (await _reconcile("BalanceSheet.pdf", [pl_only])) is None
    assert (await _reconcile("ProfitandLoss.pdf", [pl_only])) is pl_only


def test_the_download_template_filenames_route_to_their_rows() -> None:
    assert filename_evidence_classification("Qualified Commercial - Balance Sheet.xlsx") == "balance_sheet"
    assert filename_evidence_classification("Qualified Commercial - Profit and Loss Statement.xlsx") == "current_p_and_l"
    assert filename_evidence_classification("Qualified Commercial - Business Debt Schedule.xlsx") == "debt_schedule"
    assert (
        filename_evidence_classification("Qualified Commercial - Personal Financial Statement.xlsx")
        == "personal_financial_statement"
    )


def test_non_bank_statements_do_not_satisfy_business_bank_request() -> None:
    assert filename_evidence_classification("Merchant Processing Statement.pdf") == "merchant_processing_statement"
    assert filename_evidence_classification("Mortgage Statement.pdf") == "payoff_or_mortgage_statement"
    assert filename_evidence_classification("Income Statement 2026-06.pdf") == "current_p_and_l"
    assert filename_evidence_classification("Investment Statement 2026-06.pdf") is None
    assert "bank_statement" in classifications_for_requested_doc(
        "Last 6 months business bank statements", "Bank Statements"
    )


# ---------------------------------------------------------------------------
# Coverage against a slot the borrower has actually satisfied.
# ---------------------------------------------------------------------------


def _coverage(requested, analyses, slot_files=None):
    from app.services.bucket_ai import _merge_per_file_analyses

    result: dict = {}
    _merge_per_file_analyses(
        result, analyses, requested_documents=requested, slot_files=slot_files or {}
    )
    return {
        row["category"]: row
        for row in result["document_evidence_map"]["baseline_coverage"]
    }


def test_a_satisfied_slot_is_covered_even_when_the_file_reads_as_something_else():
    """A borrower dropped an MCA agreement into the Debts slot. The analyzer
    classified it correctly — it is an advance contract, not a schedule — and
    coverage then reported the request as missing, which it is not. All four
    routes to satisfying a form end at `status == "uploaded"`."""
    rows = _coverage(
        requested=[{"id": "slot-1", "name": "Debt schedule", "category": "Debts", "status": "uploaded"}],
        analyses=[{"file_id": "f1", "file_name": "advance.pdf", "ai_classification": "floorplan_mca_inventory"}],
        slot_files={"slot-1": ["advance.pdf"]},
    )
    assert rows["Debt schedule"]["status"] == "satisfied"
    # And names what answered it, so the desk can see what actually arrived.
    assert rows["Debt schedule"]["evidence"] == ["advance.pdf"]


def test_a_slot_satisfied_before_analysis_says_so_rather_than_going_quiet():
    rows = _coverage(
        requested=[{"id": "slot-2", "name": "Personal financial statement", "category": "Personal Financials", "status": "uploaded"}],
        analyses=[{"file_id": "f9", "file_name": "unrelated.pdf", "ai_classification": "bank_statement"}],
        slot_files={},
    )
    row = rows["Personal financial statement"]
    assert row["status"] == "satisfied"
    assert "not analyzed yet" in row["gap"]


def test_an_outstanding_slot_is_still_missing():
    """The change must not turn the checklist into a row of green ticks."""
    rows = _coverage(
        requested=[{"id": "slot-3", "name": "Debt schedule", "category": "Debts", "status": "requested"}],
        analyses=[{"file_id": "f1", "file_name": "bank.pdf", "ai_classification": "bank_statement"}],
        slot_files={},
    )
    assert rows["Debt schedule"]["status"] == "missing"
    assert rows["Debt schedule"]["gap"] == "No matching document analyzed yet."


def test_a_classification_match_still_wins_and_names_the_file():
    rows = _coverage(
        requested=[{"id": "slot-4", "name": "Debt schedule", "category": "Debts", "status": "uploaded"}],
        analyses=[{"file_id": "f2", "file_name": "schedule.pdf", "ai_classification": "debt_schedule"}],
        slot_files={"slot-4": ["schedule.pdf", "stray.pdf"]},
    )
    assert rows["Debt schedule"]["evidence"] == ["schedule.pdf"]
