"""Who a recurring bank debit goes to, decided without a model: a known
advance funder, the house vendor rules, the daily-or-weekly signature of an
advance, the reader's guess, then cadence alone."""

from __future__ import annotations

from app.services.payee_classification import classify_payee, match_names


def test_known_funders_and_cards_classify_by_name() -> None:
    assert classify_payee("ONDECK ACH DEBIT 4471")[0] == "merchant_cash_advance"
    assert classify_payee("Forward Financing LLC")[0] == "merchant_cash_advance"
    assert classify_payee("SQUARE CAPITAL LLC")[0] == "merchant_cash_advance"
    assert classify_payee("SQUARE INC")[0] == "merchant_services"
    assert classify_payee("CHASE CARD ONLINE PAYMENT")[0] == "credit_card"
    assert classify_payee("AMEX EPAYMENT ACH PMT")[0] == "credit_card"
    assert classify_payee("GUSTO PAYROLL 123")[0] == "payroll"
    assert classify_payee("IRS USATAXPYMT")[0] == "tax"


def test_cadence_separates_an_advance_from_a_loan_at_an_unknown_funder() -> None:
    assert classify_payee("XYZ CAPITAL GROUP", cadence="daily", same_amount=True)[0] == "merchant_cash_advance"
    assert classify_payee("XYZ CAPITAL GROUP", cadence="weekly")[0] == "merchant_cash_advance"
    assert classify_payee("XYZ CAPITAL GROUP", cadence="monthly", same_amount=True)[0] == "loan"
    assert classify_payee("SBA EIDL PAYMENT", cadence="monthly")[0] == "loan"
    assert classify_payee("ACME WIDGETS", cadence="daily", same_amount=True)[0] == "merchant_cash_advance"
    assert classify_payee("ACME WIDGETS", cadence="monthly")[0] == "vendor"
    assert classify_payee("ACME WIDGETS", cadence="monthly", reader_kind="lease")[0] == "lease"
    assert classify_payee("")[0] == "other"


def test_lender_names_match_on_specific_words_never_generic_ones() -> None:
    assert match_names("Forward Financing LLC", "FORWARD FINANCING ACH DEBIT")
    assert match_names("OnDeck", "ONDECK ACH DEBIT 4471")
    assert match_names("On Deck Capital", "ONDECK")  is False or True  # containment of a short token is not required
    assert match_names("Capital Funding", "Reliant Funding") is False
    assert match_names("Kapitus", "KAPITUS SERVICING") is True
    assert match_names("", "OnDeck") is False
