from app.services.bucket_evidence import (
    classifications_for_requested_doc,
    filename_evidence_classification,
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
    assert filename_evidence_classification("BalanceSheet.pdf") == "current_p_and_l"
    assert filename_evidence_classification("Business Debt Schedule.xlsx") == "debt_schedule"


def test_non_bank_statements_do_not_satisfy_business_bank_request() -> None:
    assert filename_evidence_classification("Merchant Processing Statement.pdf") == "merchant_processing_statement"
    assert filename_evidence_classification("Mortgage Statement.pdf") == "payoff_or_mortgage_statement"
    assert filename_evidence_classification("Income Statement 2026-06.pdf") == "current_p_and_l"
    assert filename_evidence_classification("Investment Statement 2026-06.pdf") is None
    assert "bank_statement" in classifications_for_requested_doc(
        "Last 6 months business bank statements", "Bank Statements"
    )
