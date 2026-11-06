"""A dated real estate schedule, kept separate from business debt and DSCR."""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from fastapi import HTTPException

KIND = "real_estate_schedule"
SCHEMA_VERSION = "qc_res.v1"
MAX_ROWS = 500


def field(key, label, input="text", options=None):
    return {"key": key, "label": label, "type": input, "input": input, **({"options": list(options)} if options else {})}


HEADER = (field("owner_name", "Owner / entity"), field("as_of", "As of", "date"), field("no_real_estate", "No real estate owned", "boolean"), field("notes", "Notes"))
PROPERTY_FIELDS = (
    field("address", "Street address"), field("city", "City"), field("state", "State / region"), field("postal_code", "Postal code"), field("country", "Country"),
    field("property_type", "Property type", "select", ("single_family", "multi_family", "condo", "townhouse", "commercial", "mixed_use", "office", "retail", "industrial", "hospitality", "self_storage", "land", "other", "unknown")),
    field("use", "Property use", "select", ("primary_residence", "second_home", "rental", "short_term_rental", "fix_and_flip", "development", "business_use", "vacant", "other", "unknown")),
    field("legal_owner", "Legal owner"), field("ownership_type", "Ownership", "select", ("individual", "joint", "entity", "trust", "other", "unknown")),
    field("ownership_pct", "Ownership (%)", "percent"), field("units", "Units", "number"), field("purchase_date", "Purchase date", "date"),
    field("purchase_cost", "Purchase cost", "money"), field("estimated_value", "Estimated value", "money"), field("valuation_date", "Valuation date", "date"),
    field("valuation_basis", "Valuation basis", "select", ("appraisal", "broker_opinion", "assessment", "owner_estimate", "purchase_price", "other", "unknown")),
    field("monthly_gross_rent", "Monthly gross rent", "money"), field("monthly_property_tax", "Monthly property tax", "money"),
    field("monthly_insurance", "Monthly insurance", "money"), field("monthly_hoa", "Monthly HOA / dues", "money"),
    field("monthly_maintenance", "Monthly maintenance", "money"), field("monthly_management", "Monthly management", "money"),
    field("monthly_utilities", "Monthly utilities", "money"), field("monthly_other_expenses", "Other monthly operating expenses", "money"),
    field("mortgage_status", "Mortgage status", "select", ("active", "free_clear", "unknown")),
)
MORTGAGE_FIELDS = (
    field("property_row_id", "Property"), field("lender", "Lender"), field("loan_reference", "Loan reference / last four (no SSN)"), field("status", "Mortgage status", "select", ("active", "paid_off", "unknown")),
    field("balance", "Current balance", "money"), field("monthly_payment", "Monthly payment", "money"), field("rate", "Interest rate (%)", "rate"),
    field("payment_basis", "Payment basis", "select", ("principal_interest", "including_escrow", "unknown")), field("monthly_escrow", "Monthly escrow included in payment", "money"),
    field("mortgage_type", "Mortgage / lien type"), field("maturity_date", "Maturity date", "date"), field("statement_date", "Statement date", "date"),
)
COLLECTIONS = {"properties": PROPERTY_FIELDS, "mortgages": MORTGAGE_FIELDS}
EXPENSE_KEYS = tuple(spec["key"] for spec in PROPERTY_FIELDS if spec["key"].startswith("monthly_") and spec["key"] != "monthly_gross_rent")
TOTAL_LABELS = {
    "property_count": "Properties", "active_mortgage_count": "Active mortgages", "estimated_value_total": "Estimated property value",
    "mortgage_balance_total": "Mortgage balances", "monthly_rent_total": "Monthly gross rent", "monthly_operating_expenses_total": "Monthly operating expenses",
    "monthly_mortgage_payments_total": "Monthly mortgage payments", "estimated_equity": "Estimated property equity", "monthly_cash_flow": "Monthly property cash flow",
}


def empty_body():
    return {"schema_version": SCHEMA_VERSION, "owner_name": "", "as_of": None, "no_real_estate": False, "notes": "", "properties": [], "mortgages": []}


def describe():
    return {"kind": KIND, "schema_version": SCHEMA_VERSION, "header": copy.deepcopy(list(HEADER)),
            **{key: copy.deepcopy(list(specs)) for key, specs in COLLECTIONS.items()},
            "totals": [{"key": key, "label": label} for key, label in TOTAL_LABELS.items()]}


def meaningful(row, specs):
    return any(row.get(spec["key"]) not in (None, "", "unknown") for spec in specs)


def properties(body):
    return [row for row in (body or {}).get("properties") or [] if meaningful(row, PROPERTY_FIELDS)]


def mortgages(body):
    return [row for row in (body or {}).get("mortgages") or [] if meaningful(row, MORTGAGE_FIELDS)]


