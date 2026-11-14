"""One reading of a file's earnings, shared by every figure that follows.

A filed business return leads and a dated P&L is its labelled fallback. The
ladder is anchored on the source's own bottom line, so it always adds up:

    revenue - cost of goods sold - operating expenses       = NOI
    NOI + other income + depreciation and amortization      = EBITDA

Interest is financing, never an operating expense. Depreciation and
amortization stay inside NOI where the business expensed them. An add-back
the reader did not find is named and counted as zero: the figure is then
conservative instead of blank. Nothing here selects a source; the earnings
source selection (financial_earnings_sources) stays the single authority and
this module explains the figures that selection produced.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from app.services.financial_tax_earnings import _number as signed_number

log = logging.getLogger(__name__)
POLICY_VERSION = "financial-baseline-v1"
_FORMS = {"1120S": "1120-S"}
TARGET_RATIOS = (1.0, 1.1, 1.25)
TOLERANCE = 1.0

# Named facts, not fixed IRS line numbers: the same expense appears on
# different lines of Forms 1120, 1120-S and 1065 and their statements.
LINES = (
    ("rent", "rent", "Rent and premises", ("rent", "rents", "rent_expense", "rent_and_lease", "rent_buildings")),
    ("salaries_and_wages", "salaries", "Employee salaries and wages", ("salaries_and_wages", "wages", "salaries", "payroll_expense")),
    ("officer_compensation", "salaries", "Officer compensation", ("officer_compensation", "compensation_of_officers", "officers_compensation")),
    ("contract_labor", "professional_fees", "Contract labor", ("contract_labor", "contractor_expense", "subcontractors")),
    ("payroll_taxes", "payroll_taxes", "Employer payroll taxes", ("payroll_taxes", "employer_payroll_taxes")),
    ("insurance", "insurance", "Business insurance", ("insurance", "insurance_expense", "business_insurance")),
    ("utilities", "utilities", "Utilities", ("utilities", "utility_expense")),
    ("advertising", "marketing", "Marketing and advertising", ("advertising", "marketing", "marketing_expense", "advertising_expense")),
    ("software", "software", "Software and subscriptions", ("software", "software_subscriptions", "subscriptions")),
    ("professional_fees", "professional_fees", "Legal and professional fees", ("professional_fees", "legal_and_professional", "legal_and_professional_fees", "legal_fees")),
    ("supplies", "supplies", "Supplies", ("supplies", "office_supplies", "supplies_expense")),
    ("repairs", "repairs", "Repairs and maintenance", ("repairs", "repairs_and_maintenance", "repair_expense")),
    ("transportation", "transportation", "Travel and transportation", ("transportation", "travel", "car_and_truck_expenses")),
    ("cogs", "cogs", "Cost of goods sold", ("cost_of_goods_sold", "cogs")),
    ("taxes_and_licenses", "other", "Taxes and licenses (classify before use)", ("taxes_and_licenses",)),
    ("depreciation_amortization", "depreciation_amortization", "Depreciation and amortization", ("depreciation_and_amortization", "depreciation", "depreciation_expense")),
)
REMAINDER_KEY = "other_deductions"
_SECTIONS = ("expenses", "expense_breakdown", "deductions", "deductions_detail", "other_deductions_detail", "income_statement", "profit_and_loss")
_LIST_SECTIONS = ("expense_lines", "expense_breakdown", "other_deductions_detail", "deductions_detail")
_REVENUE = ("gross_receipts", "gross_receipts_current_year", "gross_receipts_or_sales", "gross_sales", "total_revenue", "gross_revenue", "revenue")
_TOTAL_INCOME = ("total_income", "total_income_current", "total_income_line6")
_OTHER_INCOME = ("other_income", "other_income_loss", "total_other_income", "non_operating_income")
_TOTAL_DEDUCTIONS = ("total_deductions", "total_expenses")


def amount(value: Any) -> float | None:
    """A printed expense amount: finite and not negative, else unknown."""
    if isinstance(value, dict):
        value = value.get("value", value.get("amount"))
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("$", ""))
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def fact(facts: dict[str, Any], aliases: tuple[str, ...]) -> tuple[float | None, str | None]:
    """The first named expense fact, with the path it was read from."""
    containers = [("", facts)]
    for section in _SECTIONS:
        value = facts.get(section)
        if isinstance(value, dict):
            containers.append((section + ".", value))
    for prefix, values in containers:
        for key in aliases:
            value = amount(values.get(key))
            if value is not None:
                return value, prefix + key
    normalized = {re.sub(r"[^a-z0-9]", "", alias.casefold()) for alias in aliases}
    for section in _LIST_SECTIONS:
        rows = facts.get(section)
        if not isinstance(rows, list):
            continue
        matched = [row for row in rows if isinstance(row, dict) and any(
            re.sub(r"[^a-z0-9]", "", str(row.get(key) or "").casefold()) in normalized
            for key in ("key", "category", "label", "description"))]
        if matched and all(amount(row.get("amount", row.get("value"))) is not None for row in matched):
            return sum(amount(row.get("amount", row.get("value"))) for row in matched), section
    return None, None


def _signed(facts: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value: Any = facts
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        number = signed_number(value)
        if number is not None:
            return number
    return None


def _money(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def other_income(facts: dict[str, Any], *, revenue, cogs, deductions, income) -> tuple[float | None, str | None]:
    """Income the return adds above its deductions; it is not operating income.

    Total income less gross profit is used only when the return's own identity
    (total income - total deductions = bottom line) holds on the facts read,
    so one misread line cannot move operating income.
    """
    total_income = _signed(facts, _TOTAL_INCOME)
    if total_income is not None and revenue is not None and deductions is not None and income is not None:
        gross_profit = _signed(facts, ("gross_profit", "gross_profit_current"))
        gross_profit = gross_profit if gross_profit is not None else revenue - (cogs or 0.0)
        if abs(total_income - deductions - income) <= TOLERANCE + 1:
            derived = round(total_income - gross_profit, 2)
            return (derived if abs(derived) >= 0.5 else 0.0), "total income less gross profit"
    printed = _signed(facts, _OTHER_INCOME)
    if printed is not None:
        return printed, "other income as printed"
    return None, None


def return_ladder(facts: dict[str, Any], *, income, interest, depreciation, revenue=None) -> dict[str, Any]:
    """The ladder for one business return.

    `income` is the return's bottom line (ordinary business income, else the
    basis the tax history selected); `interest` and `depreciation` are the
    add-backs the reader found, already carrying any staff correction.
    """
    revenue = revenue if revenue is not None else _signed(facts, _REVENUE)
    cogs, _ = fact(facts, ("cost_of_goods_sold", "cogs"))
    deductions, _ = fact(facts, _TOTAL_DEDUCTIONS)
    other, other_basis = other_income(facts, revenue=revenue, cogs=cogs, deductions=deductions, income=income)
    noi = _money(income - (other or 0.0) + (interest or 0.0)) if income is not None else None
    ebitda = _money(noi + (other or 0.0) + (depreciation or 0.0)) if noi is not None else None
    operating = _money(revenue - (cogs or 0.0) - noi) if revenue is not None and noi is not None else None
    printed_operating = _money(deductions - (interest or 0.0)) if deductions is not None else None
    difference = _money(operating - printed_operating) if operating is not None and printed_operating is not None else None
    return {
        "revenue": _money(revenue), "cogs": _money(cogs), "other_income": _money(other), "other_income_basis": other_basis,
        "total_deductions": _money(deductions), "interest": _money(interest), "depreciation_amortization": _money(depreciation),
        "income": _money(income), "operating_expenses": operating, "noi": noi, "ebitda": ebitda,
        "printed_operating_expenses": printed_operating,
        # Operating expenses are derived from the bottom line. A difference
        # from the printed deductions means one line was read wrong.
        "reconciliation_difference": difference if difference is not None and abs(difference) > TOLERANCE else None,
        "unread": [name for name, value in (("interest", interest), ("depreciation_amortization", depreciation)) if value is None],
    }


def return_noi(facts: dict[str, Any], *, income, interest) -> float | None:
    """Operating income from a return: bottom line less other income plus interest."""
    return return_ladder(facts, income=income, interest=interest, depreciation=None)["noi"]


def named_lines(facts: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, category, label, aliases in LINES:
        value, path = fact(facts, aliases)
        if value is not None:
            rows.append({"key": key, "category": category, "label": label, "amount": value, "fact_path": path})
    return rows


def reconcile_lines(lines: list[dict[str, Any]], *, total: float | None, interest: float | None = None) -> dict[str, Any]:
    """Named lines against the source's own total, with the remainder named.

    `total` is total deductions on a return or total operating expenses on a
    P&L; cost of goods sold sits above both and is not part of either.
    """
    listed = round(sum(row["amount"] for row in lines if row["key"] != "cogs"), 2)
    if total is None:
        return {"status": "total_not_read", "listed": listed, "total": None, "remainder": None, "reconciles": None}
    remainder = round(total - listed - (interest or 0.0), 2)
    if remainder < -TOLERANCE:
        return {"status": "lines_exceed_total", "listed": listed, "total": _money(total), "remainder": remainder, "reconciles": False}
    return {"status": "reconciled", "listed": listed, "total": _money(total),
            "remainder": remainder if remainder > TOLERANCE else 0.0, "reconciles": True}


def _annual_factor(months: Any) -> float | None:
    months = signed_number(months)
    if months is None:
        return None
    return 1.0 if months == 12 else 12 / months if 6 <= months < 12 else None


def _statement_ladder(statement: dict[str, Any], noi_headline: dict[str, Any], ebitda_headline: dict[str, Any]) -> dict[str, Any]:
    """A P&L's own lines, annualized the way its earnings already are."""
    factor = _annual_factor(statement.get("months_covered"))

    def scale(value):
        value = signed_number(value)
        return _money(value * factor) if value is not None and factor is not None else None

    revenue, cogs = scale(statement.get("gross_revenue")), scale(statement.get("cost_of_goods_sold"))
    noi = signed_number(noi_headline.get("annualized_value"))
    ebitda = signed_number(ebitda_headline.get("annualized_value"))
    depreciation = scale(statement.get("depreciation_and_amortization"))
    operating = _money(revenue - (cogs or 0.0) - noi) if revenue is not None and noi is not None else None
    other = _money(ebitda - noi - (depreciation or 0.0)) if ebitda is not None and noi is not None else None
    printed = scale(statement.get("total_operating_expenses"))
    difference = _money(operating - printed) if operating is not None and printed is not None else None
    return {
        "revenue": revenue, "cogs": cogs, "other_income": other, "other_income_basis": "earnings less operating income and D&A" if other is not None else None,
        "total_deductions": printed, "interest": scale(statement.get("interest")), "depreciation_amortization": depreciation,
        "income": scale(statement.get("net_income")), "operating_expenses": operating, "noi": _money(noi), "ebitda": _money(ebitda),
        "printed_operating_expenses": printed,
        "reconciliation_difference": difference if difference is not None and abs(difference) > TOLERANCE else None,
        "unread": [name for name, key in (("interest", "interest"), ("depreciation_amortization", "depreciation_and_amortization")) if statement.get(key) is None],
    }


