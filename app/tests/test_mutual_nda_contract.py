from __future__ import annotations

import base64
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from app.enums import ContractType
from app.models.public_contract_sign_session import PublicContractSignSession
from app.routers.contracts import _PUBLIC_SESSION_TTL
from app.services.contract_templates import (
    contract_document_hash,
    get_template_spec,
    render_contract_certificate_pdf,
    render_contract_document,
)


def _values() -> dict:
    return {
        "effective_date": "2026-08-24",
        "counterparty_legal_name": "Example Capital LLC",
        "counterparty_entity_type": "Limited liability company",
        "counterparty_state_of_formation": "New Jersey",
        "counterparty_principal_address": "1 Main Street, Newark, NJ 07102",
        "counterparty_signer_name": "Jane Doe",
        "counterparty_signer_title": "Managing Member",
        "counterparty_signer_email": "jane@example.com",
        "counterparty_signature_date": "2026-08-24",
        "qc_signatory_name": "Jonathan Franco",
        "qc_signature_date": "2026-08-24",
        "preexisting_relationship_declaration": (
            "The following pre-existing relationships are disclosed before execution of this Agreement."
        ),
        "preexisting_relationship_rows": [
            {
                "name": "Example Bank",
                "category": "Bank",
                "description": "Operating relationship",
                "start_date": "2020-01-01",
            }
        ],
    }


def test_mutual_nda_renders_populated_agreement_and_exhibit_a() -> None:
    contract_type = ContractType.MUTUAL_NDA_NON_CIRCUMVENTION
    spec = get_template_spec(contract_type)
    rendered = render_contract_document(contract_type, _values())

    assert spec.document_version == "2026-08-24-1"
    assert len(spec.sections) == 19
    assert "Example Capital LLC" in rendered.plain_text
    assert "Example Bank | Bank | Operating relationship | 2020-01-01" in rendered.plain_text
    assert "page 3 of 3" not in rendered.plain_text
    assert "$counterparty" not in rendered.plain_text
    assert len(contract_document_hash(rendered)) == 64


def test_mutual_nda_preserves_supplied_non_circumvention_exception() -> None:
    rendered = render_contract_document(ContractType.MUTUAL_NDA_NON_CIRCUMVENTION, _values())

    assert "main banks, institutions, or entities" in rendered.plain_text
    assert "pre-existing relationship prior to the execution" in rendered.plain_text
    assert "State of New Jersey" in rendered.plain_text


def test_public_signing_session_is_resilient_and_idempotency_linked() -> None:
    assert _PUBLIC_SESSION_TTL == timedelta(minutes=60)
    agreement_column = PublicContractSignSession.__table__.c.agreement_id
    assert agreement_column.unique is True
    assert {foreign_key.target_fullname for foreign_key in agreement_column.foreign_keys} == {
        "contract_agreements.id"
    }


def test_signed_pdf_contains_signature_evidence_and_visible_signature_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = render_contract_document(ContractType.MUTUAL_NDA_NON_CIRCUMVENTION, _values())
    # Valid 1x1 PNG. The production canvas supplies a much larger image, but a
    # tiny deterministic asset is sufficient to verify PDF embedding.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    signature_datauri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    signature_hash = "a" * 64

    captured: dict[str, str] = {}

    class FakeHTML:
        def __init__(self, *, string: str):
            captured["html"] = string

        def write_pdf(self) -> bytes:
            return b"%PDF-test"

    monkeypatch.setitem(sys.modules, "weasyprint", types.SimpleNamespace(HTML=FakeHTML))

    pdf = render_contract_certificate_pdf(
        rendered=rendered,
        contract_type=ContractType.MUTUAL_NDA_NON_CIRCUMVENTION,
        contract_number="QC-NDA-2026-000001",
        typed_name="Jane Doe",
        document_hash=contract_document_hash(rendered),
        ip_address="198.51.100.10",
        user_agent="Agreement test",
        signed_at=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        signer_signature_datauri=signature_datauri,
        signature_hash=signature_hash,
    )

    assert pdf == b"%PDF-test"
    certificate_html = captured["html"]
    assert "QC-NDA-2026-000001" in certificate_html
    assert "Signature SHA-256" in certificate_html
    assert signature_hash in certificate_html
    assert "By: Jane Doe" in certificate_html
    assert "By: Jonathan Franco" in certificate_html
    assert certificate_html.count('class="contract-sig"') == 2


def test_signed_pdf_keeps_counterparty_drawing_when_signer_matches_qc_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _values()
    values["counterparty_signer_name"] = "Jonathan Franco"
    rendered = render_contract_document(ContractType.MUTUAL_NDA_NON_CIRCUMVENTION, values)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    signature_datauri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    captured: dict[str, str] = {}

    class FakeHTML:
        def __init__(self, *, string: str):
            captured["html"] = string

        def write_pdf(self) -> bytes:
            return b"%PDF-test"

    monkeypatch.setitem(sys.modules, "weasyprint", types.SimpleNamespace(HTML=FakeHTML))

    render_contract_certificate_pdf(
        rendered=rendered,
        contract_type=ContractType.MUTUAL_NDA_NON_CIRCUMVENTION,
        contract_number="QC-NDA-2026-000002",
        typed_name="Jonathan Franco",
        document_hash=contract_document_hash(rendered),
        ip_address="198.51.100.10",
        user_agent="Agreement test",
        signed_at=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        signer_signature_datauri=signature_datauri,
        signature_hash="b" * 64,
    )

    certificate_html = captured["html"]
    assert certificate_html.count('alt="Qualified Commercial signature"') == 1
    assert certificate_html.count('alt="Counterparty signature"') == 1
