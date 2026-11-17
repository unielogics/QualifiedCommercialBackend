"""The real-estate intake folds every way of saying a fact to one value."""

from __future__ import annotations

import pytest

from app.services import real_estate_schema
from app.services import real_estate_vocabulary as vocab


@pytest.mark.parametrize(
    ("typed", "label"),
    [
        ("Purchase", vocab.PURCHASE),
        ("purchase", vocab.PURCHASE),
        ("buy a duplex", vocab.PURCHASE),
        ("Refinance", vocab.REFINANCE),
        ("refi", vocab.REFINANCE),
        ("no cash out refi", vocab.REFINANCE),
        ("rate/term refinance", vocab.REFINANCE),
        ("Cash-out refinance", vocab.CASH_OUT),
        ("cash out", vocab.CASH_OUT),
        ("HELOC", vocab.HOME_EQUITY),
        ("Heloc request under his primary residence", vocab.HOME_EQUITY),
        ("home equity line", vocab.HOME_EQUITY),
        ("second mortgage", vocab.HOME_EQUITY),
        ("bridge loan", vocab.BRIDGE),
        ("ground-up construction", vocab.CONSTRUCTION),
        ("Portfolio review", vocab.PORTFOLIO),
        ("Not sure yet", vocab.NOT_SURE),
        ("not sure", vocab.NOT_SURE),
    ],
)
def test_transaction_types_fold_to_the_form_labels(typed: str, label: str) -> None:
    assert vocab.fold_transaction_type(typed) == label


def test_an_unknown_transaction_is_refused_and_blank_is_none() -> None:
    assert vocab.fold_transaction_type("") is None
    assert vocab.fold_transaction_type(None) is None
    with pytest.raises(ValueError, match="Choose a transaction type"):
        vocab.fold_transaction_type("whatever works")
    # Reads never raise: an older file keeps whatever it holds.
    assert vocab.transaction_type_of("whatever works") is None


@pytest.mark.parametrize(
    ("typed", "tier"),
    [
        ("Strong Credit", vocab.STRONG_CREDIT),
        ("Mid Credit", vocab.MID_CREDIT),
        ("Below Threshold", vocab.BELOW_THRESHOLD),
        ("Not sure yet", vocab.NOT_SURE),
        ("good", vocab.STRONG_CREDIT),
        ("fair", vocab.MID_CREDIT),
        ("poor", vocab.BELOW_THRESHOLD),
        ("720", "720"),
        ("700+", "700+"),
        ("FICO 680", "680"),
        ("759-720", "720-759"),
    ],
)
def test_credit_tiers_and_scores_are_kept(typed: str, tier: str) -> None:
    assert vocab.fold_credit_tier(typed) == tier


@pytest.mark.parametrize("typed", ["XXX", "abc", "950", "200"])
def test_a_credit_value_that_names_nothing_is_refused(typed: str) -> None:
    with pytest.raises(ValueError, match="credit score between 300 and 850"):
        vocab.fold_credit_tier(typed)


@pytest.mark.parametrize(
    ("typed", "slug"),
    [
        ("Primaary", vocab.PRIMARY_RESIDENCE),
        ("primary", vocab.PRIMARY_RESIDENCE),
        ("Primary residence", vocab.PRIMARY_RESIDENCE),
        ("owner occupied", vocab.PRIMARY_RESIDENCE),
        ("Investment property", vocab.INVESTMENT),
        ("rental", vocab.INVESTMENT),
        ("non-owner occupied", vocab.INVESTMENT),
        ("second home", vocab.SECOND_HOME),
        ("vacation home", vocab.SECOND_HOME),
        ("investment", vocab.INVESTMENT),
    ],
)
def test_occupancy_reads_misspellings_and_synonyms(typed: str, slug: str) -> None:
    assert vocab.fold_occupancy(typed) == slug


def test_an_unreadable_occupancy_is_refused_and_second_lien_is_not_a_second_home() -> None:
    with pytest.raises(ValueError):
        vocab.fold_occupancy("somewhere nice")
    assert vocab.occupancy_in_text("a second lien behind the bank") is None


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("single-family", ("single_family", None)),
        ("SFR", ("single_family", None)),
        ("duplex", ("multi_family", None)),
        ("small multifamily", ("multi_family", None)),
        ("2-4 unit", ("multi_family", None)),
        ("condo", ("condo", None)),
        ("town house", ("townhouse", None)),
        ("commercial retail", ("retail", None)),
        ("warehouse", ("industrial", None)),
        ("mixed use building", ("mixed_use", None)),
        ("land", ("land", None)),
        # An occupancy said in the property-type slot goes to occupancy.
        ("Primaary", (None, vocab.PRIMARY_RESIDENCE)),
        ("investment", (None, vocab.INVESTMENT)),
        # Anything else is not stored at all.
        ("weird thing", (None, None)),
        ("", (None, None)),
    ],
)
def test_property_type_folds_or_routes_or_drops(typed: str, expected: tuple[str | None, str | None]) -> None:
    assert vocab.fold_property_type(typed) == expected


def test_property_types_are_the_real_estate_schedules_own() -> None:
    schedule = next(spec for spec in real_estate_schema.PROPERTY_FIELDS if spec["key"] == "property_type")
    assert set(vocab.PROPERTY_TYPES) == set(schedule["options"]) - {"unknown"}
    assert set(vocab.PROPERTY_TYPE_LABELS) == set(vocab.PROPERTY_TYPES)


@pytest.mark.parametrize(
    ("typed", "purpose"),
    [
        (True, True),
        (False, False),
        ("yes", True),
        ("No", False),
        ("personal use", False),
        ("for my business", True),
        ("not for business", False),
        ("investment", True),
        ("family and business", None),
        ("", None),
    ],
)
def test_business_purpose_reads_plain_answers(typed: object, purpose: bool | None) -> None:
    assert vocab.fold_business_purpose(typed) is purpose


def test_owner_occupied_and_purchase_helpers() -> None:
    assert vocab.is_owner_occupied("Primaary")
    assert vocab.is_owner_occupied("second_home")
    assert not vocab.is_owner_occupied("investment")
    assert vocab.is_purchase("purchase")
    assert not vocab.is_purchase("HELOC")
    assert vocab.is_home_equity("Home equity (HELOC)")
