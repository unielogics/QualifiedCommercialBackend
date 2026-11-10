"""What a business debt schedule may call an obligation.

One list, read by everything that shows or stores a "type of debt": the
borrower's form and the desk's editor (through the dashboard's mirror of this
list), the worksheet grid, the downloadable workbook, the PDF a lender is
sent, and the check that a submitted schedule is complete. The slug is what
`dos_debts.category` stores — twenty-four characters at most — and the label
is what a person reads.

Why a closed list: the column used to take whatever was typed, so the same
obligation arrived as "LOC", "loc" and "Line of Credit", and nothing
downstream could tell a merchant cash advance from a term loan. The advance
is the one that matters: a funder that lowered a remittance because sales
fell has changed what the schedule is reporting, and the form asks about it
only when the row says it is an advance.

The dashboard keeps the same list in `src/lib/debtTypes.ts`. Change both.
"""

from __future__ import annotations

import re
from typing import Any

DEBT_TYPES: tuple[tuple[str, str], ...] = (
    ("term_loan", "Term loan"),
    ("line_of_credit", "Line of credit"),
    ("sba_loan", "SBA loan"),
    ("equipment", "Equipment loan or lease"),
    ("vehicle", "Vehicle loan"),
    ("real_estate", "Commercial mortgage or real estate loan"),
    ("mca", "Merchant cash advance (MCA)"),
    ("revenue_based", "Revenue-based financing"),
    ("invoice_factoring", "Invoice factoring or A/R financing"),
    ("credit_card", "Business credit card"),
    ("floorplan", "Floorplan line"),
    ("lease", "Lease"),
    ("personal_loan", "Personal loan used for the business"),
    ("tax_payment_plan", "Tax liability or payment plan"),
    ("other", "Other"),
)
DEBT_TYPE_SLUGS: tuple[str, ...] = tuple(slug for slug, _ in DEBT_TYPES)
DEBT_TYPE_LABELS: dict[str, str] = dict(DEBT_TYPES)

#: The width of `dos_debts.category`. A value we do not recognise is kept as
#: typed, and has to fit.
CATEGORY_WIDTH = 24
assert all(len(slug) <= CATEGORY_WIDTH for slug in DEBT_TYPE_SLUGS)

#: The kinds the schedule asks its follow-up question of — whether the funder
#: modified the advance's terms or payments. The owner asked for the
#: question on an MCA; revenue-based financing is the same product under the
#: name most daily-pull funders now sell it by, and the cockpit already reads
#: both as advances (`financial_cockpit.MCA_CATEGORIES`), so both are asked.
ADVANCE_DEBT_TYPES: frozenset[str] = frozenset({"mca", "revenue_based"})

#: Exact words the system itself has used for a type, read as the slug they
#: mean before any guessing: the cockpit's advance categories, and the three
#: short forms a desk types.
_ALIASES: dict[str, str] = {
    "merchant_cash_advance": "mca",
    "merchant cash advance": "mca",
    "cash_advance": "mca",
    "cash advance": "mca",
    "advance": "mca",
    "advances": "mca",
    "loc": "line_of_credit",
    "term": "term_loan",
    "mortgage": "real_estate",
    "auto": "vehicle",
}

#: Words a typed value is read for, in priority order — the specific before
#: the generic: "property tax plan" is a tax plan before it is property, "SBA
#: term loan" an SBA loan before a term loan, "revolving credit card" a card
#: before a line, "equipment lease" equipment finance before a lease. Matched
#: on word boundaries after `_`/`-` are folded to spaces, so "auto" never reads
#: "automatic" and "merchant_cash_advance" reads as its words. Deliberately
#: short of hints like "card", "property" or bare "auto": a wrong slug on a
#: lender's schedule is worse than the typed text kept as it was.
_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tax_payment_plan", ("tax", "irs")),
    ("mca", ("mca", "merchant cash", "cash advance", "merchant advance")),
    ("revenue_based", ("revenue based", "rbf")),
    ("sba_loan", ("sba",)),
    ("credit_card", ("credit card",)),
    ("line_of_credit", ("line of credit", "loc", "credit line", "revolver", "revolving")),
    ("floorplan", ("floorplan", "floor plan")),
    ("equipment", ("equipment",)),
    ("vehicle", ("vehicle", "auto loan", "truck", "car loan")),
    ("real_estate", ("mortgage", "real estate")),
    ("invoice_factoring", ("factoring", "receivable", "receivables", "a/r", "invoice")),
    ("lease", ("lease",)),
    ("personal_loan", ("personal",)),
    ("term_loan", ("term",)),
)

#: The column's historic default. It was written by code, never chosen by a
#: person, so it reads as "not stated".
_LEGACY_DEFAULT = "loan"


def normalize_debt_type(value: Any) -> str | None:
    """A debt type as the schedule stores it, or None for "not stated".

    The form sends a slug; a typed value ("LOC", "Term", "Floorplan") or a
    label picked in a spreadsheet is read for the slug it means. Anything
    unrecognised is kept as typed, trimmed to the column, so a word a borrower
    chose is not thrown away for failing to be one of ours — it just does not
    get the advance question.
    """
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        return None
    folded = text.casefold()
    if folded == _LEGACY_DEFAULT:
        return None
    if folded in DEBT_TYPE_LABELS:
        return folded
    if folded in _ALIASES:
        return _ALIASES[folded]
    spaced = " ".join(re.sub(r"[_\-]+", " ", folded).split())
    if spaced in _ALIASES:
        return _ALIASES[spaced]
    for slug, label in DEBT_TYPES:
        if spaced == label.casefold():
            return slug
    for slug, hints in _HINTS:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])", spaced) for hint in hints):
            return slug
    return text[:CATEGORY_WIDTH]


def debt_type_label(value: Any) -> str | None:
    """What a person reads for a stored type: the label for a slug we know,
    the text itself for one we kept as typed, nothing for "not stated"."""
    slug = normalize_debt_type(value)
    if slug is None:
        return None
    return DEBT_TYPE_LABELS.get(slug, slug)


def is_advance_type(value: Any) -> bool:
    """Whether a row of this type is asked the advance question."""
    return normalize_debt_type(value) in ADVANCE_DEBT_TYPES
