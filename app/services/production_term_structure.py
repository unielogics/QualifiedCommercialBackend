"""Typed repayment structures stored in ``ProductionTermSheet.extra``.

The production term-sheet table predates revolving and phased repayment
structures.  Its scalar columns remain the compatibility snapshot used by old
clients; this module owns the richer, versioned structure written to ``extra``
and the deterministic calculations derived from it.

Rates in this module are percentages (``10.25`` means 10.25%), matching the
existing production-term-sheet API.  The calculation is an estimate based on
nominal periodic rates; final funding documents control day-count and actual
daily-balance interest.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

STRUCTURE_VERSION = 1

FACILITY_KINDS: tuple[str, ...] = (
    "term_loan",
    "revolving_loc",
    "heloc",
    "hybrid",
    "other",
)
REPAYMENT_STRUCTURES: tuple[str, ...] = (
    "fully_amortizing",
    "interest_only",
    "interest_only_then_amortizing",
    "balloon",
    "revolving_interest_only",
    "fixed_payment",
    "custom",
)
PAYMENT_FREQUENCIES: tuple[str, ...] = (
    "daily",
    "weekly",
    "biweekly",
    "monthly",
    "custom",
)
RATE_STRUCTURES: tuple[str, ...] = ("fixed", "variable", "custom")
FUNDER_TYPES: tuple[str, ...] = (
    "bank",
    "credit_union",
    "private_credit",
    "nonbank_lender",
    "balance_sheet",
    "family_office",
    "sponsor",
    "other",
)

# Preserve the cadence contract already used by application term sheets.  The
# weekly values reflect 365.25-day years and daily means business-day drafts.
CADENCE_PERIODS_PER_YEAR: dict[str, float] = {
    "daily": 252.0,
    "weekly": 51.96,
    "biweekly": 25.98,
    "monthly": 12.0,
}

STRUCTURE_FIELDS: tuple[str, ...] = (
    "structure_version",
    "facility_kind",
    "facility_catalog_key",
    "funder_type",
    "repayment_structure",
    "payment_frequency",
    "payments_per_year",
    "rate_structure",
    "rate_index",
    "rate_index_rate_pct",
    "rate_margin_pct",
    "rate_floor_pct",
    "rate_cap_pct",
    "rate_as_of",
    "apr_pct",
    "initial_draw_amount",
    "payment_basis_amount",
    "draw_period_months",
    "interest_only_months",
    "amortization_months",
    "balloon_amount",
    "periodic_payment",
    "post_io_payment",
    "post_io_monthly_equivalent",
    "monthly_equivalent_payment",
    "monthly_program_coverage_amount",
    "monthly_program_coverage_basis",
    "lender_payment_override",
    "custom_payment_description",
    "custom_payment_frequency",
    "custom_rate_description",
    "first_payment_date",
    "expiration_days",
    "expires_on",
    "closing_estimate_days",
    "payment_count",
    "annual_debt_service",
    "total_repayment",
    "financing_cost",
    "debt_service_treatment",
    "retained_annual_debt_service",
    "dscr_before",
    "dscr_after",
    "dscr_status",
    "dscr_explanation",
    "dscr_source",
)


def is_level_payment(structure: dict[str, Any], *, term_months: int) -> bool:
    """Return whether the compatibility payment is a fixed monthly level payment."""
    return bool(
        structure.get("repayment_structure") == "fully_amortizing"
        and not structure.get("lender_payment_override")
        and int(structure.get("amortization_months") or term_months) == int(term_months)
        and structure.get("payment_frequency") == "monthly"
        and structure.get("rate_structure") == "fixed"
    )


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    return int(parsed) if parsed is not None else None


def _text(value: Any) -> str | None:
    result = str(value or "").strip()
    return result or None


def _iso_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return str(value)[:10]


def _pick(raw: dict[str, Any], key: str, default: Any = None) -> Any:
    value = raw.get(key)
    if value is not None:
        return value
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    return extra.get(key, default)


def infer_facility_kind(facility_type: Any) -> str:
    value = str(facility_type or "").strip().lower()
    if "heloc" in value or "home equity line" in value:
        return "heloc"
    if "hybrid" in value:
        return "hybrid"
    if "revolving" in value or "line of credit" in value or value == "loc":
        return "revolving_loc"
    return "term_loan"


def infer_funder_type(funding_party_kind: Any) -> str:
    value = str(funding_party_kind or "").strip().lower()
    if value == "sponsor":
        return "sponsor"
    if value in {"lender", "qualified commercial llc"}:
        return "nonbank_lender"
    return "other"


def normalize_funder_type(value: Any, funding_party_kind: Any) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_")
    aliases = {
        "private_fund": "private_credit",
        "private_capital": "private_credit",
        "warehouse": "nonbank_lender",
        "table_funder": "nonbank_lender",
        "qualified_commercial": "nonbank_lender",
    }
    return aliases.get(raw, raw) if raw else infer_funder_type(funding_party_kind)


def normalize_frequency(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "bi_weekly": "biweekly",
        "every_two_weeks": "biweekly",
        "business_daily": "daily",
    }.get(raw, raw)


def periods_per_year(frequency: str, custom: Any = None) -> float:
    if frequency == "custom":
        return float(_number(custom) or 0)
    return CADENCE_PERIODS_PER_YEAR.get(frequency, 0.0)


def period_count(months: int, periods: float) -> int:
    """Positive half-up count, identical to the existing browser contract."""
    if months <= 0 or periods <= 0:
        return 0
    return max(1, math.floor(months * periods / 12.0 + 0.5))


def _payment_for_target(balance: float, rate: float, periods: int, target: float = 0.0) -> float:
    if periods <= 0:
        return 0.0
    target = max(0.0, target)
    if rate == 0:
        return max(0.0, balance - target) / periods
    discount = (1 + rate) ** periods
    return max(0.0, (balance - target / discount) * rate / (1 - discount**-1))


def _remaining_balance(balance: float, rate: float, payment: float, periods: int) -> float:
    current = max(0.0, balance)
    for _ in range(max(0, periods)):
        current = max(0.0, current * (1 + rate) - payment)
    return current


def effective_rate_pct(values: dict[str, Any]) -> float:
    stated = max(0.0, _number(values.get("rate_pct")) or 0.0)
    if values.get("rate_structure") != "variable":
        return stated
    index_rate = _number(values.get("rate_index_rate_pct"))
    margin = _number(values.get("rate_margin_pct"))
    result = stated if index_rate is None or margin is None else index_rate + margin
    floor = _number(values.get("rate_floor_pct"))
    cap = _number(values.get("rate_cap_pct"))
    if floor is not None:
        result = max(result, floor)
    if cap is not None:
        result = min(result, cap)
    return max(0.0, result)


def normalize_input(raw: dict[str, Any], *, entered_on: date | None = None) -> dict[str, Any]:
    """Return a complete typed structure from a new payload or a legacy row."""
    facility_kind = _text(_pick(raw, "facility_kind")) or infer_facility_kind(raw.get("facility_type"))
    frequency = normalize_frequency(
        _pick(raw, "payment_frequency") or _pick(raw, "repayment_frequency") or "monthly"
    )
    ppy = periods_per_year(frequency, _pick(raw, "payments_per_year"))

    explicit_structure = _text(_pick(raw, "repayment_structure"))
    if explicit_structure:
        repayment = explicit_structure
    elif bool(raw.get("debt_service_is_level_payment")) or raw.get("monthly_debt_service") in (None, "", 0, 0.0):
        repayment = "fully_amortizing"
    else:
        # A historical hand-entered payment cannot safely be reclassified as IO.
        repayment = "fixed_payment"

    approved = _number(raw.get("approved_amount")) or 0.0
    initial_draw = _number(_pick(raw, "initial_draw_amount"))
    basis = _number(_pick(raw, "payment_basis_amount"))
    if basis is None:
        basis = initial_draw if initial_draw is not None else approved
    if initial_draw is None and facility_kind not in {"revolving_loc", "heloc", "hybrid"}:
        initial_draw = approved

    term_months = max(0, _integer(raw.get("term_months")) or 0)
    io_months = max(0, _integer(_pick(raw, "interest_only_months")) or 0)
    amortization_months = _integer(_pick(raw, "amortization_months"))
    # Treat repayment_structure as the discriminator. Values left behind after
    # switching the drawer from one structure to another must not change the
    # economics of the newly selected structure.
    if repayment == "fully_amortizing":
        io_months = 0
        amortization_months = term_months
    elif repayment in {"interest_only", "revolving_interest_only"}:
        io_months = term_months
        amortization_months = None
    elif repayment == "interest_only_then_amortizing":
        amortization_months = amortization_months or max(0, term_months - io_months)
    else:
        io_months = 0
        if repayment in {"fixed_payment", "custom"}:
            amortization_months = None

    explicit_balloon = _number(_pick(raw, "balloon_amount"))
    if repayment in {"fully_amortizing", "interest_only_then_amortizing", "interest_only", "revolving_interest_only"}:
        explicit_balloon = None

    rate_structure = _text(_pick(raw, "rate_structure")) or "fixed"
    debt_service_treatment = _text(_pick(raw, "debt_service_treatment")) or "additive"
    retained_annual_debt_service = _number(_pick(raw, "retained_annual_debt_service"))
    if debt_service_treatment != "refinance":
        retained_annual_debt_service = None
    values: dict[str, Any] = {
        "structure_version": STRUCTURE_VERSION,
        "facility_kind": facility_kind,
        "facility_catalog_key": _text(_pick(raw, "facility_catalog_key")),
        "funder_type": normalize_funder_type(_pick(raw, "funder_type"), raw.get("funding_party_kind")),
        "repayment_structure": repayment,
        "payment_frequency": frequency,
        "payments_per_year": ppy,
        "rate_structure": rate_structure,
        "rate_index": _text(_pick(raw, "rate_index")),
        "rate_index_rate_pct": _number(_pick(raw, "rate_index_rate_pct")),
        "rate_margin_pct": _number(_pick(raw, "rate_margin_pct")),
        "rate_floor_pct": _number(_pick(raw, "rate_floor_pct")),
        "rate_cap_pct": _number(_pick(raw, "rate_cap_pct")),
        "rate_as_of": _iso_date(_pick(raw, "rate_as_of")),
        "apr_pct": _number(_pick(raw, "apr_pct")),
        "initial_draw_amount": initial_draw,
        "payment_basis_amount": basis,
        "draw_period_months": _integer(_pick(raw, "draw_period_months")),
        "interest_only_months": io_months,
        "amortization_months": amortization_months,
        "balloon_amount": explicit_balloon,
        "periodic_payment": _number(_pick(raw, "periodic_payment")),
        "post_io_payment": _number(_pick(raw, "post_io_payment")),
        "monthly_equivalent_payment": _number(_pick(raw, "monthly_equivalent_payment")),
        "monthly_program_coverage_amount": _number(_pick(raw, "monthly_program_coverage_amount")),
        "monthly_program_coverage_basis": _text(_pick(raw, "monthly_program_coverage_basis")),
        "lender_payment_override": bool(_pick(raw, "lender_payment_override", False)),
        "custom_payment_description": _text(_pick(raw, "custom_payment_description")),
        "custom_payment_frequency": _text(_pick(raw, "custom_payment_frequency")),
        "custom_rate_description": _text(_pick(raw, "custom_rate_description")),
        # This is a frontend display snapshot. Preserve its JSON shape in
        # ``extra`` but never use it as the source of truth for calculations.
        "payment_summary": _pick(raw, "payment_summary"),
        "first_payment_date": _iso_date(_pick(raw, "first_payment_date")),
        "expiration_days": _integer(_pick(raw, "expiration_days")),
        "expires_on": _iso_date(_pick(raw, "expires_on")),
        "closing_estimate_days": _integer(_pick(raw, "closing_estimate_days")),
        "debt_service_treatment": debt_service_treatment,
        "retained_annual_debt_service": retained_annual_debt_service,
        "approved_amount": approved,
        "term_months": term_months,
        "rate_pct": _number(raw.get("rate_pct")) or 0.0,
        "monthly_debt_service": _number(raw.get("monthly_debt_service")),
    }
    values["effective_rate_pct"] = effective_rate_pct(values)

    return values


def calculate(raw: dict[str, Any], *, entered_on: date | None = None) -> dict[str, Any]:
    """Calculate payment snapshots, debt service, balloon, and total repayment."""
    values = normalize_input(raw, entered_on=entered_on)
    structure = values["repayment_structure"]
    ppy = float(values["payments_per_year"] or 0)
    term_count = period_count(int(values["term_months"] or 0), ppy)
    io_count = min(term_count, period_count(int(values["interest_only_months"] or 0), ppy))
    basis = max(0.0, float(values["payment_basis_amount"] or 0))
    periodic_rate = (float(values["effective_rate_pct"] or 0) / 100.0) / ppy if ppy > 0 else 0.0
    interest_payment = basis * periodic_rate
    manual = values["periodic_payment"]
    if manual is None and values["monthly_debt_service"] is not None and frequency_is_legacy_manual(values):
        manual = float(values["monthly_debt_service"]) * 12.0 / ppy if ppy else None

    payments: list[float] = []
    post_io: float | None = None
    balloon = 0.0

    if structure in {"interest_only", "revolving_interest_only"}:
        periodic = float(manual) if values["lender_payment_override"] and manual is not None else interest_payment
        payments = [periodic] * term_count
        balloon = basis
    elif structure == "custom":
        periodic = float(manual or 0.0)
        payments = [periodic] * term_count
        balloon = max(0.0, float(values["balloon_amount"] or 0.0))
    else:
        remaining_count = max(0, term_count - io_count)
        explicit_balloon = values["balloon_amount"]
        amort_months = values["amortization_months"]
        amort_count = period_count(int(amort_months or 0), ppy) if amort_months else remaining_count
        if structure == "balloon" and explicit_balloon is not None:
            regular = _payment_for_target(basis, periodic_rate, remaining_count, float(explicit_balloon))
        else:
            regular = _payment_for_target(basis, periodic_rate, max(1, amort_count), 0.0)
        if structure == "fixed_payment" or (
            values["lender_payment_override"] and structure != "interest_only_then_amortizing"
        ):
            regular = float(manual or 0.0)
        if structure == "interest_only_then_amortizing" or io_count:
            initial_payment = (
                float(manual or 0.0)
                if structure == "interest_only_then_amortizing" and values["lender_payment_override"]
                else interest_payment
            )
            payments.extend([initial_payment] * io_count)
            post_io = regular if remaining_count else None
        payments.extend([regular] * remaining_count)
        periodic = payments[0] if payments else regular
        computed_balloon = _remaining_balance(basis, periodic_rate, regular, remaining_count)
        if structure == "balloon" and explicit_balloon is not None:
            balloon = max(0.0, float(explicit_balloon))
        else:
            balloon = computed_balloon
        if balloon < 0.005:
            balloon = 0.0

    # An explicit lender balloon controls the disclosed maturity amount for
    # every structure, including IO and custom schedules.
    if values["balloon_amount"] is not None:
        balloon = max(0.0, float(values["balloon_amount"]))

    horizon = min(float(term_count), ppy)
    whole = int(math.floor(horizon))
    fraction = horizon - whole
    regular_annual = sum(payments[:whole])
    if fraction and whole < len(payments):
        regular_annual += payments[whole] * fraction
    # DSCR measures recurring scheduled debt service. A maturity balloon is
    # disclosed separately and included in total repayment, but is not loaded
    # into a single year's coverage denominator.
    annual = regular_annual
    total_regular = sum(payments)
    total = total_regular + balloon
    # Normalize one periodic payment into a monthly snapshot. Annual DSCR is a
    # different concept: it follows the actual first-year schedule above and
    # may therefore include a post-IO step-up.
    monthly_equivalent = float(periodic or 0.0) * ppy / 12.0
    post_io_monthly_equivalent = float(post_io or 0.0) * ppy / 12.0 if post_io is not None else None

    coverage = values["monthly_program_coverage_amount"]
    if coverage is None:
        coverage = max(monthly_equivalent, post_io_monthly_equivalent or 0.0)
    coverage_basis = values["monthly_program_coverage_basis"] or "calculated scheduled payment"

    values.update(
        {
            "periodic_payment": round(float(periodic or 0.0), 2),
            "post_io_payment": round(float(post_io), 2) if post_io is not None else None,
            "post_io_monthly_equivalent": (
                round(post_io_monthly_equivalent, 2) if post_io_monthly_equivalent is not None else None
            ),
            "monthly_equivalent_payment": round(monthly_equivalent, 2),
            "monthly_program_coverage_amount": round(float(coverage or 0.0), 2),
            "monthly_program_coverage_basis": coverage_basis,
            "balloon_amount": round(balloon, 2),
            "payment_count": term_count,
            "annual_debt_service": round(annual, 2),
            "total_repayment": round(total, 2),
            "financing_cost": round(max(0.0, total - basis), 2),
        }
    )
    return values


def frequency_is_legacy_manual(values: dict[str, Any]) -> bool:
    return values.get("repayment_structure") in {"fixed_payment", "custom"} and values.get("payment_frequency") == "monthly"


def validation_errors(values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    facility = values.get("facility_kind")
    structure = values.get("repayment_structure")
    frequency = values.get("payment_frequency")
    rate_structure = values.get("rate_structure")
    funder_type = values.get("funder_type")
    approved = float(values.get("approved_amount") or 0)
    basis = float(values.get("payment_basis_amount") or 0)
    initial_draw = values.get("initial_draw_amount")
    term = int(values.get("term_months") or 0)
    io_months = int(values.get("interest_only_months") or 0)
    draw_months = values.get("draw_period_months")

    if facility not in FACILITY_KINDS:
        errors.append("Choose a supported facility kind.")
    if structure not in REPAYMENT_STRUCTURES:
        errors.append("Choose a supported repayment structure.")
    if frequency not in PAYMENT_FREQUENCIES or float(values.get("payments_per_year") or 0) <= 0:
        errors.append("Choose a valid payment frequency; custom cadence requires payments per year.")
    if rate_structure not in RATE_STRUCTURES:
        errors.append("Choose a supported rate structure.")
    apr = values.get("apr_pct")
    if apr is not None and not 0 <= float(apr) <= 100:
        errors.append("Lender-disclosed APR must be between 0% and 100%.")
    if funder_type not in FUNDER_TYPES:
        errors.append("Choose a supported funder type.")
    if rate_structure == "variable":
        if not values.get("rate_index"):
            errors.append("Variable-rate terms require a rate index.")
        if values.get("rate_index_rate_pct") is None or values.get("rate_margin_pct") is None:
            errors.append("Variable-rate terms require the current index rate and margin.")
        if not values.get("rate_as_of"):
            errors.append("Variable-rate terms require a rate-as-of date.")
    floor = values.get("rate_floor_pct")
    cap = values.get("rate_cap_pct")
    if floor is not None and cap is not None and float(floor) > float(cap):
        errors.append("The rate floor cannot exceed the rate cap.")
    if basis < 0 or basis > approved:
        errors.append("The payment basis must be between zero and the approved amount.")
    if initial_draw is not None and (float(initial_draw) < 0 or float(initial_draw) > approved):
        errors.append("The initial draw must be between zero and the approved amount.")
    if io_months < 0 or io_months > term:
        errors.append("The interest-only period cannot exceed the facility term.")
    if draw_months is not None and (int(draw_months) < 0 or int(draw_months) > term):
        errors.append("The draw period cannot exceed the facility term.")
    if structure == "interest_only_then_amortizing" and not (0 < io_months < term):
        errors.append("IO-then-amortizing terms require an interest-only period shorter than the term.")
    if structure == "interest_only_then_amortizing":
        remaining = max(0, term - io_months)
        amort = values.get("amortization_months")
        if amort is None or int(amort) < remaining:
            errors.append(
                "The amortization period cannot be shorter than the remaining term after the interest-only phase."
            )
    if structure == "balloon":
        amort = values.get("amortization_months")
        explicit = values.get("balloon_amount")
        remaining = max(0, term - io_months)
        if explicit is None and (amort is None or int(amort) <= remaining):
            errors.append("Balloon terms require a balloon amount or amortization longer than the remaining term.")
    if structure in {"fixed_payment", "custom"} and float(values.get("periodic_payment") or 0) <= 0:
        errors.append("Fixed or custom terms require a positive periodic payment.")
    if values.get("lender_payment_override") and float(values.get("periodic_payment") or 0) <= 0:
        errors.append("A lender payment override requires a positive periodic payment.")
    if structure == "custom" and not values.get("custom_payment_description"):
        errors.append("Custom terms require a payment description.")
    if structure == "revolving_interest_only" and facility not in {"revolving_loc", "heloc", "hybrid"}:
        errors.append("Revolving interest-only repayment requires a revolving, HELOC, or hybrid facility.")
    treatment = values.get("debt_service_treatment")
    retained = values.get("retained_annual_debt_service")
    if treatment not in {"additive", "refinance"}:
        errors.append("Choose additive or refinance debt-service treatment.")
    elif treatment == "refinance" and retained is None:
        errors.append(
            "Refinance terms require the annual debt service that will remain after payoff (enter 0 for a full payoff)."
        )
    if retained is not None and float(retained) < 0:
        errors.append("Retained annual debt service cannot be negative.")
    expiration = values.get("expiration_days")
    if expiration is not None and not 1 <= int(expiration) <= 180:
        errors.append("Expiration must be between 1 and 180 days.")
    closing = values.get("closing_estimate_days")
    if closing is not None and not 0 <= int(closing) <= 180:
        errors.append("Estimated closing must be between 0 and 180 business days.")
    if float(values.get("monthly_equivalent_payment") or 0) <= 0:
        errors.append("Enter a positive payment basis, rate, or lender payment; this database cannot store a zero-payment term sheet.")
    if float(values.get("monthly_program_coverage_amount") or 0) <= 0:
        errors.append("Monthly program coverage must be above zero.")
    return errors


def stored_extra(existing: dict[str, Any] | None, values: dict[str, Any]) -> dict[str, Any]:
    """Merge calculated canonical keys without dropping unrelated legacy extras."""
    out = dict(existing or {})
    for key in STRUCTURE_FIELDS:
        if key in values:
            out[key] = values[key]
    return out


def public_values(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a stored row/dict for API, PDF, and delivery summaries."""
    calculated = calculate(raw)
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    for key in (
        "dscr_before",
        "dscr_after",
        "dscr_status",
        "dscr_explanation",
        "dscr_source",
        "debt_service_treatment",
        "retained_annual_debt_service",
    ):
        if key in extra:
            calculated[key] = extra[key]
    return calculated