def _rows(ladder: dict[str, Any], *, kind: str) -> list[dict[str, Any]]:
    """Display order, signed so the column adds to each subtotal."""
    def row(key, label, value, *, sign=1, subtotal=False, note=None):
        return {"key": key, "label": label, "amount": _money(sign * value) if value is not None else None,
                "subtotal": subtotal, **({"note": note} if note else {})}

    unread = set(ladder["unread"])
    tax = kind == "tax_return"
    return [
        row("revenue", "Gross receipts" if tax else "Revenue", ladder["revenue"]),
        row("cogs", "Cost of goods sold", ladder["cogs"], sign=-1, note=None if ladder["cogs"] is not None else "Not printed on the source"),
        row("operating_expenses", "Operating expenses (deductions less interest)" if tax else "Operating expenses", ladder["operating_expenses"], sign=-1),
        row("noi", "NOI", ladder["noi"], subtotal=True),
        row("other_income", "Other income", ladder["other_income"], note=None if ladder["other_income"] is not None else "None read"),
        row("depreciation_amortization", "Depreciation and amortization", ladder["depreciation_amortization"],
            note="Not read from the source; none added back" if "depreciation_amortization" in unread else None),
        row("ebitda", "EBITDA", ladder["ebitda"], subtotal=True),
    ]


