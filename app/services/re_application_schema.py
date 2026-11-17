"""The Qualified Commercial real-estate loan application, declared once.

One declaration drives the client's form, the desk's read-back (filled out of
applicable fields, per section), the prefill's validation and the PDF. Franco
(2026-11-16): business-purpose loans only; last four of the SSN and the date of
birth, never a full SSN; a co-borrower signs for themselves.

Fields carry a type the coercion enforces and, where a field only matters for
some deals, an ``applies_when`` rule in the program-rule node shape:

    {"field": "request.loan_purpose", "in": ["purchase"]}
    {"all": [...]} / {"any": [...]} / {"not": {...}}
    {"field": "$.years_at_address", "lt": 2}   # "$." is the same borrower

A rule over a value nobody has given yet does not apply: the question that
decides it (loan purpose, occupancy, who is borrowing) is asked first.
Counts are over applicable fields only; the rest read "Not applicable".
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services import real_estate_vocabulary as vocab

KIND = "re_application"
SCHEMA_VERSION = "qc_rea.v1"
REQUIREMENT_KEY = "signed_re_application"
TITLE = "Qualified Commercial Real Estate Loan Application"

ROLES = ("borrower", "coborrower")
ROLE_LABELS = {"borrower": "Borrower", "coborrower": "Co-borrower"}

_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK "
    "OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI".split()
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MONEY_MAX = Decimal("10000000000")
TEXT_MAX = 300
TEXTAREA_MAX = 2000

LOAN_PURPOSES = (
    ("purchase", "Purchase"),
    ("rate_term_refi", "Refinance (rate and term)"),
    ("cash_out_refi", "Cash-out refinance"),
    ("heloc", "Home equity line (HELOC) for business"),
    ("bridge", "Bridge"),
    ("construction", "Construction"),
)
#: The intake's transaction labels (real_estate_vocabulary) as the application's purposes.
PURPOSE_BY_TRANSACTION = {
    vocab.PURCHASE: "purchase",
    vocab.REFINANCE: "rate_term_refi",
    vocab.CASH_OUT: "cash_out_refi",
    vocab.HOME_EQUITY: "heloc",
    vocab.BRIDGE: "bridge",
    vocab.CONSTRUCTION: "construction",
}
PROGRAMS = (
    ("dscr_rental", "DSCR rental"),
    ("fix_and_flip", "Fix and flip"),
    ("bridge", "Bridge"),
    ("ground_up", "Ground-up construction"),
    ("commercial", "Commercial property"),
    ("home_equity_business", "Home equity for a business"),
    ("not_sure", "Not sure"),
)
USES = (
    ("property_purchase", "Buy the property"),
    ("renovation", "Renovation or construction"),
    ("refinance_payoff", "Pay off the current loan"),
    ("business_working_capital", "Business working capital"),
    ("business_expansion", "Business expansion"),
    ("debt_consolidation", "Pay off business debt"),
    ("inventory_equipment", "Inventory or equipment"),
    ("other", "Other"),
)
OCCUPANCIES = tuple(vocab.OCCUPANCY_LABELS.items())
PROPERTY_TYPES = tuple((key, vocab.PROPERTY_TYPE_LABELS[key]) for key in vocab.PROPERTY_TYPES)
DOWN_PAYMENT_SOURCES = (
    ("savings", "Savings"),
    ("business_funds", "Business funds"),
    ("sale_of_property", "Sale of another property"),
    ("gift", "Gift"),
    ("borrowed", "Borrowed"),
    ("other", "Other"),
)
BORROWING_AS = (("individual", "In my own name"), ("entity", "Through a company or trust"))
CITIZENSHIP = (
    ("us_citizen", "U.S. citizen"),
    ("permanent_resident", "Permanent resident"),
    ("non_permanent_resident", "Non-permanent resident"),
    ("foreign_national", "Foreign national"),
)
MARITAL = (("married", "Married"), ("unmarried", "Unmarried"), ("separated", "Separated"))
HOUSING = (("own", "Own"), ("rent", "Rent"), ("rent_free", "Live rent-free"))
ENTITY_TYPES = (
    ("llc", "LLC"),
    ("corporation", "Corporation"),
    ("s_corporation", "S corporation"),
    ("partnership", "Partnership"),
    ("sole_proprietorship", "Sole proprietorship"),
    ("trust", "Trust"),
    ("other", "Other"),
)
EXIT_STRATEGIES = (
    ("sale", "Sell the property"),
    ("refinance", "Refinance into a long-term loan"),
    ("hold_and_rent", "Hold and rent"),
    ("business_cash_flow", "Repay from business cash flow"),
    ("other", "Other"),
)
VESTING = (("individual", "Individual"), ("joint", "Jointly"), ("entity", "Company"), ("trust", "Trust"))

OWNER_OCCUPIED = {"field": "request.occupancy", "in": sorted(vocab.OWNER_OCCUPIED)}
PURCHASE = {"field": "request.loan_purpose", "in": ["purchase"]}
OWNED = {"field": "request.loan_purpose", "in": ["rate_term_refi", "cash_out_refi", "heloc", "bridge", "construction"]}
PAYOFF = {"field": "request.loan_purpose", "in": ["rate_term_refi", "cash_out_refi", "bridge"]}
REHAB = {
    "any": [
        {"field": "request.program_interest", "in": ["fix_and_flip", "ground_up"]},
        {"field": "request.loan_purpose", "in": ["construction"]},
    ]
}
RENTAL = {"field": "request.occupancy", "in": ["investment"]}
EXIT = {
    "any": [
        {"field": "request.loan_purpose", "in": ["bridge", "construction", "heloc"]},
        {"field": "request.program_interest", "in": ["fix_and_flip", "ground_up", "bridge"]},
    ]
}
ENTITY = {"field": "request.borrowing_as", "eq": "entity"}


def _f(key: str, label: str, type_: str = "text", *, options=None, required: bool = True, applies_when=None, help_: str | None = None, sensitive: bool = False) -> dict[str, Any]:
    spec: dict[str, Any] = {"key": key, "label": label, "type": type_, "required": required}
    if options is not None:
        spec["options"] = [{"value": value, "label": label} for value, label in options]
    if applies_when is not None:
        spec["applies_when"] = applies_when
    if help_:
        spec["help"] = help_
    if sensitive:
        spec["sensitive"] = True
    return spec


_YEARS_AT_ADDRESS_UNDER_TWO = {"field": "$.years_at_address", "lt": 2}
_DECLARATIONS = (
    ("decl_bankruptcy", "Bankruptcy in the last 7 years?"),
    ("decl_foreclosure", "Foreclosure, short sale or deed in lieu in the last 7 years?"),
    ("decl_judgments", "Outstanding judgments or liens?"),
    ("decl_lawsuit", "A party to a lawsuit?"),
    ("decl_federal_debt", "Delinquent or in default on federal debt?"),
    ("decl_support", "Obligated to pay alimony, child support or separate maintenance?"),
    ("decl_comaker", "A co-maker, co-signer or guarantor on another note?"),
)
_ANY_DECLARATION = {"any": [{"field": f"$.{key}", "eq": True} for key, _ in _DECLARATIONS]}

SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "request",
        "label": "Loan request",
        "fields": [
            _f("loan_purpose", "What is the loan for?", "select", options=LOAN_PURPOSES),
            _f("program_interest", "Type of financing", "select", options=PROGRAMS, required=False),
            _f("amount_requested", "Amount requested", "money"),
            _f("occupancy", "Who uses the property?", "select", options=OCCUPANCIES),
            _f("business_purpose", "Is the loan for a business or investment purpose?", "yesno",
               help_="Qualified Commercial makes business-purpose loans only."),
            _f("purpose_description", "What will the business use the money for?", "textarea", applies_when=OWNER_OCCUPIED),
            _f("use_of_funds", "Use of funds", "multiselect", options=USES),
            _f("use_of_funds_detail", "Detail on the use of funds", "textarea", required=False),
            _f("borrowing_as", "Who is borrowing?", "select", options=BORROWING_AS),
            _f("down_payment", "Down payment", "money", applies_when=PURCHASE),
            _f("down_payment_source", "Where the down payment comes from", "select", options=DOWN_PAYMENT_SOURCES, applies_when=PURCHASE),
            _f("target_close_date", "Target closing date", "date", required=False),
        ],
    },
    {
        "key": "property",
        "label": "Subject property",
        "fields": [
            _f("street", "Property street address"),
            _f("unit", "Unit", required=False),
            _f("city", "City"),
            _f("state", "State", "state"),
            _f("zip", "ZIP code", "zip"),
            _f("county", "County", required=False),
            _f("property_type", "Property type", "select", options=PROPERTY_TYPES),
            _f("units", "Number of units", "integer"),
            _f("square_feet", "Square feet", "integer", required=False),
            _f("year_built", "Year built", "year", required=False),
            _f("under_contract", "Is the property under contract?", "yesno", applies_when=PURCHASE),
            _f("purchase_price", "Purchase price", "money", applies_when=PURCHASE),
            _f("contract_close_date", "Contract closing date", "date", required=False, applies_when=PURCHASE),
            _f("original_purchase_price", "Original purchase price", "money", applies_when=OWNED),
            _f("original_purchase_date", "Original purchase date", "date", applies_when=OWNED),
            _f("first_lien_balance", "First mortgage balance", "money", applies_when=OWNED,
               help_="Enter 0 if the property is owned free and clear."),
            _f("first_lien_payment", "First mortgage monthly payment", "money", required=False, applies_when=OWNED),
            _f("first_lien_rate", "First mortgage interest rate (%)", "percent", required=False, applies_when=OWNED),
            _f("first_lien_lender", "First mortgage lender", required=False, applies_when=OWNED),
            _f("second_lien_balance", "Second mortgage or line balance", "money", required=False, applies_when=OWNED),
            _f("second_lien_payment", "Second mortgage monthly payment", "money", required=False, applies_when=OWNED),
            _f("payoff_amount", "Payoff amount", "money", applies_when=PAYOFF),
            _f("as_is_value", "Estimated value today (as-is)", "money"),
            _f("rehab_budget", "Renovation or construction budget", "money", applies_when=REHAB),
            _f("arv", "Estimated value after the work (ARV)", "money", applies_when=REHAB),
            _f("monthly_rent_current", "Current monthly rent", "money", applies_when=RENTAL,
               help_="Enter 0 if the property is vacant."),
            _f("monthly_rent_market", "Market monthly rent", "money", required=False, applies_when=RENTAL),
            _f("annual_taxes", "Annual property taxes", "money"),
            _f("annual_insurance", "Annual insurance", "money"),
            _f("monthly_hoa", "Monthly HOA dues", "money", required=False),
        ],
    },
    {
        "key": "borrowers",
        "label": "Borrowers",
        "per_role": True,
        "fields": [
            _f("first_name", "Legal first name"),
            _f("middle_name", "Middle name", required=False),
            _f("last_name", "Legal last name"),
            _f("suffix", "Suffix", required=False),
            _f("dob", "Date of birth", "date", sensitive=True),
            _f("ssn_last4", "Last 4 digits of SSN", "ssn_last4", sensitive=True,
               help_="Only the last four digits. We never ask for or store a full Social Security number."),
            _f("email", "Email", "email"),
            _f("mobile_phone", "Mobile phone", "phone"),
            _f("home_phone", "Home phone", "phone", required=False),
            _f("citizenship", "Citizenship", "select", options=CITIZENSHIP),
            _f("marital_status", "Marital status", "select", options=MARITAL, applies_when=OWNER_OCCUPIED),
            _f("dependents_count", "Number of dependents", "integer", required=False, applies_when=OWNER_OCCUPIED),
            _f("street", "Home street address"),
            _f("city", "City"),
            _f("state", "State", "state"),
            _f("zip", "ZIP code", "zip"),
            _f("housing", "Do you own or rent your home?", "select", options=HOUSING),
            _f("monthly_housing_payment", "Monthly rent", "money", applies_when={"field": "$.housing", "eq": "rent"}),
            _f("years_at_address", "Years at this address", "integer"),
            _f("prev_street", "Previous street address", applies_when=_YEARS_AT_ADDRESS_UNDER_TWO),
            _f("prev_city", "Previous city", applies_when=_YEARS_AT_ADDRESS_UNDER_TWO),
            _f("prev_state", "Previous state", "state", applies_when=_YEARS_AT_ADDRESS_UNDER_TWO),
            _f("prev_zip", "Previous ZIP code", "zip", applies_when=_YEARS_AT_ADDRESS_UNDER_TWO),
            _f("employer_name", "Current employer or business", applies_when=OWNER_OCCUPIED),
            _f("position", "Position or title", applies_when=OWNER_OCCUPIED),
            _f("employment_years", "Years in this role", "integer", applies_when=OWNER_OCCUPIED),
            _f("self_employed", "Self-employed?", "yesno", applies_when=OWNER_OCCUPIED),
            _f("monthly_income", "Monthly income", "money", applies_when=OWNER_OCCUPIED),
            _f("prior_employer_name", "Prior employer", applies_when={"all": [OWNER_OCCUPIED, {"field": "$.employment_years", "lt": 2}]}),
            _f("other_income_monthly", "Other monthly income", "money", required=False, applies_when=OWNER_OCCUPIED),
            _f("other_income_source", "Source of other income", required=False, applies_when=OWNER_OCCUPIED),
            _f("checking", "Checking account balances", "money"),
            _f("savings", "Savings account balances", "money", required=False),
            _f("retirement", "Retirement account balances", "money", required=False),
            *(_f(key, label, "yesno") for key, label in _DECLARATIONS),
            _f("decl_down_payment_borrowed", "Is any part of the down payment borrowed or a gift?", "yesno", applies_when=PURCHASE),
            _f("decl_explanation", "Explain each yes above", "textarea", applies_when=_ANY_DECLARATION),
        ],
    },
    {
        "key": "entity",
        "label": "Borrowing company",
        "applies_when": ENTITY,
        "fields": [
            _f("legal_name", "Legal name of the company"),
            _f("dba", "Doing business as", required=False),
            _f("entity_type", "Company type", "select", options=ENTITY_TYPES),
            _f("ein", "Tax ID (EIN)", "ein"),
            _f("state_of_formation", "State of formation", "state"),
            _f("date_established", "Date established", "date"),
            _f("good_standing", "Is the company in good standing?", "yesno"),
            _f("street", "Business street address"),
            _f("city", "City"),
            _f("state", "State", "state"),
            _f("zip", "ZIP code", "zip"),
            _f("phone", "Main business phone", "phone"),
            _f("fax", "Fax", "phone", required=False),
            _f("email", "Business email", "email"),
            _f("website", "Website", required=False),
            _f("employees", "Number of employees", "integer"),
            _f("business_type", "Type of business"),
            _f("ownership_pct", "Your ownership (%)", "percent"),
        ],
    },
    {
        "key": "experience",
        "label": "Experience",
        "fields": [
            _f("projects_completed_36m", "Projects completed in the last 3 years", "integer",
               help_="Purchases, renovations, builds or rentals you completed."),
            _f("properties_owned", "Investment properties you own today", "integer"),
            _f("experience_notes", "Anything else about your experience", "textarea", required=False),
        ],
        "collections": {
            "other_properties": [
                _f("address", "Address"),
                _f("estimated_value", "Estimated value", "money", required=False),
                _f("mortgage_balance", "Mortgage balance", "money", required=False),
                _f("monthly_payment", "Monthly payment", "money", required=False),
                _f("monthly_rent", "Monthly rent", "money", required=False),
            ],
        },
    },
    {
        "key": "financial",
        "label": "Liquidity and credit",
        "fields": [
            _f("cash_to_close", "Cash available to close", "money"),
            _f("exit_strategy", "How the loan will be repaid", "select", options=EXIT_STRATEGIES, applies_when=EXIT),
            _f("credit_score_estimate", "Estimated middle credit score", "score",
               help_="Your best estimate; the soft credit check replaces it."),
        ],
    },
    {
        "key": "closing",
        "label": "Title and closing contacts",
        "fields": [
            _f("title_vesting", "How title will be held", "select", options=VESTING),
            _f("persons_on_title", "Names of everyone on title"),
            _f("title_company", "Attorney or title company", required=False),
            _f("title_contact_phone", "Attorney or title phone", "phone", required=False),
            _f("title_contact_email", "Attorney or title email", "email", required=False),
            _f("access_contact_name", "Appraisal and access contact", required=False),
            _f("access_contact_phone", "Access contact phone", "phone", required=False),
        ],
    },
    {
        "key": "notes",
        "label": "Additional information",
        "fields": [_f("notes", "Anything else about the loan or the borrowers", "textarea", required=False)],
    },
)

_SECTION_BY_KEY = {section["key"]: section for section in SECTIONS}
_FIELD_BY_PATH: dict[str, dict[str, Any]] = {}
for _section in SECTIONS:
    for _spec in _section["fields"]:
        if _section.get("per_role"):
            for _role in ROLES:
                _FIELD_BY_PATH[f"borrowers.{_role}.{_spec['key']}"] = _spec
        else:
            _FIELD_BY_PATH[f"{_section['key']}.{_spec['key']}"] = _spec


def describe() -> dict[str, Any]:
    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "title": TITLE,
        "roles": [{"key": role, "label": ROLE_LABELS[role]} for role in ROLES],
        "sections": copy.deepcopy(list(SECTIONS)),
    }


def empty_body() -> dict[str, Any]:
    body: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "has_coborrower": False}
    for section in SECTIONS:
        if section.get("per_role"):
            body[section["key"]] = {role: {} for role in ROLES}
        else:
            body[section["key"]] = {}
            for name in section.get("collections", {}):
                body[section["key"]][name] = []
    return body


def spec_for(path: str) -> dict[str, Any] | None:
    return _FIELD_BY_PATH.get(path)


def get_at(body: dict[str, Any], path: str) -> Any:
    node: Any = body
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def set_at(body: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = body
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    if value is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == []


# ---------------------------------------------------------------------------
# Typed values
# ---------------------------------------------------------------------------


class FieldProblem(ValueError):
    pass


def _option_value(spec: dict[str, Any], value: Any) -> str:
    text = str(value).strip()
    for option in spec.get("options", []):
        if text == option["value"] or text.casefold() == option["label"].casefold():
            return option["value"]
    raise FieldProblem(f"Choose one of the listed options for {spec['label'].lower()}")


def _money(value: Any) -> float:
    try:
        number = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        raise FieldProblem("Enter an amount in dollars") from None
    if not number.is_finite() or number < 0 or number > MONEY_MAX:
        raise FieldProblem("Enter an amount in dollars")
    return float(number.quantize(Decimal("0.01")))


def _integer(value: Any, *, low: int = 0, high: int = 100_000) -> int:
    if isinstance(value, bool):
        raise FieldProblem("Enter a whole number")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise FieldProblem("Enter a whole number") from None
    if number != number.to_integral_value() or not low <= number <= high:
        raise FieldProblem(f"Enter a whole number from {low} to {high}")
    return int(number)


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        parsed = value
    else:
        text = str(value).strip()[:10]
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(value).strip())
            if not match:
                raise FieldProblem("Enter a date like 2026-11-17") from None
            parsed = date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    if not 1900 <= parsed.year <= 2100:
        raise FieldProblem("Enter a real date")
    return parsed.isoformat()


def coerce_value(spec: dict[str, Any], value: Any) -> Any:
    """One value as its field's type, or FieldProblem. Blank is None."""
    if _blank(value):
        return None
    kind = spec["type"]
    if kind in ("text", "textarea"):
        text = " ".join(str(value).split()) if kind == "text" else str(value).strip()
        limit = TEXT_MAX if kind == "text" else TEXTAREA_MAX
        if len(text) > limit:
            raise FieldProblem(f"Keep this under {limit} characters")
        return text or None
    if kind == "select":
        return _option_value(spec, value)
    if kind == "multiselect":
        items = value if isinstance(value, list) else [value]
        out: list[str] = []
        for item in items:
            chosen = _option_value(spec, item)
            if chosen not in out:
                out.append(chosen)
        return out or None
    if kind == "money":
        return _money(value)
    if kind == "integer":
        return _integer(value)
    if kind == "year":
        return _integer(value, low=1800, high=2100)
    if kind == "percent":
        number = _money(value)
        if number > 100:
            raise FieldProblem("Enter a percentage from 0 to 100")
        return number
    if kind == "score":
        if str(value).strip().casefold() in {"unknown", "not sure", "dont know", "don't know"}:
            return "unknown"
        return _integer(value, low=300, high=850)
    if kind == "date":
        return _date(value)
    if kind == "yesno":
        if isinstance(value, bool):
            return value
        text = str(value).strip().casefold()
        if text in {"yes", "y", "true", "1"}:
            return True
        if text in {"no", "n", "false", "0"}:
            return False
        raise FieldProblem("Answer yes or no")
    if kind == "email":
        text = str(value).strip()
        if not _EMAIL.fullmatch(text):
            raise FieldProblem("Enter a complete email address")
        return text.lower()
    if kind == "phone":
        from app.schemas.phone import normalize

        normalized = normalize(str(value))
        if normalized is None:
            raise FieldProblem("Enter a complete phone number, like (704) 555-0123")
        return normalized
    if kind == "ssn_last4":
        digits = re.sub(r"\D", "", str(value))
        if len(digits) != 4:
            raise FieldProblem("Enter only the last 4 digits of the Social Security number")
        return digits
    if kind == "state":
        text = str(value).strip().upper()
        if text not in _STATES:
            raise FieldProblem("Enter the two-letter state")
        return text
    if kind == "zip":
        digits = re.sub(r"\D", "", str(value))
        if len(digits) == 9:
            return f"{digits[:5]}-{digits[5:]}"
        if len(digits) != 5:
            raise FieldProblem("Enter a 5-digit ZIP code")
        return digits
    if kind == "ein":
        digits = re.sub(r"\D", "", str(value))
        if len(digits) != 9:
            raise FieldProblem("Enter the 9-digit EIN, like 12-3456789")
        return f"{digits[:2]}-{digits[2:]}"
    raise FieldProblem("Unsupported field")


