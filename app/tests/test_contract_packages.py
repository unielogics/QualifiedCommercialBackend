from __future__ import annotations

from datetime import UTC, datetime

import fitz
import pytest
from pydantic import ValidationError

from app.dealer_os.schemas import ContractEnvelopeGenerateRequest
from app.dealer_os.services import contract_fill, contract_packages, contract_sign


def _values(program_key: str) -> dict[str, str]:
    return {
        "selected_program": program_key,
        "owner_first": "Jordan",
        "owner_last": "Alvarez",
        "owner_email": "jordan@example.com",
        "owner_phone": "(555) 010-2288",
        "owner_ssn_notice": "Collected securely through credit authorization",
        "owner_pct": "100",
        "guaranty": "Personal",
        "owner_street": "123 Main Street",
        "owner_address_2": "N/A",
        "owner_city": "Newark",
        "owner_state": "NJ",
        "owner_zip": "07102",
        "welcome_email": "Yes",
        "biz_legal": "Alvarez Auto LLC",
        "biz_dba": "Alvarez Auto",
        "biz_industry": "Automobile Dealers",
        "biz_entity": "Limited liability company",
        "biz_office_space": "Leased",
        "biz_location_type": "Retail dealership",
        "biz_formation_state": "New Jersey",
        "biz_start": "05/2019",
        "biz_website": "alvarezauto.example",
        "business_stage": "existing",
        "biz_address": "456 Market Street",
        "biz_city": "Newark",
        "biz_state": "NJ",
        "biz_zip": "07105",
        "mail_address": "456 Market Street",
        "mail_city": "Newark",
        "mail_state": "NJ",
        "mail_zip": "07105",
        "mail_same_as_physical": "yes",
        "annual_sales": "$3,250,000",
        "amount_requested": "$250,000",
        "mca_balance": "$0",
        "sba_balance": "$72,500",
        "use_of_funds": "Purchase inventory and refinance one existing business obligation.",
        "business_dscr": "1.42x",
        "owner_count": "1",
        "ucc_filings": "1",
        "affiliates": "No",
        "signer_title": "Managing Member",
    }


def _render(program_key: str) -> contract_fill.FillResult:
    source = contract_packages.SOURCE_ASSET.read_bytes()
    return contract_fill.fill_pdf(
        contract_packages.PROGRAM_APPLICATION_KEY,
        source,
        _values(program_key),
        overlay_map=contract_packages.DEFAULT_OVERLAY_MAP,
    )


def _signature_png() -> bytes:
    document = fitz.open()
    page = document.new_page(width=300, height=90)
    points = [(12, 66), (48, 25), (76, 67), (112, 31), (148, 64), (193, 36), (255, 57)]
    for start, end in zip(points, points[1:], strict=False):
        page.draw_line(fitz.Point(*start), fitz.Point(*end), color=(0.04, 0.10, 0.24), width=2)
    return page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=True).tobytes("png")


def test_program_template_is_flat_immutable_source() -> None:
    source = contract_packages.SOURCE_ASSET.read_bytes()
    document = fitz.open(stream=source, filetype="pdf")

    assert source.startswith(b"%PDF")
    assert document.page_count == 1
    assert not list(document[0].widgets() or [])
    assert contract_packages._sha(source) == (
        "be378f13802c30696acd1209c04c57a2859e867d4a3d7cab74d8a293f2fec258"
    )


def test_ez_and_microcap_population_are_program_specific() -> None:
    ez = _render(contract_packages.EZ_PROGRAM_KEY)
    micro = _render(contract_packages.MICRO_PROGRAM_KEY)

    assert ez.missing == []
    assert ez.placed["Program: EZ Term"] == "X"
    assert "Program: MicroCap" not in ez.placed
    assert ez.placed["Business State"] == "NJ"
    assert ez.placed["Mailing State"] == "NJ"
    assert ez.placed["Address 2"] == "N/A"
    assert ez.placed["Business Dscr"] == "N/A"
    assert ez.placed["Affiliate businesses"] == "N/A"

    assert micro.missing == []
    assert micro.placed["Program: MicroCap"] == "X"
    assert "Program: EZ Term" not in micro.placed
    assert micro.placed["Business Dscr"] == "1.42x"
    assert micro.placed["Number Of Owners"] == "1"
    assert micro.placed["Active Ucc"] == "1"
    assert micro.placed["Affiliate businesses: No"] == "X"


