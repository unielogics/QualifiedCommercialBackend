"""Real PDF content, pagination and private-identifier regressions."""
from copy import deepcopy
from datetime import UTC, datetime

import fitz

from app.capital_partners.services.ppm_pdf import render_ppm_pdf


def fixture():
    return {"title": "Synthetic Wellness Offering", "listing_ref": "SYNTHETIC-PPM",
            "issuer": "Synthetic Issuer LLC", "source_fingerprint": "a" * 64,
            "missing": [{"key": "jurisdiction", "label": "Confirm governing law", "section": "legal", "required": True}],
            "warnings": ["Saved financial evidence remains subject to review."],
            "sections": [{"key": "terms", "title": "Saved financing terms", "paragraphs": ["Source-bound terms; no money is invented."],
                          "facts": [{"label": "Gross principal", "value": "USD 120,000.00"},
                                    {"label": "Six-month net profit", "value": "USD 5,400.00", "note": "Principal is not profit."}]},
                         {"key": "fees", "title": "Recorded charges", "paragraphs": [],
                          "table": {"columns": ["Charge", "Payer", "Timing", "Total"],
                                    "rows": [["Software monitoring", "Investor", "Monthly", "USD 1,200.00"]]}}]}


def test_saved_facts_and_all_long_table_content_survive_pagination_without_footer_overlap():
    data = fixture()
    data["sections"][1]["table"]["rows"] *= 95
    data["sections"].append({"key": "final", "title": "Final disclosure", "paragraphs": ["Z" * 350, "The final saved disclosure is retained."]})
    original = deepcopy(data)
    pdf = fitz.open(stream=render_ppm_pdf(data, version=3), filetype="pdf")
    text = " ".join(page.get_text() for page in pdf)
    assert len(pdf) >= 3
    for value in ("USD 120,000.00", "USD 5,400.00", "Confirm governing law", "The final saved disclosure is retained."):
        assert value in text
    assert text.count("Software monitoring") == 95
    assert all(f"Page {index} of {len(pdf)}" in page.get_text() for index, page in enumerate(pdf, 1))
    assert all(page.get_images() for page in pdf)
    assert data == original
    for page in pdf:
        for block in page.get_text("blocks"):
            if block[1] < 744 and block[4].strip() and block[6] == 0:
                assert block[3] < 738, block


def test_reviewed_representation_has_exact_facts_and_no_draft_badge():
    data = fixture()
    data["missing"] = []
    stamp = datetime(2026, 11, 11, 12, tzinfo=UTC)
    draft = render_ppm_pdf(data, version=1, generated_at=stamp)
    approved = render_ppm_pdf(data, version=2, status="reviewed", generated_at=stamp, reviewed_at="2026-11-11 12:01 UTC")
    assert draft != approved
    text = " ".join(page.get_text() for page in fitz.open(stream=approved, filetype="pdf"))
    assert "STAFF-REVIEWED VERSION" in text and "DRAFT - STAFF REVIEW REQUIRED" not in text
    assert "USD 120,000.00" in text and "USD 5,400.00" in text


def test_free_text_identifiers_are_scrubbed_and_active_markup_is_plain_text():
    data = fixture()
    data["sections"][0]["paragraphs"] = ["SSN 123-45-6789; EIN 12-3456789; account 123456789012; person@example.test; <script>alert('sample')</script>"]
    text = " ".join(page.get_text() for page in fitz.open(stream=render_ppm_pdf(data, version=1), filetype="pdf"))
    for value in ("123-45-6789", "12-3456789", "123456789012", "person@example.test"):
        assert value not in text
    assert "<script>" in text
