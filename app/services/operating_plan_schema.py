"""Presentation adapter for the versioned operating plan's domain schema."""
from __future__ import annotations

from app.schemas import earnings_improvement as domain

KIND = "operating_plan"
LABEL = "Operating expenses and earnings plan"
REQUIREMENT_KEY = "operating_expenses_and_earnings_plan"
SCHEMA_VERSION = domain.SCHEMA_VERSION
empty_body = domain.empty_body
validate = domain.validate_body
key_facts = domain.key_facts
completion = domain.completion

CADENCES = ("weekly", "biweekly", "semi_monthly", "monthly", "quarterly", "annual", "one_time")
CATEGORIES = ("rent", "salaries", "payroll_taxes", "insurance", "utilities", "marketing", "software", "professional_fees", "supplies", "repairs", "transportation", "cogs", "merchant_processing", "loan_payments", "income_taxes", "owner_draws", "transfers", "depreciation_amortization", "other")
ACTION_KINDS = ("price_change", "product_cost_change", "processing_cost_change", "revenue_change", "marketing_investment", "expense_reduction", "expense_increase", "one_time_adjustment")
STAFF_FIELDS = frozenset(domain.REVIEW_FIELDS)
CLIENT_HEADER_FIELDS = ("business_name", "period_start", "period_end", "currency", "basis", "projection_start", "no_operating_expenses_confirmed", "client_attestation", "notes", "released_cash_flow", "category_confirmations")
CLIENT_EXPENSE_FIELDS = tuple(key for key in domain.EXPENSE_FIELDS if key not in STAFF_FIELDS and key != "id")
CLIENT_ADJUSTMENT_FIELDS = tuple(key for key in domain.ADJUSTMENT_FIELDS if key not in STAFF_FIELDS and key != "id")


def field(key, label=None, input="text", options=None):
    return {"key": key, "label": label or key.replace("_", " ").capitalize(), "input": input, "type": input, **({"options": list(options)} if options else {})}


HEADER = (
    field("business_name", "Business name"), field("period_start", "Reporting period start", "date"),
    field("period_end", "Reporting period end", "date"), field("currency", "Currency"),
    field("projection_start", "Projection start", "date"),
    field("basis", "Accounting basis", "select", ("cash", "accrual", "unknown")),
    field("no_operating_expenses_confirmed", "No operating expenses to disclose", "boolean"),
    field("client_attestation", "I confirm these disclosures and proposals", "boolean"), field("notes", "Plan notes"),
)


def _spec(key):
    options = {"category": CATEGORIES, "cadence": CADENCES, "kind": ACTION_KINDS,
               "cost_basis": ("total", "per_unit"),
               "implementation_cost_cadence": ("one_time", "monthly", "annual"),
               "current_processing_rate_basis": ("all_in", "processor_markup", "unknown"),
               "proposed_processing_rate_basis": ("all_in", "processor_markup", "unknown")}.get(key)
    kind = "select" if options else "boolean" if key in {"essential", "excluded"} else "date" if key == "effective_date" else "percent" if key.endswith("_pct") else "number" if key in {"units_per_period", "current_units_per_period", "proposed_units_per_period", "processing_volume", "processing_transaction_count"} else "money" if key.endswith(("_amount", "_price", "_cost", "_fee")) or key == "implementation_cost" else "text"
    return field(key, input=kind, options=options)


# Evidence arrays, relationship IDs and staff decisions use scoped controls in
# the editor. They are never free-text worksheet cells.
_NO_GRID_FIELDS = STAFF_FIELDS | {"id", "source_file_ids", "expense_row_id", "duplicate_of", "bank_vendor_key", "source_group_key", "source_candidate_key"}
COLLECTIONS = {
    "expenses": tuple(_spec(key) for key in CLIENT_EXPENSE_FIELDS if key not in _NO_GRID_FIELDS),
    "adjustments": tuple(_spec(key) for key in CLIENT_ADJUSTMENT_FIELDS if key not in _NO_GRID_FIELDS),
}


def describe():
    return {"kind": KIND, "label": LABEL, "schema_version": SCHEMA_VERSION,
            "header": list(HEADER), **{key: list(fields) for key, fields in COLLECTIONS.items()},
            "client_fields": {"expenses": list(CLIENT_EXPENSE_FIELDS), "adjustments": list(CLIENT_ADJUSTMENT_FIELDS)},
            "proposal_only": True}


def totals(body):
    # Worksheet totals disclose counts only, never a client's hidden staff
    # review fields or a parallel source of projected earnings.
    return {"disclosed_expense_rows": sum(bool(row.get("label")) for row in (body or {}).get("expenses") or []),
            "adjustment_rows": sum(bool(row.get("label")) for row in (body or {}).get("adjustments") or [])}