def _debt_service(cockpit: dict[str, Any]) -> dict[str, Any]:
    service = (cockpit.get("debt") or {}).get("service") or {}
    evaluation = (cockpit.get("plan") or {}).get("evaluation") or {}
    terms = cockpit.get("proposed_terms") or {}
    obligations = [row for row in (cockpit.get("debt") or {}).get("obligations") or [] if row.get("in_debt_service")]
    current_annual, current_monthly = signed_number(service.get("annual")), signed_number(service.get("monthly"))
    planned = bool(evaluation.get("active") and not evaluation.get("coverage_unavailable_reason"))
    after_annual = signed_number(evaluation.get("debt_service_after_annual")) if planned else None
    after_monthly = signed_number(evaluation.get("debt_service_after_regular_monthly")
                                  if "debt_service_after_regular_monthly" in evaluation else evaluation.get("debt_service_after_monthly")) if planned else None
    return {
        "source": service.get("source"), "currency": service.get("currency") or "USD",
        "current_annual": _money(current_annual), "current_monthly": _money(current_monthly), "obligations": len(obligations),
        "with_terms_annual": _money(after_annual), "with_terms_monthly": _money(after_monthly),
        "released_monthly": _money(current_monthly - after_monthly) if current_monthly is not None and after_monthly is not None else None,
        "retired": evaluation.get("retired_count") if planned else None,
        "terms": {key: terms.get(key) for key in ("label", "source", "amount", "rate_pct", "term_months", "monthly_payment")} if terms else None,
    }


