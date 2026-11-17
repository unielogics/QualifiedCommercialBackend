"""One vocabulary for what a real-estate intake says about the deal.

The public start form, the staff create form, the room's application form and
the AI's proposed facts all describe the same few things in different words:
the transaction ("Purchase", "refi", "HELOC"), the credit tier ("Strong
Credit", "700+"), who lives in the property, and what kind of property it is.
Each answer is folded to one canonical value on the way in, so the desk never
again reads "Primaary" as a property type or "XXX" as a credit score.

Only write models fold. Files written before this vocabulary hold whatever was
typed, and read models hand those values back as stored: a read that rejects
an old value takes the whole page down with it.

Canonical transaction and credit values stay human labels (the forms, packets
and PDFs already print them as text). Occupancy and property type are slugs
with labels, because nothing printed them before and the application form
shares them with the real-estate schedule (`real_estate_schema`).
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from app.services import real_estate_schema

PURCHASE = "Purchase"
REFINANCE = "Refinance"
CASH_OUT = "Cash-out refinance"
HOME_EQUITY = "Home equity (HELOC)"
BRIDGE = "Bridge"
CONSTRUCTION = "Construction"
PORTFOLIO = "Portfolio review"
NOT_SURE = "Not sure yet"

TRANSACTION_TYPES: tuple[str, ...] = (
    PURCHASE,
    REFINANCE,
    CASH_OUT,
    HOME_EQUITY,
    BRIDGE,
    CONSTRUCTION,
    PORTFOLIO,
    NOT_SURE,
)
TRANSACTION_MESSAGE = (
    "Choose a transaction type: " + ", ".join(TRANSACTION_TYPES[:-1]) + f" or {NOT_SURE}."
)

STRONG_CREDIT = "Strong Credit"
MID_CREDIT = "Mid Credit"
BELOW_THRESHOLD = "Below Threshold"
CREDIT_TIERS: tuple[str, ...] = (STRONG_CREDIT, MID_CREDIT, BELOW_THRESHOLD, NOT_SURE)
CREDIT_MESSAGE = (
    "Choose Strong Credit, Mid Credit, Below Threshold or Not sure yet, "
    "or enter a credit score between 300 and 850."
)

INVESTMENT = "investment"
PRIMARY_RESIDENCE = "primary_residence"
SECOND_HOME = "second_home"
OCCUPANCY_LABELS: dict[str, str] = {
    INVESTMENT: "Investment property",
    PRIMARY_RESIDENCE: "Primary residence",
    SECOND_HOME: "Second home",
}
OWNER_OCCUPIED = frozenset({PRIMARY_RESIDENCE, SECOND_HOME})
OCCUPANCY_MESSAGE = "Choose who uses the property: investment property, primary residence or second home."

#: The real-estate schedule's own property types, so the intake, the schedule
#: and the application read one list. "unknown" is a schedule placeholder,
#: not an answer, and is never stored from the intake.
PROPERTY_TYPES: tuple[str, ...] = tuple(
    option
    for spec in real_estate_schema.PROPERTY_FIELDS
    if spec["key"] == "property_type"
    for option in spec.get("options", ())
    if option != "unknown"
)
PROPERTY_TYPE_LABELS: dict[str, str] = {
    "single_family": "Single family",
    "multi_family": "Multifamily (2+ units)",
    "condo": "Condo",
    "townhouse": "Townhouse",
    "commercial": "Commercial",
    "mixed_use": "Mixed-use",
    "office": "Office",
    "retail": "Retail",
    "industrial": "Industrial",
    "hospitality": "Hospitality",
    "self_storage": "Self storage",
    "land": "Land",
    "other": "Other",
}

PURPOSE_DESCRIPTION_MAX = 500


def _words(value: Any) -> str:
    """Lower-case words only: "Cash-out Refi." and "cash out refi" compare equal."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(phrase)}\b", text) for phrase in phrases)