def structure_label(value: str) -> str:
    return {
        "fully_amortizing": "Fully amortizing",
        "interest_only": "Interest only",
        "interest_only_then_amortizing": "Interest only, then amortizing",
        "balloon": "Amortizing with balloon",
        "revolving_interest_only": "Revolving interest only",
        "fixed_payment": "Fixed lender payment",
        "custom": "Custom payment structure",
    }.get(value, value.replace("_", " ").title())


def cadence_label(values: dict[str, Any]) -> str:
    frequency = str(values.get("payment_frequency") or "monthly")
    if frequency == "custom":
        return str(values.get("custom_payment_frequency") or "Custom cadence")
    return {
        "daily": "Daily (business days)",
        "weekly": "Weekly",
        "biweekly": "Every two weeks",
        "monthly": "Monthly",
    }.get(frequency, frequency.replace("_", " ").title())


def rate_label(values: dict[str, Any]) -> str:
    rate = float(values.get("effective_rate_pct") or values.get("rate_pct") or 0)
    if values.get("rate_structure") == "custom":
        return str(values.get("custom_rate_description") or f"{rate:.2f}% custom")
    if values.get("rate_structure") != "variable":
        return f"{rate:.2f}% fixed" if values.get("rate_structure") == "fixed" else f"{rate:.2f}%"
    index = values.get("rate_index") or "Index"
    if str(index).strip().lower() == "other" and values.get("custom_rate_description"):
        index = values["custom_rate_description"]
    index_rate = values.get("rate_index_rate_pct")
    margin = values.get("rate_margin_pct")
    detail = f"{rate:.2f}% current ({index}"
    if index_rate is not None:
        detail += f" {float(index_rate):.2f}%"
    if margin is not None:
        numeric_margin = float(margin)
        sign = "+" if numeric_margin >= 0 else "-"
        detail += f" {sign} {abs(numeric_margin):.2f}%"
    detail += ")"
    if values.get("rate_as_of"):
        detail += f" as of {values['rate_as_of']}"
    floor = values.get("rate_floor_pct")
    cap = values.get("rate_cap_pct")
    if floor is not None:
        detail += f"; {float(floor):.2f}% floor"
    if cap is not None:
        detail += f"; {float(cap):.2f}% cap"
    return detail