def _metrics(ebitda, noi, revenue, annual_debt) -> dict[str, Any]:
    return {
        "ebitda": _money(ebitda), "noi": _money(noi),
        "dscr": round(ebitda / annual_debt, 2) if ebitda is not None and annual_debt else None,
        "free_cash_flow": _money(ebitda - annual_debt) if ebitda is not None and annual_debt is not None else None,
        "operating_margin": round(noi / revenue * 100, 2) if noi is not None and revenue else None,
        "debt_service": _money(annual_debt),
    }


def _targets(ebitda, debt: dict[str, Any]) -> list[dict[str, Any]]:
    """Earnings still to find for each coverage goal; a target is not a forecast."""
    def needed(ratio, annual):
        return _money(max(0.0, ratio * annual - ebitda)) if ebitda is not None and annual else None

    return [{"ratio": ratio, "current_debt": needed(ratio, debt["current_annual"]),
             "with_terms": needed(ratio, debt["with_terms_annual"])} for ratio in TARGET_RATIOS]


def _candidates(cockpit: dict[str, Any], selected_file: str | None) -> list[dict[str, Any]]:
    """Every source that could give earnings, with the one in use marked."""
    rows = []
    for year in reversed((cockpit.get("tax") or {}).get("years") or []):
        rows.append({"key": f"tax_return:{year.get('file_id')}", "kind": "tax_return", "label": f"Filed return {year.get('year')}",
                     "net_profit": _money(signed_number(year.get("income"))), "ebitda": _money(signed_number(year.get("ebitda"))),
                     "selected": str(year.get("file_id")) == selected_file})
    statement = cockpit.get("profit_and_loss")
    if statement:
        annualized = 0 < (statement.get("months_covered") or 0) < 12
        rows.append({"key": f"profit_and_loss:{statement.get('file_id')}", "kind": "profit_and_loss",
                     "label": f"P&L {statement.get('period_label') or ''}".strip() + (" · annualized" if annualized else ""),
                     "net_profit": _money(signed_number(statement.get("annualized_net_income"))),
                     "ebitda": _money(signed_number(statement.get("annualized_ebitda"))),
                     "selected": str(statement.get("file_id")) == selected_file})
    bank = (cockpit.get("bank") or {}).get("summary") or {}
    if bank.get("annualized_net") is not None:
        rows.append({"key": "bank_cash_flow", "kind": "bank_proxy", "label": "Bank cash retained · proxy",
                     "net_profit": _money(signed_number(bank.get("annualized_net"))), "ebitda": None, "selected": False})
    return rows