# Order matters: "no cash out" before "cash out", "cash out refinance" before
# "refinance", and a home-equity line before either.
_TRANSACTION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("no cash out", "rate term", "rate and term"), REFINANCE),
    (
        (
            "heloc",
            "home equity",
            "equity line",
            "equity loan",
            "second mortgage",
            "2nd mortgage",
            "second lien",
            "2nd lien",
        ),
        HOME_EQUITY,
    ),
    (("cash out", "cashout"), CASH_OUT),
    (("refinance", "refi", "refinancing", "refinanced"), REFINANCE),
    (("bridge",), BRIDGE),
    (("construction", "ground up", "groundup", "new build", "build"), CONSTRUCTION),
    (("purchase", "purchasing", "buy", "buying", "acquisition", "acquire", "acquiring"), PURCHASE),
    (("portfolio", "blanket"), PORTFOLIO),
    (("not sure", "unsure", "unknown", "dont know", "don t know", "undecided", "not sure yet"), NOT_SURE),
)


def transaction_type_of(value: Any) -> str | None:
    """A stored or typed transaction as its canonical label, or None. Never raises."""
    text = _words(value)
    if not text:
        return None
    for label in TRANSACTION_TYPES:
        if text == _words(label):
            return label
    for phrases, label in _TRANSACTION_RULES:
        if _has(text, phrases):
            return label
    return None


def fold_transaction_type(value: Any) -> str | None:
    """Write-model fold: blank → None, a known way of saying it → its label, else refuse."""
    if value is None or not str(value).strip():
        return None
    label = transaction_type_of(value)
    if label is None:
        raise ValueError(TRANSACTION_MESSAGE)
    return label


def is_purchase(value: Any) -> bool:
    return transaction_type_of(value) == PURCHASE


def is_home_equity(value: Any) -> bool:
    return transaction_type_of(value) == HOME_EQUITY


_CREDIT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("not sure", "unsure", "unknown", "dont know", "don t know", "no idea"), NOT_SURE),
    (("below threshold", "below", "poor", "bad", "low", "weak", "challenged", "subprime"), BELOW_THRESHOLD),
    (("strong", "excellent", "great", "very good", "good", "prime"), STRONG_CREDIT),
    (("mid", "middle", "average", "fair", "ok", "okay", "decent"), MID_CREDIT),
)


def fold_credit_tier(value: Any) -> str | None:
    """A stated credit signal as one of the four tiers, or a score / range.

    "720", "700+", "FICO 680" and "720-759" keep their numbers (the DSCR screen
    reads them); anything that names no tier and no score in 300-850, such
    as "XXX", is refused.
    """
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    text = _words(raw)
    for label in CREDIT_TIERS:
        if text == _words(label):
            return label
    scores = [int(match) for match in re.findall(r"(?<!\d)\d{3}(?!\d)", raw)]
    if scores:
        if any(not 300 <= score <= 850 for score in scores):
            raise ValueError(CREDIT_MESSAGE)
        if len(scores) >= 2:
            low, high = sorted(scores[:2])
            return f"{low}-{high}"
        return f"{scores[0]}+" if "+" in raw else str(scores[0])
    for phrases, label in _CREDIT_RULES:
        if _has(text, phrases):
            return label
    raise ValueError(CREDIT_MESSAGE)


# Exact phrases only: a bare "second" also means a second lien.
_OCCUPANCY_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SECOND_HOME, ("second home", "2nd home", "vacation home", "vacation property", "seasonal home")),
    (
        INVESTMENT,
        (
            "investment property",
            "investment",
            "investor",
            "rental property",
            "rental",
            "rented",
            "non owner occupied",
            "non owner",
            "noo",
            "tenant occupied",
            "tenants",
        ),
    ),
    (
        PRIMARY_RESIDENCE,
        (
            "primary residence",
            "primary home",
            "primary",
            "owner occupied",
            "owner occupant",
            "my residence",
            "my home",
            "our home",
            "i live there",
            "we live there",
            "live in it",
            "homestead",
            "residence",
        ),
    ),
)

# Single tokens a short answer may misspell ("Primaary").
_OCCUPANCY_TOKENS: dict[str, str] = {
    "primary": PRIMARY_RESIDENCE,
    "residence": PRIMARY_RESIDENCE,
    "investment": INVESTMENT,
    "rental": INVESTMENT,
    "vacation": SECOND_HOME,
}


