"""Which kind of counterparty a recurring bank debit goes to.

The statement reader lists every payee debited more than once in a statement
(or debited on a daily or weekly cadence). This module decides, without a
model, whether that payee is a merchant cash advance funder, a loan, a credit
card, a lease, payroll, rent, tax, insurance, a utility, a transfer or a plain
vendor, so the financial cockpit can show advances against banks against
cards and reconcile the advances a client described out loud with what the
statements actually show.

Order of evidence: a known advance funder by name; the house vendor rules
(dealer_os/services/vendors.py); fixed daily or weekly debits to a lender-like
name, which is an advance's signature; the reader's own guess; cadence alone.
"""

from __future__ import annotations

import re

from app.dealer_os.services.vendors import _GENERIC_LENDER_TOKENS, classify_vendor, normalize_vendor

KINDS = (
    "merchant_cash_advance",
    "loan",
    "credit_card",
    "lease",
    "payroll",
    "rent",
    "tax",
    "insurance",
    "utilities",
    "merchant_services",
    "bank_fees",
    "owner_draw",
    "transfer",
    "vendor",
    "other",
)
KIND_LABELS = {
    "merchant_cash_advance": "Merchant cash advance",
    "loan": "Loan",
    "credit_card": "Credit card",
    "lease": "Lease",
    "payroll": "Payroll",
    "rent": "Rent",
    "tax": "Tax",
    "insurance": "Insurance",
    "utilities": "Utilities",
    "merchant_services": "Processing fees",
    "bank_fees": "Bank fees",
    "owner_draw": "Owner draw",
    "transfer": "Transfer",
    "vendor": "Vendor",
    "other": "Other",
    # inbound
    "customer": "Customer payments",
    "card_settlement": "Card settlements",
    "advance_funding": "Advance funded",
    "loan_proceeds": "Loan proceeds",
    "refund": "Refund",
    "owner_contribution": "Owner contribution",
}
_CREDIT_READER_KINDS = {
    "customer": "customer",
    "customer_payment": "customer",
    "card_settlement": "card_settlement",
    "merchant_settlement": "card_settlement",
    "transfer": "transfer",
    "loan_proceeds": "loan_proceeds",
    "advance_funding": "advance_funding",
    "funding": "advance_funding",
    "refund": "refund",
    "owner_contribution": "owner_contribution",
    "other": "other",
}
#: Kinds whose payments are contractual debt service (cards are reported beside them, not inside).
DEBT_KINDS = frozenset({"merchant_cash_advance", "loan", "lease"})
CARD_KINDS = frozenset({"credit_card"})
FAST_CADENCES = frozenset({"daily", "weekly"})

#: Advance funders by name, matched on word boundaries over the normalized
#: payee (upper case, digits and punctuation removed). Multi-word names first
#: so "SQUARE CAPITAL" is an advance while a bare "SQUARE" stays processing.
MCA_FUNDERS = (
    "ONDECK",
    "ON DECK",
    "KABBAGE",
    "FORWARD FINANCING",
    "FORWARDLINE",
    "CREDIBLY",
    "RAPID FINANCE",
    "RAPID ADVANCE",
    "RAPIDADVANCE",
    "RAPID CAPITAL",
    "KAPITUS",
    "FORA FINANCIAL",
    "FUNDBOX",
    "BLUEVINE",
    "CAN CAPITAL",
    "LENDINI",
    "LIBERTAS",
    "NATIONAL FUNDING",
    "RELIANT FUNDING",
    "EXPANSION CAPITAL",
    "FUNDING CIRCLE",
    "BIZ CREDIT",
    "SQUARE CAPITAL",
    "PAYPAL WORKING CAPITAL",
    "PAYPAL LOAN",
    "SHOPIFY CAPITAL",
    "AMEX MERCHANT FINANCING",
    "HEADWAY CAPITAL",
    "IOU FINANCIAL",
    "CFG MERCHANT",
    "YELLOWSTONE CAPITAL",
    "PEARL CAPITAL",
    "EVEREST BUSINESS FUNDING",
    "MANTIS FUNDING",
    "VELOCITY CAPITAL",
    "LG FUNDING",
    "WORLD BUSINESS LENDERS",
    "BYZFUNDER",
    "FUNDKITE",
    "GREENBOX CAPITAL",
    "MULLIGAN FUNDING",
    "QUICKBRIDGE",
    "FUNDATION",
    "SPLASH ADVANCE",
    "TORRO",
    "FUNDWORKS",
    "CLARIFY CAPITAL",
    "LEGEND FUNDING",
    "ALPINE FUNDING",
    "ARSENAL FUNDING",
    "CLEARFUND",
    "SAMSON FUNDING",
    "ELEVATE FUNDING",
    "FOX CAPITAL",
    "LOANME",
    "LOAN ME",
    "SMART BUSINESS FUNDING",
    "SPARTAN CAPITAL",
    "UNIQUE FUNDING",
    "FRESH FUNDING",
    "WAVE FUNDING",
    "KINGS CAPITAL",
    "LENDR",
    "CAPIFY",
    "ADVANCE FUNDS NETWORK",
    "VOX FUNDING",
    "SELLERS FUNDING",
    "SELLERSFUNDING",
    "NEWTEK",
    "BUSINESS BACKER",
    "GRAND CAPITAL",
    "DELTA BRIDGE",
    "LENDIO",
    "PREMIUM MERCHANT FUNDING",
    "RDM CAPITAL",
    "SNAP ADVANCES",
    "TVT CAPITAL",
    "LAST CHANCE FUNDING",
    "ITRIA",
    "MERCHANT CAPITAL SOURCE",
    "BITTY ADVANCE",
    "CORE BUSINESS FUNDING",
    "GOOD FUNDING",
    "BALBOA CAPITAL",
    "EXPRESS CAPITAL",
    "CREDIBLY",
    "KALAMATA",
    "PARAFIN",
    "STRIPE CAPITAL",
    "UPLYFT",
    "FUNDERA",
)
_MCA_COMPILED = tuple((re.compile(rf"\b{re.escape(name)}\b"), name) for name in MCA_FUNDERS)

