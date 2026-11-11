"""Risk polarity and future payment assertions cannot be invented by PPM prose."""

import pytest

from app.capital_partners.services.ppm import RISK_FACTS, SectionSelection, _validated_ai


def selection(paragraph, source_key):
    return SectionSelection(section_order=["overview"], paragraphs=[{
        "section": "overview", "paragraph": paragraph, "source_keys": [source_key],
    }])


@pytest.mark.parametrize(("key", "source", "claim"), [
    ("coverage_risk", "Saved debt service coverage is below full coverage and requires review.",
        "Saved debt service coverage is full."),
    ("general_risk", RISK_FACTS["loss"], "Capital is not at risk."),
    ("payoff_risk", RISK_FACTS["estimates"],
        "Principal returned is profit."),
    ("use_of_proceeds", "Saved allocations require reconciliation with net financing proceeds.",
        "Saved allocations require no reconciliation with net financing proceeds."),
])
def test_risk_qualifiers_cannot_be_removed_or_reversed(key, source, claim):
    with pytest.raises(ValueError, match="protected risk or limitation"):
        _validated_ai(selection(claim, key), {key: source})


@pytest.mark.parametrize("modal", ["will", "shall", "must"])
def test_allowed_vocabulary_does_not_authorize_future_principal_payment(modal):
    # Every ordinary word is present in the source/connector vocabulary, but
    # a historical distinction does not promise a future payment.
    source = "Principal returned is separate from profit."
    with pytest.raises(ValueError, match="future financial assertion"):
        _validated_ai(selection(f"Principal {modal} be returned.", "business_context"), {"business_context": source})


@pytest.mark.parametrize(("key", "source"), [
    ("coverage_risk", "Saved debt service coverage is below full coverage and requires review."),
    ("general_risk", RISK_FACTS["loss"]),
    ("payoff_risk", RISK_FACTS["estimates"]),
])
def test_exact_server_risk_clauses_remain_usable(key, source):
    assert _validated_ai(selection(source.upper(), key), {key: source}) == {"overview": source}


def test_positive_business_paragraph_remains_draftable():
    source = "This offering relates to financing an operating business."
    paragraph = "This offering relates to an operating business and financing."
    assert _validated_ai(selection(paragraph, "business_context"), {"business_context": source}) == {"overview": paragraph}