def coerce(raw: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """A body as the schema reads it, plus a message for every value refused.

    Unknown keys are dropped. A refused value is left out of the body (it is
    missing, never shown as an answer) and reported by path.
    """
    body = empty_body()
    problems: dict[str, str] = {}
    source = raw if isinstance(raw, dict) else {}
    body["has_coborrower"] = bool(source.get("has_coborrower"))
    for path, spec in _FIELD_BY_PATH.items():
        value = get_at(source, path)
        if _blank(value):
            continue
        try:
            coerced = coerce_value(spec, value)
        except FieldProblem as exc:
            problems[path] = str(exc)
            continue
        if coerced is not None:
            set_at(body, path, coerced)
    for section in SECTIONS:
        for name, specs in section.get("collections", {}).items():
            rows = get_at(source, f"{section['key']}.{name}")
            clean_rows: list[dict[str, Any]] = []
            for index, row in enumerate(rows if isinstance(rows, list) else []):
                if not isinstance(row, dict) or len(clean_rows) >= 50:
                    continue
                clean: dict[str, Any] = {}
                for spec in specs:
                    try:
                        value = coerce_value(spec, row.get(spec["key"]))
                    except FieldProblem as exc:
                        problems[f"{section['key']}.{name}.{index}.{spec['key']}"] = str(exc)
                        continue
                    if value is not None:
                        clean[spec["key"]] = value
                if clean:
                    clean_rows.append(clean)
            body[section["key"]][name] = clean_rows
    return body, problems


# ---------------------------------------------------------------------------
# What applies, and how complete it is
# ---------------------------------------------------------------------------


def _lookup(body: dict[str, Any], field: str, role: str | None) -> Any:
    if field.startswith("$."):
        return get_at(body, f"borrowers.{role}.{field[2:]}") if role else None
    return get_at(body, field)


def applies(node: Any, body: dict[str, Any], role: str | None = None) -> bool:
    if node is None:
        return True
    if not isinstance(node, dict):
        return False
    if "all" in node:
        return all(applies(child, body, role) for child in node["all"])
    if "any" in node:
        return any(applies(child, body, role) for child in node["any"])
    if "not" in node:
        return not applies(node["not"], body, role)
    value = _lookup(body, str(node.get("field", "")), role)
    if _blank(value):
        return False
    if "in" in node:
        return value in node["in"]
    if "eq" in node:
        return value == node["eq"]
    if "lt" in node:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value < node["lt"]
    if node.get("present"):
        return True
    return False


def active_roles(body: dict[str, Any]) -> tuple[str, ...]:
    return ROLES if body.get("has_coborrower") else ROLES[:1]


def applicable_paths(body: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(section key, path, spec) for every field that applies to this body, in order."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for section in SECTIONS:
        if not applies(section.get("applies_when"), body):
            continue
        for spec in section["fields"]:
            if section.get("per_role"):
                for role in active_roles(body):
                    if applies(spec.get("applies_when"), body, role):
                        out.append((section["key"], f"borrowers.{role}.{spec['key']}", spec))
            elif applies(spec.get("applies_when"), body):
                out.append((section["key"], f"{section['key']}.{spec['key']}", spec))
    return out


def completion(body: dict[str, Any], *, confirmed: Iterable[str] | None = None) -> dict[str, Any]:
    """Filled out of applicable required fields, per section.

    ``confirmed`` (when given) limits what counts as filled to those paths:
    a value the AI suggested is not an answer until the client confirms it.
    """
    allowed = set(confirmed) if confirmed is not None else None
    sections: list[dict[str, Any]] = []
    totals = {"filled": 0, "total": 0}
    for section in SECTIONS:
        sections.append({"key": section["key"], "label": section["label"], "filled": 0, "total": 0, "missing": [], "applies": applies(section.get("applies_when"), body)})
    by_key = {item["key"]: item for item in sections}
    for section_key, path, spec in applicable_paths(body):
        if not spec.get("required"):
            continue
        item = by_key[section_key]
        item["total"] += 1
        totals["total"] += 1
        if not _blank(get_at(body, path)) and (allowed is None or path in allowed):
            item["filled"] += 1
            totals["filled"] += 1
        else:
            item["missing"].append(path)
    return {
        "sections": sections,
        "filled": totals["filled"],
        "total": totals["total"],
        "missing": [path for item in sections for path in item["missing"]],
        "complete": totals["filled"] == totals["total"],
    }


def option_label(spec: dict[str, Any], value: Any) -> str:
    for option in spec.get("options", []):
        if option["value"] == value:
            return option["label"]
    return str(value)


def display(spec: dict[str, Any], value: Any, *, mask: bool = True) -> str:
    """A value as a person reads it ("$184,321.50", "Yes", "XXX-XX-1234")."""
    if _blank(value):
        return ""
    kind = spec["type"]
    if kind == "money":
        number = Decimal(str(value))
        return f"${number:,.2f}" if number != number.to_integral_value() else f"${number:,.0f}"
    if kind == "percent":
        return f"{value:g}%"
    if kind == "yesno":
        return "Yes" if value else "No"
    if kind == "select":
        return option_label(spec, value)
    if kind == "multiselect":
        return ", ".join(option_label(spec, item) for item in value)
    if kind == "ssn_last4":
        return f"XXX-XX-{value}" if mask else str(value)
    if kind == "score":
        return "Unknown" if value == "unknown" else str(value)
    if kind == "phone":
        digits = re.sub(r"\D", "", str(value))
        if len(digits) == 11 and digits.startswith("1"):
            return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
        return str(value)
    return str(value)


def signer_identity(body: dict[str, Any], role: str = "borrower") -> dict[str, Any]:
    """The identity a credit authorization needs, read from the application."""
    person = get_at(body, f"borrowers.{role}") or {}
    return {
        "legal_first_name": person.get("first_name"),
        "legal_last_name": person.get("last_name"),
        "dob": person.get("dob"),
        "street": person.get("street"),
        "city": person.get("city"),
        "state": person.get("state"),
        "zip": person.get("zip"),
    }