_READER_KINDS = {
    "merchant_cash_advance": "merchant_cash_advance",
    "mca": "merchant_cash_advance",
    "advance": "merchant_cash_advance",
    "cash_advance": "merchant_cash_advance",
    "loan": "loan",
    "term_loan": "loan",
    "credit_card": "credit_card",
    "card": "credit_card",
    "lease": "lease",
    "equipment_lease": "lease",
    "payroll": "payroll",
    "rent": "rent",
    "tax": "tax",
    "insurance": "insurance",
    "utility": "utilities",
    "utilities": "utilities",
    "software": "vendor",
    "subscription": "vendor",
    "transfer": "transfer",
    "owner_draw": "owner_draw",
    "other": "other",
}
_LENDER_WORDS = frozenset(
    {
        "CAPITAL",
        "FUNDING",
        "FUNDINGS",
        "FUND",
        "ADVANCE",
        "ADVANCES",
        "FINANCE",
        "FINANCIAL",
        "LENDING",
        "LENDER",
        "LOAN",
        "LOANS",
    }
)


def classify_payee(
    payee: str,
    *,
    cadence: str | None = None,
    same_amount: bool | None = None,
    reader_kind: str | None = None,
    self_names: tuple[str, ...] = (),
) -> tuple[str, str]:
    """(kind, rationale) for a recurring debit's payee. Deterministic."""
    key = normalize_vendor(payee or "")
    if not key:
        return "other", "No payee name."
    text = f" {key} "
    for pattern, label in _MCA_COMPILED:
        if pattern.search(text):
            return "merchant_cash_advance", f"Known advance funder ({label})."
    category, rationale = classify_vendor(key, -1, self_names)
    cadence_key = str(cadence or "").strip().lower()
    fixed_fast = cadence_key in FAST_CADENCES and same_amount is not False
    lender_like = category == "loan" or any(token in _LENDER_WORDS for token in key.split(" "))
    if lender_like and category in {"loan", "vendor", "other"}:
        if fixed_fast:
            return (
                "merchant_cash_advance",
                "Fixed daily or weekly debits to a funding company: advance remittances.",
            )
        return "loan", rationale if category == "loan" else "Lender-like name."
    if category == "floorplan":
        return "loan", rationale
    if category == "bank_fees" and "card processor" in rationale:
        return "merchant_services", rationale
    if category == "rent" and "LEASE" in key.split(" "):
        return "lease", rationale
    if category in {
        "credit_card",
        "payroll",
        "tax",
        "insurance",
        "rent",
        "utilities",
        "merchant_services",
        "bank_fees",
        "owner_draw",
        "transfer",
    }:
        return category, rationale
    guess = _READER_KINDS.get(str(reader_kind or "").strip().lower().replace(" ", "_"))
    if guess:
        return guess, "The statement reader's reading of the payee."
    if cadence_key in FAST_CADENCES and same_amount:
        return (
            "merchant_cash_advance",
            "Fixed daily or weekly debits with no recognizable payee: typical of an advance.",
        )
    return ("vendor" if category == "vendor" else "other"), rationale


def significant_tokens(name: str) -> set[str]:
    key = normalize_vendor(name or "")
    return {
        token
        for token in key.split(" ")
        if token and token not in _GENERIC_LENDER_TOKENS and len(token) >= 3
    }


def match_names(left: str, right: str) -> bool:
    """Do two counterparty names describe the same lender? Exact normalized
    match, a shared specific word (never a generic finance word), or one name
    contained in the other once it is long enough to be specific."""
    a, b = normalize_vendor(left or ""), normalize_vendor(right or "")
    if not a or not b:
        return False
    if a == b:
        return True
    if significant_tokens(left) & significant_tokens(right):
        return True
    if (len(a) >= 5 and f" {a} " in f" {b} ") or (len(b) >= 5 and f" {b} " in f" {a} "):
        return True
    # "OnDeck" against "ON DECK CAPITAL": the same name with or without a space.
    compact_a, compact_b = a.replace(" ", ""), b.replace(" ", "")
    return (len(compact_a) >= 5 and compact_a in compact_b) or (
        len(compact_b) >= 5 and compact_b in compact_a
    )


def classify_credit(
    payee: str, *, reader_kind: str | None = None, self_names: tuple[str, ...] = ()
) -> tuple[str, str]:
    """(kind, rationale) for a recurring inbound payer: an advance landing,
    loan proceeds, card settlements, transfers, refunds, or customers."""
    key = normalize_vendor(payee or "")
    if not key:
        return "other", "No payer name."
    text = f" {key} "
    for pattern, label in _MCA_COMPILED:
        if pattern.search(text):
            return (
                "advance_funding",
                f"Known advance funder ({label}) paying in: an advance landing.",
            )
    category, rationale = classify_vendor(key, 1, self_names)
    if category == "loan":
        return "loan_proceeds", rationale
    if category == "merchant_services":
        return "card_settlement", rationale
    if category == "transfer":
        return "transfer", rationale
    if category == "owner_draw":
        return "owner_contribution", rationale
    if category == "tax":
        return "refund", rationale
    guess = _CREDIT_READER_KINDS.get(str(reader_kind or "").strip().lower().replace(" ", "_"))
    if guess:
        return guess, "The statement reader's reading of the payer."
    return "customer", "Inbound from a counterparty with no lender or processor name."
