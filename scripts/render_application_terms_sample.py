"""Build the print-ready HTML used to visually QA the client terms PDF."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from app.models.application_terms import ApplicationTermSheet
from app.services.application_terms_pdf import render_terms_html

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tmp" / "pdfs" / "qc-client-terms-sample.html"


def main() -> None:
    issued = datetime(2026, 9, 15, 14, 30, tzinfo=UTC)
    row = ApplicationTermSheet(
        version=3,
        is_current=True,
        status="issued",
        program_key="commercial_debt_refinance",
        program_name="Commercial Debt Refinance",
        amount=750_000,
        apr_pct=12.99,
        term_months=36,
        funder_type="private_fund",
        funder_name="Confidential capital partner",
        repayment_frequency="monthly",
        payments_per_year=12,
        custom_repayment_label=None,
        debt_service_treatment="refinance",
        retained_annual_debt_service=24_000,
        periodic_payment=25_265.32,
        payment_count=36,
        annual_new_debt_service=303_183.84,
        projected_annual_debt_service=327_183.84,
        cash_flow_value=490_776,
        cash_flow_label="Bankable annual EBITDA",
        current_annual_debt_service=408_980,
        dscr_before=1.20,
        dscr_after=1.50,
        dscr_method="business",
        dscr_status="ready",
        dscr_explanation="Current and projected DSCR are calculated from verified file evidence and the proposed payment schedule.",
        dscr_source="Verified file evidence: bankable EBITDA and current annual debt service",
        expiration_days=10,
        closing_estimate_days=7,
        issued_at=issued,
        expires_on=date(2026, 9, 25),
        co_brand_enabled=True,
        sponsor_name="UrChoice",
        client_note="This structure consolidates the obligations identified in the payoff schedule while preserving working-capital flexibility.",
        conditions=[
            "Final verification of payoff statements and retained obligations.",
            "Satisfactory completion of legal and funding-source documentation.",
            "No material adverse change before closing.",
        ],
        created_by_user_id=None,
        created_at=issued,
        updated_at=issued,
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        render_terms_html(row, business_name="Northstar Property Group LLC", client_name="Jordan Morgan"),
        encoding="utf-8",
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
