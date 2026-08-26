from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import fitz

from app.dealer_os.services import contract_sign, qc_master_application
from app.dealer_os.services.lender_neutral_routing import (
    RULES_VERSION,
    TERM_DISPLAY_NAME,
    TERM_PROGRAM_KEY,
)


def _sample_context() -> dict:
    completed_at = datetime(2026, 8, 25, 14, 30, tzinfo=timezone.utc)
    profile = SimpleNamespace(
        human_review_status="fundable",
        human_review_note="Cash flow, ownership, and direct-program eligibility reviewed.",
    )
    pre_screen = SimpleNamespace(
        file_answers={"refinance_debt": True},
        completed_at=completed_at,
    )
    matched_rule = {
        "rule_id": "amount.term.range",
        "matched_value": "$250,000 requested",
        "explanation": "The requested amount is within the published direct-program range.",
    }
    routing = {
        "rules_version": RULES_VERSION,
        "programs": [
            {
                "program_key": TERM_PROGRAM_KEY,
                "name": TERM_DISPLAY_NAME,
                "status": "recommended",
                "matched_rules": [matched_rule],
                "borrower_safe_reasons": [],
                "unresolved": [],
            }
        ],
        "calculated_metrics": {"dscr": 1.42, "dscr_source": "verified cash flow and debt schedule"},
    }
    return {
        "generated_at": completed_at,
        "template_version": qc_master_application.MASTER_VERSION,
        "rules_version": RULES_VERSION,
        "case_ref": "QC-2026-SAMPLE",
        "business": {
            "legal_name": "Harbor Works LLC",
            "dba_name": "Harbor Works",
            "entity_type": "Limited liability company",
            "website": "https://harborworks.example",
            "state_of_formation": "NJ",
            "started_on": "March 15, 2018",
            "location_type": "Leased",
            "physical_address": "100 Market Street, Newark, NJ 07102",
            "mailing_address": "PO Box 210, Newark, NJ 07101",
            "email": "operations@harborworks.example",
            "phone": "+1 201 555 0110",
        },
        "taxonomy": {
            "industry": "Manufacturing",
            "subcategory": "Fabricated Metal Product Manufacturing",
            "naics_code": "332710",
            "naics_label": "Machine Shops",
            "status": "official",
            "canonical": True,
        },
        "request": {
            "amount": 250000.0,
            "original_amount": 250000.0,
            "term_months": 60,
            "purpose": "Refinance",
            "use_of_funds": "Refinance two existing business obligations and add working capital for materials.",
            "line_items": [
                {"description": "Debt refinance", "amount": 175000},
                {"description": "Working capital", "amount": 75000},
            ],
            "collateral": "Business assets; final collateral requirements remain subject to underwriting.",
        },
        "financial": {
            "annual_sales": 1850000.0,
            "annual_cash_flow_available_for_debt": 340000.0,
            "monthly_debt_payments": 20000.0,
            "dscr": 1.42,
            "dscr_source": "verified cash flow and debt schedule",
            "statement_months": ["2026-05", "2026-06", "2026-07"],
            "missing_statement_months": [],
        },
        "owners": [
            {
                "id": "sample-owner",
                "name": "Jordan Rivera",
                "ownership_pct": 100.0,
                "email": "jordan@harborworks.example",
                "phone": "+1 201 555 0121",
                "primary": True,
                "credit_required": True,
                "credit_status": "Completed - threshold met",
                "credit_reference": "credit-workflow-sample",
                "residency": "us_citizen",
                "bankruptcy": "none",
                "foreclosure": False,
                "felony": "none",
                "misdemeanor": False,
                "active_legal_charges": False,
                "ofac_match": False,
            }
        ],
        "primary_signer": {
            "name": "Jordan Rivera",
            "title": "Managing Member",
            "email": "jordan@harborworks.example",
        },
        "debts": [
            {
                "lender": "Existing commercial obligation",
                "category": "term loan",
                "balance": 175000.0,
                "monthly_payment": 20000.0,
                "maturity": "December 31, 2027",
                "ucc": True,
            }
        ],
        "documents": [
            {
                "name": "May 2026 Statement.pdf",
                "classification": "Bank Statement",
                "source": "Uploaded",
                "status": "extracted",
                "official_statement": True,
            },
            {
                "name": "June 2026 Statement.pdf",
                "classification": "Bank Statement",
                "source": "Uploaded",
                "status": "extracted",
                "official_statement": True,
            },
            {
                "name": "July 2026 Statement.pdf",
                "classification": "Bank Statement",
                "source": "Uploaded",
                "status": "extracted",
                "official_statement": True,
            },
            {
                "name": "Business Debt Schedule.pdf",
                "classification": "Debt Schedule",
                "source": "Uploaded",
                "status": "extracted",
                "official_statement": False,
            },
        ],
        "routing": routing,
        "route_key": TERM_PROGRAM_KEY,
        "route_label": TERM_DISPLAY_NAME,
        "route_status": "recommended",
        "route_reasons": [],
        "route_unresolved": [],
        "profile": profile,
        "pre_screen": pre_screen,
    }


