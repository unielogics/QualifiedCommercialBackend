"""One registry of online forms, and the signature a requirement may configure."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.funding_program import FundingProgramRequirementWrite
from app.schemas.submission_batch import OfferingRequirement
from app.services import form_kinds
from app.services.form_kinds import FormConfigError, FormKind

# What the hard-coded lists said before the registry; existing kinds must not move.
OLD_FORM_KINDS_BY_KEY = {
    "owner_personal_financial_statement": ("pfs",),
    "business_debt_schedule": ("debt_schedule",),
    "refinance_debt_schedule": ("debt_schedule",),
    "ytd_p_and_l_balance_sheet": ("p_and_l", "balance_sheet"),
    "ytd_profit_and_loss": ("p_and_l",),
    "real_estate_schedule": ("real_estate_schedule",),
    "operating_expenses_and_earnings_plan": ("operating_plan",),
}
OLD_FINANCIAL = {"pfs", "debt_schedule", "p_and_l", "balance_sheet", "real_estate_schedule", "operating_plan"}
OLD_BY_CLASSIFICATION = {
    "personal_financial_statement": "pfs",
    "debt_schedule": "debt_schedule",
    "current_p_and_l": "p_and_l",
    "balance_sheet": "balance_sheet",
    "real_estate_schedule": "real_estate_schedule",
    "operating_plan": "operating_plan",
}
OLD_SLOT_CATEGORY = {"pfs": "Personal Financials", "debt_schedule": "Debts", "p_and_l": "Financials", "balance_sheet": "Financials", "real_estate_schedule": "Real estate", "operating_plan": "Financials"}
OLD_REQUIREMENT_KEYS = {
    "debt_schedule": "business_debt_schedule",
    "pfs": "owner_personal_financial_statement",
    "p_and_l": "ytd_p_and_l_balance_sheet",
    "balance_sheet": "ytd_p_and_l_balance_sheet",
    "real_estate_schedule": "real_estate_schedule",
    "operating_plan": "operating_expenses_and_earnings_plan",
}


def test_existing_kinds_read_exactly_as_the_old_lists_did() -> None:
    by_key = form_kinds.kinds_by_requirement_key()
    for key, kinds in OLD_FORM_KINDS_BY_KEY.items():
        assert by_key[key] == kinds
    assert OLD_FINANCIAL <= form_kinds.financial_kinds()
    for classification, kind in OLD_BY_CLASSIFICATION.items():
        assert form_kinds.kind_for_classification(classification) == kind
    for kind, category in OLD_SLOT_CATEGORY.items():
        assert form_kinds.slot_category(kind) == category
    for kind, key in OLD_REQUIREMENT_KEYS.items():
        assert form_kinds.requirement_key_for(kind) == key
    assert form_kinds.slot_category("unknown", "Credit") == "Credit"
    assert form_kinds.kind_for_classification("bank_statement") is None


def test_a_requirements_own_configured_form_wins_over_its_key() -> None:
    assert form_kinds.kinds_for_requirement("business_debt_schedule") == ("debt_schedule",)
    assert form_kinds.kinds_for_requirement("anything", None, {"form_kind": "pfs"}) == ("pfs",)
    assert form_kinds.kinds_for_requirement("anything", None, {"form_kind": "nope"}) == ()


@pytest.fixture
def signable_kind():
    kind = form_kinds.register(
        FormKind(
            key="test_application",
            label="Test application",
            slot_category="Application",
            classification="test_application",
            engine=form_kinds.ENGINE_CLIENT_FORM,
            requirement_keys=("signed_test_application",),
            signable=True,
            default_signature={"signers": ["borrower"], "consents": ["accuracy", "esign"]},
        )
    )
    yield kind
    form_kinds.KINDS.pop(kind.key, None)


def test_writes_refuse_what_the_registry_does_not_know() -> None:
    with pytest.raises(FormConfigError, match="Unknown online form"):
        form_kinds.normalize_collection_config({"form_kind": "mystery"}, requirement_kind="form")
    with pytest.raises(FormConfigError, match="cannot end in a signature"):
        form_kinds.normalize_collection_config({"form_kind": "pfs", "signature": {"required": True}}, requirement_kind="form")
    with pytest.raises(FormConfigError, match="Only a form requirement"):
        form_kinds.normalize_collection_config({"form_kind": "pfs"}, requirement_kind="document")
    # Reads keep the row and drop only what cannot be honoured.
    assert form_kinds.normalize_collection_config({"form_kind": "mystery", "x": 1}, strict=False) == {"form_kind": "mystery", "x": 1}
    assert form_kinds.normalize_collection_config({"form_kind": "pfs", "signature": {"required": True}}, strict=False) == {"form_kind": "pfs"}


def test_a_signature_is_normalized_from_the_kinds_defaults(signable_kind: FormKind) -> None:
    config = form_kinds.normalize_collection_config(
        {"form_kind": "test_application", "signature": {"required": True}}, requirement_kind="form"
    )
    assert config["signature"] == {"required": True, "signers": ["borrower"], "consents": ["accuracy", "esign"], "satisfies": []}

    config = form_kinds.normalize_collection_config(
        {
            "form_kind": "test_application",
            "signature": {
                "signers": ["coborrower"],
                "consents": ["credit_authorization", "information_sharing"],
                "satisfies": ["signed_credit_authorization"],
                "credit_reuse_days": 14,
            },
        },
        requirement_kind="form",
    )
    assert config["signature"]["signers"] == ["borrower", "coborrower"]
    assert config["signature"]["consents"] == ["credit_authorization", "information_sharing", "esign"]
    assert config["signature"]["satisfies"] == ["signed_credit_authorization"]
    assert config["signature"]["credit_reuse_days"] == 14
    assert form_kinds.signature_of(config) == config["signature"]
    assert form_kinds.signature_of({"form_kind": "test_application", "signature": {"required": False}}) is None


def test_a_signature_counts_as_a_credit_authorization_only_when_it_carries_one(signable_kind: FormKind) -> None:
    with pytest.raises(FormConfigError, match="credit authorization"):
        form_kinds.normalize_collection_config(
            {"form_kind": "test_application", "signature": {"consents": ["esign"], "satisfies": ["signed_credit_authorization"]}},
            requirement_kind="form",
        )
    with pytest.raises(FormConfigError, match="credit_reuse_days"):
        form_kinds.normalize_collection_config(
            {"form_kind": "test_application", "signature": {"credit_reuse_days": 900}}, requirement_kind="form"
        )
    with pytest.raises(FormConfigError, match="unknown consent"):
        form_kinds.normalize_collection_config(
            {"form_kind": "test_application", "signature": {"consents": ["vibes"]}}, requirement_kind="form"
        )


def test_several_sources_merge_to_the_strictest_signature() -> None:
    merged = form_kinds.merge_signatures(
        [
            {"signers": ["borrower"], "consents": ["accuracy", "esign"], "satisfies": [], "credit_reuse_days": 30},
            None,
            {"signers": ["borrower", "coborrower"], "consents": ["credit_authorization", "esign"], "satisfies": ["signed_credit_authorization"], "credit_reuse_days": 14},
        ]
    )
    assert merged == {
        "required": True,
        "signers": ["borrower", "coborrower"],
        "consents": ["accuracy", "esign", "credit_authorization"],
        "satisfies": ["signed_credit_authorization"],
        "credit_reuse_days": 14,
    }
    assert form_kinds.merge_signatures([None, {}]) is None


def test_consents_are_versioned_hashed_and_the_credit_text_is_the_certified_one() -> None:
    from app.services import document_signature

    credit = form_kinds.consent("credit_authorization")
    assert credit.text == document_signature.credit_authorization_document_text()
    assert credit.version == document_signature.CREDIT_AUTHORIZATION_DOCUMENT_VERSION
    assert credit.sha256 == document_signature.document_hash(credit.text)
    assert {item.key for item in form_kinds.consents()} == set(form_kinds.CONSENT_KEYS)
    assert all(item.draft for item in form_kinds.consents() if item.key in {"business_purpose", "accuracy", "information_sharing"})


def test_program_and_lender_requirement_writes_check_the_form() -> None:
    with pytest.raises(ValidationError, match="Unknown online form"):
        FundingProgramRequirementWrite(requirement_key="x_form", label="X form", requirement_kind="form", collection_config={"form_kind": "mystery"})
    written = FundingProgramRequirementWrite(requirement_key="pfs_form", label="PFS", requirement_kind="form", collection_config={"form_kind": "pfs"})
    assert written.collection_config == {"form_kind": "pfs"}
    with pytest.raises(ValidationError, match="cannot end in a signature"):
        OfferingRequirement(requirement_key="lender_pfs", label="PFS", requirement_kind="form", collection_config={"form_kind": "pfs", "signature": {"required": True}})


def test_the_catalog_lists_kinds_consents_and_satisfiers() -> None:
    catalog = form_kinds.catalog()
    assert {kind["key"] for kind in catalog["kinds"]} >= OLD_FINANCIAL
    assert {consent["key"] for consent in catalog["consents"]} == set(form_kinds.CONSENT_KEYS)
    assert catalog["satisfiers"] == [{"key": "signed_credit_authorization", "consent": "credit_authorization"}]
    assert catalog["signer_roles"] == ["borrower", "coborrower"]