def payment_summary_text(values: dict[str, Any]) -> str:
    """Generate a client-safe description from canonical server calculations."""
    structure = str(values.get("repayment_structure") or "fully_amortizing")
    cadence = cadence_label(values).lower()
    periodic = float(values.get("periodic_payment") or 0)
    post_io = values.get("post_io_payment")
    io_months = int(values.get("interest_only_months") or 0)
    balloon = float(values.get("balloon_amount") or 0)
    def money(amount: Any) -> str:
        return f"${float(amount):,.2f}"

    if structure == "custom" and values.get("custom_payment_description"):
        description = str(values["custom_payment_description"])
    elif structure == "interest_only_then_amortizing" and post_io is not None:
        description = (
            f"{money(periodic)} {cadence} for the first {io_months} months, then "
            f"{money(post_io)} {cadence}."
        )
    elif structure in {"interest_only", "revolving_interest_only"}:
        description = f"{money(periodic)} {cadence} interest-only estimate."
    elif structure == "balloon":
        description = f"{money(periodic)} {cadence} on the stated amortization."
    elif structure == "fixed_payment":
        description = f"{money(periodic)} {cadence} lender-stated payment."
    else:
        description = f"{money(periodic)} {cadence} principal-and-interest payment."
    if balloon > 0:
        description += f" {money(balloon)} is due at maturity."
    return description
