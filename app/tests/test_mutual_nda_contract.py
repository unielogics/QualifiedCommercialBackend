from __future__ import annotations

from app.enums import ContractType
from app.services.contract_templates import contract_document_hash, get_template_spec, render_contract_document


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