def _signature_png() -> bytes:
    signature = fitz.open()
    page = signature.new_page(width=440, height=130)
    points = [
        (24, 84),
        (74, 34),
        (53, 92),
        (120, 47),
        (94, 91),
        (168, 58),
        (144, 91),
        (225, 67),
        (310, 80),
    ]
    for start, end in zip(points, points[1:]):
        page.draw_line(fitz.Point(*start), fitz.Point(*end), color=(0.04, 0.10, 0.24), width=2.2)
    return page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=True).tobytes("png")


def test_qc_master_application_readiness_and_pdf_security() -> None:
    context = _sample_context()
    readiness = qc_master_application.build_readiness(context)
    rendered = qc_master_application.render_html(context, readiness)

    assert readiness["ready"] is True
    assert "Qualified Commercial Business Financing Application" in rendered
    assert "332710" in rendered
    assert "credit-workflow-sample" in rendered
    assert "Social Security number" in rendered
    assert "123-45-6789" not in rendered
    assert "Quidity" not in rendered
    assert "raw credit score" in rendered


def test_master_application_stamp_replaces_unsigned_placeholders() -> None:
    unsigned = fitz.open()
    page = unsigned.new_page(width=612, height=792)
    page.insert_text((42, 580), "SIGNATURE OF AUTHORIZED REPRESENTATIVE")
    page.insert_text((42, 635), "Electronic signature")
    page.insert_text((360, 635), "Signed electronically after review")
    stamped = contract_sign._stamp(
        unsigned.tobytes(),
        qc_master_application.MASTER_TEMPLATE_KEY,
        typed_name="Jordan Rivera",
        signature_png=_signature_png(),
        signed_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    executed = fitz.open(stream=stamped, filetype="pdf")
    executed_text = "\n".join(result.get_text() for result in executed)

    assert "Electronic signature" not in executed_text
    assert "Signed electronically after review" not in executed_text
    assert "August 25, 2026" in executed_text
    assert executed[0].get_images(full=True)


def test_completion_certificate_describes_each_hash_stage() -> None:
    certificate_html = contract_sign._certificate_html(
        [
            ("Document SHA-256 (pre-signing)", "a" * 64),
            ("Signed agreement SHA-256 (before certificate)", "b" * 64),
        ],
        qc_master_application.MASTER_TITLE,
    )

    normalized = " ".join(certificate_html.split())
    assert "pre-signing SHA-256" in normalized
    assert "signed agreement SHA-256" in normalized
    assert "final executed-document SHA-256" in normalized


def _prepare_visual_sample(output_dir: Path) -> None:
    context = _sample_context()
    readiness = qc_master_application.build_readiness(context)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "application.html").write_text(
        qc_master_application.render_html(context, readiness),
        encoding="utf-8",
    )
    (output_dir / "signature.png").write_bytes(_signature_png())


def _prepare_visual_certificate(output_dir: Path) -> None:
    unsigned = (output_dir / "unsigned.pdf").read_bytes()
    signature = (output_dir / "signature.png").read_bytes()
    signed_at = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    stamped = contract_sign._stamp(
        unsigned,
        qc_master_application.MASTER_TEMPLATE_KEY,
        typed_name="Jordan Rivera",
        signature_png=signature,
        signed_at=signed_at,
    )
    (output_dir / "stamped.pdf").write_bytes(stamped)
    certificate_rows = [
        ("Agreement", qc_master_application.MASTER_TITLE),
        ("Case", "QC-2026-SAMPLE"),
        ("Client", "Harbor Works LLC"),
        ("Signer", "Jordan Rivera"),
        ("Signer title", "Managing Member"),
        ("Signature method", "Drawn on device"),
        ("Signature SHA-256", hashlib.sha256(signature).hexdigest()),
        ("Document SHA-256 (pre-signing)", hashlib.sha256(unsigned).hexdigest()),
        ("Signed agreement SHA-256 (before certificate)", hashlib.sha256(stamped).hexdigest()),
        ("E-SIGN consent recorded", signed_at.isoformat()),
        ("Signed at", signed_at.isoformat()),
        ("IP address", "198.51.100.25"),
        ("Device", "QC rendered-PDF visual inspection"),
    ]
    (output_dir / "certificate.html").write_text(
        contract_sign._certificate_html(certificate_rows, qc_master_application.MASTER_TITLE),
        encoding="utf-8",
    )


def _finalize_visual_sample(output_dir: Path, final_path: Path) -> None:
    executed = fitz.open(output_dir / "stamped.pdf")
    executed.insert_pdf(fitz.open(output_dir / "certificate.pdf"))
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(executed.tobytes(deflate=True))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Render the QC master-application visual sample.")
    parser.add_argument("mode", choices=("prepare", "certificate", "finalize"))
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("final_path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        _prepare_visual_sample(args.output_dir)
    elif args.mode == "certificate":
        _prepare_visual_certificate(args.output_dir)
    else:
        if args.final_path is None:
            parser.error("final_path is required in finalize mode")
        _finalize_visual_sample(args.output_dir, args.final_path)