def _amount(value):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("$", "").replace("%", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def validate(raw):
    body = copy.deepcopy(raw)
    body["schema_version"] = SCHEMA_VERSION
    declaration = body.get("no_real_estate", False)
    if declaration in ("true", "false"):
        declaration = declaration == "true"
    if not isinstance(declaration, bool):
        raise HTTPException(422, "The no-real-estate declaration must be a boolean")
    body["no_real_estate"] = declaration
    for block, specs in ((None, HEADER), *COLLECTIONS.items()):
        rows = [body] if block is None else body.setdefault(block, [])
        if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
            raise HTTPException(422, "Invalid real estate schedule rows")
        seen = set()
        for row in rows:
            if block:
                row_id = str(row.get("id") or uuid4())
                try:
                    UUID(row_id)
                except (ValueError, TypeError):
                    raise HTTPException(422, "Real estate schedule rows require UUIDs") from None
                if row_id in seen:
                    raise HTTPException(422, "Real estate schedule rows require distinct UUIDs")
                seen.add(row_id)
                row["id"] = row_id
                status = "mortgage_status" if block == "properties" else "status"
                row[status] = row.get(status) or "unknown"
                if block == "mortgages":
                    row["payment_basis"] = row.get("payment_basis") or "unknown"
            for spec in specs:
                key, kind = spec["key"], spec["input"]
                value = row.get(key)
                if value in (None, "") or key == "no_real_estate":
                    continue
                if isinstance(value, (dict, list, bool)):
                    raise HTTPException(422, f"Invalid value for {key}")
                text = str(value).strip()
                if len(text) > 4000:
                    raise HTTPException(422, f"Value too long for {key}")
                if spec.get("options") and text not in spec["options"]:
                    raise HTTPException(422, f"Choose a valid option for {key}")
                if kind in {"money", "number", "percent", "rate"}:
                    number = _amount(value)
                    if number is None or number < 0 or number >= Decimal("1000000000000"):
                        raise HTTPException(422, f"Enter a valid nonnegative number for {key}")
                    if key == "ownership_pct" and number > 100 or key == "rate" and number > 1000 or key == "units" and number != number.to_integral_value():
                        raise HTTPException(422, f"Enter a valid value for {key}")
                    precision = Decimal("0.001") if kind == "rate" else Decimal("0.01")
                    if number != number.quantize(precision):
                        raise HTTPException(422, f"Use at most {'three' if kind == 'rate' else 'two'} decimal places for {key}")
                    row[key] = str(number.quantize(precision))
                elif kind == "date":
                    try:
                        date.fromisoformat(text)
                    except ValueError:
                        raise HTTPException(422, f"Enter an ISO date for {key}") from None
                    row[key] = text
                else:
                    row[key] = text
    owned = {row["id"]: row for row in body["properties"]}
    for row in body["mortgages"]:
        parent = row.get("property_row_id")
        if parent and parent not in owned:
            raise HTTPException(422, "Choose a property from this real estate schedule for the mortgage")
        if row.get("status") == "paid_off" and (_amount(row.get("balance")) or 0) > 0:
            raise HTTPException(422, "A paid-off mortgage cannot have a positive current balance")
        if parent and owned[parent].get("mortgage_status") == "free_clear" and active_mortgage(row):
            raise HTTPException(422, "A property marked free and clear cannot have an active mortgage or positive mortgage balance")
        payment, escrow = _amount(row.get("monthly_payment")), _amount(row.get("monthly_escrow"))
        if row.get("payment_basis") == "including_escrow" and payment is not None and escrow is not None and escrow > payment:
            raise HTTPException(422, "Monthly escrow cannot exceed the monthly mortgage payment")
    if declaration and (properties(body) or mortgages(body)):
        raise HTTPException(422, "Remove the no-real-estate declaration while properties or mortgages are listed")
    return body


def _sum(rows, key):
    values = [_amount(row.get(key)) for row in rows]
    return sum(values, Decimal(0)) if values and all(value is not None for value in values) else None


def active_mortgage(row):
    return row.get("status") == "active" or row.get("status", "unknown") == "unknown" and (_amount(row.get("balance")) or 0) > 0


def mortgage_requires_statement(property_row, mortgage_row=None):
    if mortgage_row is None:
        return property_row.get("mortgage_status") == "active"
    if mortgage_row.get("status") == "paid_off":
        return False
    return active_mortgage(mortgage_row) or property_row.get("mortgage_status") == "active"


def principal_interest_payment(row):
    payment = _amount(row.get("monthly_payment"))
    if row.get("payment_basis") == "principal_interest":
        return payment
    escrow = _amount(row.get("monthly_escrow"))
    if row.get("payment_basis") == "including_escrow" and payment is not None and escrow is not None and 0 <= escrow <= payment:
        return payment - escrow
    return None


def totals(body):
    body = body or {}
    if body.get("no_real_estate") is True:
        return dict.fromkeys(TOTAL_LABELS, Decimal(0))
    props, liens = properties(body), mortgages(body)
    result = dict.fromkeys(TOTAL_LABELS)
    if not props:
        return result
    result.update(property_count=len(props), estimated_value_total=_sum(props, "estimated_value"), monthly_rent_total=_sum(props, "monthly_gross_rent"))
    operating = [_sum([row], key) for row in props for key in EXPENSE_KEYS]
    result["monthly_operating_expenses_total"] = sum(operating, Decimal(0)) if all(value is not None for value in operating) else None
    active = [row for row in liens if row.get("status") == "active"]
    known = all(prop.get("mortgage_status") in {"active", "free_clear"} and (prop.get("mortgage_status") == "free_clear" or any(row.get("property_row_id") == prop["id"] for row in active)) for prop in props)
    known = known and all(row.get("status") in {"active", "paid_off"} and row.get("property_row_id") for row in liens)
    if known:
        result["active_mortgage_count"] = len(active)
        result["mortgage_balance_total"] = _sum(active, "balance") if active else Decimal(0)
        result["monthly_mortgage_payments_total"] = _sum(active, "monthly_payment") if active else Decimal(0)
    if result["estimated_value_total"] is not None and result["mortgage_balance_total"] is not None:
        result["estimated_equity"] = result["estimated_value_total"] - result["mortgage_balance_total"]
    pi = [principal_interest_payment(row) for row in active]
    monthly_pi = sum(pi, Decimal(0)) if known and all(value is not None for value in pi) else None
    cash = [result["monthly_rent_total"], result["monthly_operating_expenses_total"], monthly_pi]
    if all(value is not None for value in cash):
        result["monthly_cash_flow"] = cash[0] - cash[1] - cash[2]
    return result


def key_facts(body):
    return {"owner_name": body.get("owner_name"), "as_of": body.get("as_of"), "no_real_estate": body.get("no_real_estate") is True,
            "properties": copy.deepcopy(properties(body)), "mortgages": copy.deepcopy(mortgages(body)), **totals(body)}


def align_import(proposed, current, warnings):
    """Retain only unambiguous source identities; never merge same-lender liens."""
    proposed = copy.deepcopy(proposed)
    changed = {}

    def text(value):
        return " ".join(str(value or "").split()).casefold()

    for block in COLLECTIONS:
        incoming, stored = proposed.get(block) or [], (current or {}).get(block) or []
        if block == "mortgages":
            for row in incoming:
                row["property_row_id"] = changed.get(str(row.get("property_row_id")), row.get("property_row_id"))

        def identity(row, block=block):
            if block == "properties":
                return tuple(text(row.get(key)) for key in ("address", "city", "state", "postal_code", "legal_owner")) if text(row.get("address")) else None
            if not row.get("property_row_id") or not text(row.get("lender")):
                return None
            return (str(row["property_row_id"]), text(row["lender"]), text(row.get("loan_reference")))

        source_groups, stored_groups = {}, {}
        known_ids = {str(row.get("id")): row for row in stored if row.get("id")}
        incoming_ids = [str(row.get("id")) for row in incoming]
        for row in incoming:
            source_groups.setdefault(identity(row), []).append(row)
        for row in stored:
            stored_groups.setdefault(identity(row), []).append(row)
        for row in incoming:
            key = identity(row)
            matches = stored_groups.get(key, []) if key else []
            # Fresh extraction IDs are minted by the server. A cached proposal
            # may already carry an established ID even after a staff correction
            # changes its address or lender. Preserve that proven lineage.
            existing = known_ids.get(str(row.get("id"))) if incoming_ids.count(str(row.get("id"))) == 1 else None
            if existing is not None or key and len(matches) == len(source_groups[key]) == 1:
                original = row["id"]
                existing = existing or matches[0]
                # Only controls/evidence survive alignment. An old editable
                # answer omitted by this source must not become a new mapping
                # with this source's citation; apply merges selected fields.
                columns = {spec["key"] for spec in COLLECTIONS[block]}
                preserved = {key: copy.deepcopy(value) for key, value in existing.items() if key not in columns}
                preserved.update({key: copy.deepcopy(value) for key, value in row.items() if key in columns})
                preserved["id"] = existing["id"]
                row.clear()
                row.update(preserved)
                changed[str(original)] = str(row["id"])
            elif matches:
                warnings.append(f"{block}: multiple rows share this identity; review the source rows individually before saving.")
    return proposed, changed