def occupancy_in_text(value: Any) -> str | None:
    """Who uses the property, when the text says it in so many words. Never raises."""
    text = _words(value)
    if not text:
        return None
    for slug in OCCUPANCY_LABELS:
        if text == _words(slug) or text == _words(OCCUPANCY_LABELS[slug]):
            return slug
    for slug, phrases in _OCCUPANCY_PHRASES:
        if _has(text, phrases):
            return slug
    return None


def _close_occupancy(text: str) -> str | None:
    tokens = text.split()
    if not tokens or len(tokens) > 3:
        return None
    for token in tokens:
        match = difflib.get_close_matches(token, list(_OCCUPANCY_TOKENS), n=1, cutoff=0.82)
        if match:
            return _OCCUPANCY_TOKENS[match[0]]
    return None


def occupancy_of(value: Any) -> str | None:
    """Lenient read: exact phrases first, then a misspelled one-to-three word answer."""
    found = occupancy_in_text(value)
    if found:
        return found
    return _close_occupancy(_words(value))


def fold_occupancy(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    slug = occupancy_of(value)
    if slug is None:
        raise ValueError(OCCUPANCY_MESSAGE)
    return slug


def is_owner_occupied(value: Any) -> bool:
    return occupancy_of(value) in OWNER_OCCUPIED


_PROPERTY_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mixed_use", ("mixed use",)),
    ("self_storage", ("self storage", "mini storage", "storage facility", "storage units")),
    (
        "multi_family",
        (
            "multi family",
            "multifamily",
            "multiplex",
            "duplex",
            "triplex",
            "fourplex",
            "quadplex",
            "4plex",
            "two family",
            "2 family",
            "three family",
            "four family",
            "2 4 unit",
            "2 4 units",
            "apartment",
            "apartments",
            "apartment building",
        ),
    ),
    ("condo", ("condo", "condominium", "co op", "coop")),
    ("townhouse", ("townhouse", "townhome", "town house", "rowhouse", "row house")),
    (
        "single_family",
        ("single family", "sfr", "sfh", "single family home", "single family residence", "detached home", "house"),
    ),
    ("office", ("office", "office building")),
    ("retail", ("retail", "storefront", "strip center", "strip mall", "shopping center")),
    ("industrial", ("industrial", "warehouse", "flex space", "manufacturing")),
    ("hospitality", ("hotel", "motel", "hospitality", "inn", "bed and breakfast")),
    ("land", ("land", "vacant lot", "lot", "acreage", "parcel")),
    ("commercial", ("commercial",)),
    ("other", ("other",)),
)


def fold_property_type(value: Any) -> tuple[str | None, str | None]:
    """(property_type, occupancy) read from one free-text "property type".

    The chat used to store whatever the model wrote, so an occupancy answer
    ("Primaary") landed in the property-type slot. A recognisable property
    type comes back as its slug; an occupancy said in that slot comes back in
    the second position instead; anything else comes back as (None, None) and
    is not stored at all.
    """
    text = _words(value)
    if not text:
        return None, None
    for slug in PROPERTY_TYPES:
        if text == _words(slug) or text == _words(PROPERTY_TYPE_LABELS.get(slug, slug)):
            return slug, None
    for slug, phrases in _PROPERTY_PHRASES:
        if _has(text, phrases):
            return slug, None
    return None, occupancy_of(value)


def property_type_label(value: Any) -> str | None:
    slug, _ = fold_property_type(value)
    return PROPERTY_TYPE_LABELS.get(slug) if slug else None


def fold_business_purpose(value: Any) -> bool | None:
    """True for business or investment use, False for personal use, None if unclear."""
    if value is None or isinstance(value, bool):
        return value
    text = _words(value)
    if not text:
        return None
    if text in {"yes", "y", "true", "1"}:
        return True
    if text in {"no", "n", "false", "0"}:
        return False
    if _has(text, ("not business", "not for business", "non business", "not for my business")):
        return False
    personal = _has(text, ("personal", "family", "household", "consumer"))
    business = _has(text, ("business", "investment", "commercial", "company"))
    if personal != business:
        return business
    return None


def clean_purpose_description(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:PURPOSE_DESCRIPTION_MAX] or None