def _compare_statement(groups: dict[str, Any], cockpit: dict[str, Any], analyses: list[dict[str, Any]]) -> None:
    """Beside each line of the return, what the latest P&L says for the same line, annualized.

    For comparison only: the return stays the source and nothing here changes a
    figure. A P&L too short to annualize is left out rather than scaled up.
    """
    statement = cockpit.get("profit_and_loss") or {}
    file_id, factor = statement.get("file_id"), _annual_factor(statement.get("months_covered"))
    facts = next((row.get("key_facts") or {} for row in analyses if str(row.get("file_id")) == str(file_id)), None)
    if not file_id or factor is None or facts is None:
        return
    try:
        other = expense_groups(statement if isinstance(facts.get("income_statements"), list) else facts, kind="profit_and_loss")
        amounts = {row["key"]: _money(row["amount"] * factor) for row in other["rows"]}
    except Exception:  # noqa: BLE001 - an odd statement must not cost the file its baseline; the column is optional
        log.exception("financial baseline: statement comparison unavailable profile=%s", cockpit.get("profile_id"))
        return
    for row in groups["rows"]:
        if row["key"] in amounts:
            row["statement_amount"] = amounts[row["key"]]
    groups["statement"] = {"file_id": str(file_id), "label": f"P&L · {statement.get('period_label') or 'period unconfirmed'}",
                           "months_covered": statement.get("months_covered"), "annualized": factor != 1}


def expense_groups(facts: dict[str, Any], *, kind: str, interest: float | None = None) -> dict[str, Any]:
    """Named lines plus the remainder, so the list adds up to its source."""
    lines = named_lines(facts)
    total, total_path = fact(facts, _TOTAL_DEDUCTIONS if kind == "tax_return" else ("total_operating_expenses", "operating_expenses"))
    check = reconcile_lines(lines, total=total, interest=interest if kind == "tax_return" else None)
    rows = [dict(row) for row in lines]
    if check["reconciles"] and check["remainder"]:
        rows.append({"key": REMAINDER_KEY, "category": "other", "amount": check["remainder"], "fact_path": total_path, "derived": True,
                     "label": "Other deductions (rest of the return's total)" if kind == "tax_return" else "Other operating expenses (rest of the statement's total)"})
    outside = []
    if kind == "tax_return" and interest:
        outside.append({"key": "interest", "label": "Interest (financing, not an operating cost)", "amount": _money(interest)})
    return {"rows": rows, "outside_plan": outside, **check}


