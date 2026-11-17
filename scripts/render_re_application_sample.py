"""Render the real estate loan application PDF from a fictional sample, to look at it.

Writes three files: the unsigned application as submitted, the same after the
borrower signs (signature and date stamped on the anchors) and the executed
copy (anchors cleared, a certificate page appended). It uses WeasyPrint when
it is installed (the production image) and the PyMuPDF fallback otherwise, so
run it inside the backend image to see what clients and lenders receive:

    docker run --rm -v "$OUT:/out" qcbackend:current \
        sh -lc 'cd /app && .venv/bin/python scripts/render_re_application_sample.py /out'

No database, no storage and no real person: every value below is made up.
"""

from __future__ import annotations

import base64
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.services import pdf_stamping, re_application_pdf
from app.services import re_application_schema as schema

# A one-pixel PNG stands in for a drawn signature.
SIGNATURE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

VALUES = {
    "request.loan_purpose": "heloc",
    "request.program_interest": "home_equity_business",
    "request.amount_requested": 150000,
    "request.occupancy": "primary_residence",
    "request.business_purpose": True,
    "request.purpose_description": "Working capital and equipment for the family landscaping company.",
    "request.use_of_funds": ["business_working_capital", "inventory_equipment"],
    "request.borrowing_as": "entity",
    "request.target_close_date": "2026-12-15",
    "property.street": "48 Sample Lane",
    "property.city": "Springfield",
    "property.state": "NJ",
    "property.zip": "07081",
    "property.county": "Union",
    "property.property_type": "single_family",
    "property.units": 1,
    "property.year_built": 1994,
    "property.original_purchase_price": 410000,
    "property.original_purchase_date": "2015-06-30",
    "property.first_lien_balance": 184321.5,
    "property.first_lien_payment": 2310,
    "property.first_lien_rate": 3.75,
    "property.first_lien_lender": "Sample Mortgage Co.",
    "property.as_is_value": 725000,
    "property.annual_taxes": 14200,
    "property.annual_insurance": 2100,
    "borrowers.borrower.first_name": "Jordan",
    "borrowers.borrower.last_name": "Sample",
    "borrowers.borrower.dob": "1978-04-12",
    "borrowers.borrower.ssn_last4": "1234",
    "borrowers.borrower.email": "jordan.sample@example.com",
    "borrowers.borrower.mobile_phone": "+19735550142",
    "borrowers.borrower.citizenship": "us_citizen",
    "borrowers.borrower.marital_status": "married",
    "borrowers.borrower.dependents_count": 2,
    "borrowers.borrower.street": "48 Sample Lane",
    "borrowers.borrower.city": "Springfield",
    "borrowers.borrower.state": "NJ",
    "borrowers.borrower.zip": "07081",
    "borrowers.borrower.housing": "own",
    "borrowers.borrower.monthly_housing_payment": 3820,
    "borrowers.borrower.years_at_address": 11,
    "borrowers.borrower.employer_name": "Sample Landscaping LLC",
    "borrowers.borrower.position": "Owner",
    "borrowers.borrower.employment_years": 14,
    "borrowers.borrower.self_employed": True,
    "borrowers.borrower.monthly_income": 18500,
    "borrowers.borrower.checking": 42000,
    "borrowers.borrower.savings": 61000,
    "borrowers.borrower.retirement": 180000,
    **{f"borrowers.borrower.{key}": False for key in (
        "decl_bankruptcy", "decl_foreclosure", "decl_judgments", "decl_lawsuit",
        "decl_federal_debt", "decl_support", "decl_comaker",
    )},
    "entity.legal_name": "Sample Landscaping LLC",
    "entity.entity_type": "llc",
    "entity.state_of_formation": "NJ",
    "entity.date_established": "2012-03-01",
    "entity.good_standing": True,
    "entity.street": "12 Commerce Way",
    "entity.city": "Union",
    "entity.state": "NJ",
    "entity.zip": "07083",
    "entity.phone": "+19735550100",
    "entity.employees": 9,
    "entity.business_type": "Landscaping services",
    "entity.ownership_pct": 100,
    "experience.properties_owned": 1,
    "financial.cash_to_close": 0,
    "financial.exit_strategy": "business_cash_flow",
    "financial.credit_score_estimate": 742,
    "closing.title_vesting": "joint",
    "closing.persons_on_title": "Jordan Sample and Casey Sample",
    "closing.access_contact_name": "Jordan Sample",
    "closing.access_contact_phone": "+19735550142",
}

LETTERHEAD = {"address": "Office address goes here", "phone": "(000) 000-0000", "email": "applications@example.com"}


def sample_body() -> dict:
    body = schema.empty_body()
    for path, value in VALUES.items():
        schema.set_at(body, path, value)
    return body


def main(out: Path) -> None:
    from app.dealer_os.services.contract_sign import _certificate_html
    from app.services.production_signing import _append_pages

    out.mkdir(parents=True, exist_ok=True)
    body = sample_body()
    when = datetime(2026, 11, 17, 15, 30, tzinfo=UTC)
    html = re_application_pdf.build_html(body, reference="QC-RE-SAMPLE", version_number=1, generated_at=when, letterhead=LETTERHEAD)
    unsigned = re_application_pdf.render_pdf(html)
    (out / "re-application-1-unsigned.pdf").write_bytes(unsigned)
    print("anchors:", sorted(pdf_stamping.find_anchors(unsigned)))

    signed, counts = pdf_stamping.stamp_party(unsigned, party="borrower", typed_name="Jordan Sample", signature_png=SIGNATURE, signed_at=when)
    (out / "re-application-2-borrower-signed.pdf").write_bytes(signed)
    print("stamped:", counts)

    executed = pdf_stamping.redact_remaining_anchors(signed)
    certificate = re_application_pdf.render_pdf(_certificate_html([
        ("Document", f"{schema.TITLE}, version 1"),
        ("Signer", "Jordan Sample"),
        ("Role", "Borrower"),
        ("Email", "jordan.sample@example.com"),
        ("Signed at", when.isoformat()),
        ("IP address", "203.0.113.10"),
        ("Agreed to", "Business purpose, Accuracy, Credit authorization, Information sharing, Electronic signature"),
        ("Unsigned document SHA-256", "0" * 64),
        ("Signature record", "sample"),
    ], "Electronic signature certificate"))
    (out / "re-application-3-executed.pdf").write_bytes(_append_pages(executed, certificate))
    try:
        import weasyprint  # noqa: F401

        print("renderer: WeasyPrint")
    except (ImportError, OSError):
        print("renderer: PyMuPDF fallback")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "tmp/pdfs"))