def test_program_pdf_never_contains_raw_ssn_and_drawn_signature_is_visible() -> None:
    rendered = _render(contract_packages.EZ_PROGRAM_KEY)
    unsigned = fitz.open(stream=rendered.pdf, filetype="pdf")
    unsigned_text = "\n".join(page.get_text() for page in unsigned)

    assert "Collected securely through credit authorization" in unsigned_text
    assert "123-45-6789" not in unsigned_text

    signed = contract_sign._stamp(
        rendered.pdf,
        contract_packages.PROGRAM_APPLICATION_KEY,
        typed_name="Jordan Alvarez",
        signature_png=_signature_png(),
        signed_at=datetime(2026, 8, 27, 15, 30, tzinfo=UTC),
        signature_spots=contract_packages.signature_spots(
            contract_packages.DEFAULT_OVERLAY_MAP
        ),
    )
    executed = fitz.open(stream=signed, filetype="pdf")
    executed_text = "\n".join(page.get_text() for page in executed)

    assert executed[0].get_images(full=True)
    assert "August 27, 2026" in executed_text
    assert "123-45-6789" not in executed_text


def test_missing_debt_disclosures_are_not_invented_as_zero() -> None:
    values = _values(contract_packages.EZ_PROGRAM_KEY)
    values["mca_balance"] = ""
    values["sba_balance"] = ""

    missing = contract_packages._missing_for_program(
        values,
        [],
        contract_packages.EZ_PROGRAM_KEY,
    )

    assert "existing MCA balance (enter zero when none)" in missing
    assert "existing SBA balance (enter zero when none)" in missing


def test_multi_program_generation_request_preserves_explicit_overrides() -> None:
    payload = ContractEnvelopeGenerateRequest(
        program_keys=[
            contract_packages.EZ_PROGRAM_KEY,
            contract_packages.MICRO_PROGRAM_KEY,
        ],
        overrides=[
            {
                "program_key": contract_packages.MICRO_PROGRAM_KEY,
                "acknowledged": True,
                "note": "Client and rep confirmed the blocked submission path.",
            }
        ],
    )

    assert payload.program_key == contract_packages.EZ_PROGRAM_KEY
    assert payload.program_keys == [
        contract_packages.EZ_PROGRAM_KEY,
        contract_packages.MICRO_PROGRAM_KEY,
    ]
    assert payload.overrides[0].acknowledged is True


def test_generation_request_rejects_duplicate_programs_and_unselected_overrides() -> None:
    with pytest.raises(ValidationError, match="Each program may be selected only once"):
        ContractEnvelopeGenerateRequest(
            program_keys=[
                contract_packages.EZ_PROGRAM_KEY,
                contract_packages.EZ_PROGRAM_KEY,
            ]
        )

    with pytest.raises(ValidationError, match="must belong to a selected program"):
        ContractEnvelopeGenerateRequest(
            program_keys=[contract_packages.EZ_PROGRAM_KEY],
            overrides=[
                {
                    "program_key": contract_packages.MICRO_PROGRAM_KEY,
                    "acknowledged": True,
                }
            ],
        )


def test_combined_envelope_uses_stable_order_and_distinct_template_keys() -> None:
    assert contract_packages.ordered_program_keys(
        [contract_packages.MICRO_PROGRAM_KEY, contract_packages.EZ_PROGRAM_KEY]
    ) == [contract_packages.EZ_PROGRAM_KEY, contract_packages.MICRO_PROGRAM_KEY]
    assert contract_packages.envelope_document_key(
        contract_packages.PROGRAM_APPLICATION_KEY,
        contract_packages.EZ_PROGRAM_KEY,
        multiple=True,
    ) == "qc_program_application__ez"
    assert contract_packages.envelope_document_key(
        contract_packages.PROGRAM_APPLICATION_KEY,
        contract_packages.MICRO_PROGRAM_KEY,
        multiple=True,
    ) == "qc_program_application__micro"