def safe_build(cockpit: dict[str, Any], analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """`build`, except that a fault here never takes the cockpit down.

    The baseline explains figures the cockpit already holds, so losing the
    explanation must not lose the figures.
    """
    try:
        return build(cockpit, analyses)
    except Exception:  # noqa: BLE001
        log.exception("financial baseline unavailable profile=%s", cockpit.get("profile_id"))
        return {"policy_version": POLICY_VERSION, "unavailable": True,
                "source": {"kind": None, "label": None, "file_id": None, "selected_by": None},
                "reason": "The baseline could not be assembled. The file's other figures are unaffected.",
                "ladder": [], "figures": None, "staff_adjustments": [], "groups": None, "debt_service": {},
                "metrics": {"current": {}, "with_terms": None}, "targets": [], "sources": [], "notes": []}


def build(cockpit: dict[str, Any], analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """The baseline block of the cockpit; reads the selection, never makes one."""
    earnings = cockpit.get("ebitda") or {}
    selection = earnings.get("source_selection") or {}
    headline = earnings.get("headline") or {}
    noi_headline = (cockpit.get("noi") or {}).get("headline") or {}
    selected = next((row for row in selection.get("sources") or [] if row.get("selected")), None)
    selected_file = selected["file_id"] if selected else None
    facts = next((row.get("key_facts") or {} for row in analyses if str(row.get("file_id")) == selected_file), {})
    debt = _debt_service(cockpit)
    notes: list[str] = []
    ladder = groups = None
    source: dict[str, Any] = {"kind": None, "label": None, "file_id": None, "selected_by": None}
    if selected and selected.get("kind") == "tax_return":
        year = next((row for row in (cockpit.get("tax") or {}).get("years") or [] if str(row.get("file_id")) == selected_file), {})
        ladder = return_ladder(facts, income=signed_number(year.get("income")), interest=signed_number(year.get("interest")),
                               depreciation=signed_number(year.get("depreciation")), revenue=signed_number(year.get("gross_receipts")))
        groups = expense_groups(facts, kind="tax_return", interest=ladder["interest"])
        _compare_statement(groups, cockpit, analyses)
        form = _FORMS.get(year.get("tax_form"), year.get("tax_form"))
        source = {"kind": "tax_return", "label": f"IRS {form} · {year.get('year')}" if form else f"Filed return · {year.get('year')}", "file_id": selected_file,
                  "file_name": selected.get("file_name"), "form": year.get("tax_form"), "year": year.get("year"),
                  "period_start": year.get("period_start"), "period_end": year.get("period_end"),
                  "period_assumed": bool(headline.get("period_assumed")), "months_covered": headline.get("months_covered"),
                  "currency": headline.get("currency") or year.get("currency"), "basis": year.get("basis") or None,
                  "income_basis": year.get("income_basis"), "accepted": bool(year.get("accepted"))}
        if source["period_assumed"]:
            notes.append(f"The return prints tax year {year.get('year')} without period dates; it is read as that full year.")
    elif selected and selected.get("kind") == "profit_and_loss":
        statement = cockpit.get("profit_and_loss") or {}
        if str(statement.get("file_id")) == selected_file:
            ladder = _statement_ladder(statement, noi_headline, headline)
            groups = expense_groups(facts if not isinstance(facts.get("income_statements"), list) else statement, kind="profit_and_loss")
            factor = _annual_factor(statement.get("months_covered"))
            if groups and factor and factor != 1:
                for row in groups["rows"]:
                    row["period_amount"], row["amount"] = row["amount"], _money(row["amount"] * factor)
                for key in ("listed", "total", "remainder"):
                    groups[key] = _money(groups[key] * factor) if groups.get(key) is not None else None
            source = {"kind": "profit_and_loss", "label": f"P&L · {statement.get('period_label') or 'period unconfirmed'}",
                      "file_id": selected_file, "file_name": selected.get("file_name"),
                      "period_start": statement.get("period_start"), "period_end": statement.get("period_end"),
                      "period_assumed": False, "months_covered": statement.get("months_covered"),
                      "annualized": bool(factor and factor != 1), "currency": headline.get("currency"),
                      "basis": headline.get("basis"), "accepted": bool(statement.get("accepted")), "fallback": True}
            notes.append("No filed business return is in use, so the P&L stands in as the labelled fallback.")
    if source["kind"]:
        source["selected_by"] = "staff" if selection.get("mode") == "manual" else "rule"
    ebitda = signed_number(headline.get("annualized_value"))
    noi = signed_number(noi_headline.get("annualized_value"))
    revenue = ladder["revenue"] if ladder else None
    # The headline figures are what the rest of the system uses. Where a staff
    # review moved one, the ladder shows that as its own step and still adds up.
    adjustments = []
    if ladder:
        for key, value, label in (("noi", noi, "NOI"), ("ebitda", ebitda, "EBITDA")):
            if value is not None and ladder[key] is not None and abs(value - ladder[key]) > TOLERANCE:
                adjustments.append({"key": key, "label": f"Staff review of {label}", "amount": _money(value - ladder[key])})
        for name in ladder["unread"]:
            notes.append(("Interest" if name == "interest" else "Depreciation and amortization")
                         + " was not read from the source, so none is added back. Enter it from the document to include it.")
        if ladder["reconciliation_difference"] is not None:
            notes.append("The source's printed expenses differ from the figure its bottom line implies by "
                         f"{abs(ladder['reconciliation_difference']):,.0f}. Review how the lines were read.")
    if groups and groups["status"] == "lines_exceed_total":
        notes.append("The listed expense lines add up to more than the source's total; one line is counted twice or read wrong.")
    current = _metrics(ebitda, noi, revenue, debt["current_annual"])
    with_terms = _metrics(ebitda, noi, revenue, debt["with_terms_annual"]) if debt["with_terms_annual"] is not None else None
    return {
        "policy_version": POLICY_VERSION, "source": source, "reason": earnings.get("reason") if not source["kind"] else None,
        "ladder": _rows(ladder, kind=source["kind"]) if ladder else [], "figures": ladder, "staff_adjustments": adjustments,
        "groups": groups, "debt_service": debt,
        "metrics": {"current": current, "with_terms": with_terms},
        "targets": _targets(ebitda, debt), "sources": _candidates(cockpit, selected_file), "notes": notes,
    }
