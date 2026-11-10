"""Source-bound factual inputs; hypothetical financing stays in financial_plan.

The source documents and immutable form versions are never rewritten. A new
analysis, period, account selection or published debt basis makes a correction
stale until staff reviews the new evidence. All arithmetic defaults are the
cockpit's existing underwriting rules, including its labelled tax estimate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.services import public_underwriting_packet_pdf as extraction
from app.services.public_underwriting_packet_pdf import _num as source_number

log = logging.getLogger(__name__)


def stamp(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _entity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _currency(value: Any) -> str | None:
    reported = str(value or "").strip().upper()
    return reported if re.fullmatch(r"[A-Z]{3}", reported) and reported != "XXX" else None


def _currency_comparison(source_refs):
    """Describe the existing ratio's denominations; never convert its amounts."""
    reported = sorted({str(ref.get("currency")).strip().upper() for ref in source_refs if ref.get("currency")})
    normalized = [_currency(ref.get("currency")) for ref in source_refs]
    known = {value for value in normalized if value}
    mismatch = any(value != "USD" for value in known)
    unknown = not normalized or any(value is None for value in normalized)
    status = "mismatch" if mismatch else "unknown" if unknown else "comparable"
    if mismatch:
        note = f"Source currency {', '.join(reported)} differs from the USD debt-service denomination. Amounts are retained without currency conversion; coverage is unavailable until their denominations are reconciled."
    elif unknown:
        note = "Source currency is unconfirmed against USD debt service. The existing ratio is shown without currency conversion; confirm currency before relying on it."
    else:
        note = None
    return {"numerator_currency": next(iter(known)) if len(known) == 1 and not unknown else None,
            "denominator_currency": "USD", "source_currencies": reported,
            "currency_comparable": status == "comparable", "currency_status": status, "currency_note": note,
            "denominator_currency_basis": "Application debt schedules and debt-service figures are denominated in USD; source amounts are not converted."}


def _months(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        return (last.year - first.year) * 12 + last.month - first.month + 1 if last >= first else None
    except (ValueError, TypeError):
        return None


def prepare(analyses, years, statement, bank, months, accounts, service, *, state=None, entity=None, debt_rows=None):
    """Apply matching factual corrections before any DSCR or scenario math."""
    state = state or {}
    from app.services.document_associations import attach_rows, source_excluded

    if any("identity_association" not in row for row in analyses):
        analyses = attach_rows(analyses, profile_id=None, expected_entity=entity, state=state)
    bank = dict(bank)  # Reviewed aggregates never rewrite historical statement months.
    latest = {event["input_key"]: event for event in state.get("events", []) if event.get("input_key")}
    by_file = {str(item.get("file_id")): item for item in analyses}
    from app.services.financial_fact_citations import validated_fact_citations_for_analysis

    verified = {key: validated_fact_citations_for_analysis(SimpleNamespace(id=item.get("analysis_id"), bucket_file_id=key,
                content_hash=item.get("content_hash"), status="completed", analysis={"key_facts": item.get("key_facts"), "financial_fact_citations": item.get("financial_fact_citations")})) for key, item in by_file.items()}
    debt_context = {key: service.get(key) for key in ("version_id", "source", "monthly", "annual", "schedule_authoritative", "declared_none", "identity_association_conflict", "identity_association_stamp")}
    context = {"debt": {**debt_context, "rows_stamp": stamp(debt_rows or [])}, "entity": entity,
               "accounts": [{"key": a.get("key"), "members": a.get("members"), "counted": a.get("counted")} for a in accounts]}
    inputs, calculations, results = [], [], []

    def refs(record):
        from app.services.document_associations import summary

        source = by_file.get(str(record.get("file_id")), {})
        facts = source.get("key_facts") or {}
        return [{"file_id": record.get("file_id"), "file_name": source.get("file_name"),
                 "analysis_id": source.get("analysis_id"), "content_hash": source.get("content_hash"),
                 "accepted": bool(source.get("accepted")), "entity": record.get("entity") or record.get("business_name") or facts.get("entity_name") or facts.get("business_name") or facts.get("entity"),
                 "period_start": record.get("period_start"), "period_end": record.get("period_end"),
                 "statement_period": facts.get("statement_period"),
                 "currency": record.get("currency") or facts.get("currency"), "basis": record.get("basis") or facts.get("basis"),
                 "identity_association": summary(source.get("identity_association")), "facts_stamp": stamp(facts)}]

    def operand(key, label, value, *, source_refs, period, editable=True, unit="currency", fact_keys=None, canonical=None, **extra):
        field = key.rsplit(".", 1)[-1]
        if fact_keys is None:
            fact_keys = ("ebitda",) if field == "reported_ebitda" else (field,)
        enriched = []
        for ref in source_refs:
            facts = (by_file.get(str(ref.get("file_id")), {}).get("key_facts") or {})
            def fact_value(path, source=facts):
                value = source
                for part in path.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                return source_number(value)

            fact_key = next((alias for alias in fact_keys if fact_value(alias) is not None), None)
            canonical_keys = (canonical,) if isinstance(canonical, str) else canonical or (field,)
            citations = [citation for name in canonical_keys for citation in verified.get(str(ref.get("file_id")), {}).get(name, [])]
            enriched.append({**ref, "fact_key": fact_key or fact_keys[0], "fact_present": fact_key is not None,
                             "citations": citations, "pages": sorted({c["page"] for c in citations if isinstance(c.get("page"), int)})})
        source_refs = enriched
        dependencies = {"entity": entity}
        if key.startswith("bank."):
            dependencies["accounts"] = context["accounts"]
        if key.startswith("debt."):
            dependencies["debt"] = context["debt"]
        source_stamp = stamp({"key": key, "original": value, "refs": source_refs, "period": period, "dependencies": dependencies})
        event = latest.get(key)
        from app.services.document_associations import identity_matches

        mismatch = bool(entity and any(source_excluded(ref.get("identity_association")) or ref.get("entity") and not identity_matches(entity, ref.get("entity"), ref.get("identity_association")) for ref in source_refs))
        correction = event if event and event.get("action") == "correct" and event.get("source_stamp") == source_stamp and not mismatch else None
        effective = correction["value"] if correction else value
        inputs.append({"key": key, "label": label, "original_value": value, "value": effective,
                       "unit": unit, "editable": editable and bool(source_refs) and not mismatch, "source_stamp": source_stamp,
                       "source_refs": source_refs, "period": period, "missing": effective is None,
                       "correction": correction, "unavailable_reason": "Review the document's business association in Evidence before correcting its financial inputs." if mismatch else None, **extra})
        return effective

    for year in years:
        source_refs = refs(year)
        prefix = f"tax_return:{year.get('file_id')}:{year.get('year')}"
        period = str(year.get("year") or "Unknown tax period")
        tax_months = _months(year.get("period_start"), year.get("period_end"))
        year["annual_coverage_eligible"] = tax_months == 12 if year.get("period_start") or year.get("period_end") else True
        if year.get("period_start") or year.get("period_end"):
            period += f" ({year.get('period_start') or '?'} – {year.get('period_end') or '?'})"
        keys = [f"{prefix}.{key}" for key in ("income", "interest", "depreciation")]
        from app.services.financial_cockpit import _TAX_INCOME_BASES, _TAX_LINE_ALIASES

        income_field = next((key for key, label in _TAX_INCOME_BASES if label == year.get("income_basis")), "income")
        income_aliases = {"ordinary_business_income": extraction._ORDINARY_BUSINESS_INCOME_ALIASES,
                          "net_income": extraction._NET_INCOME_ALIASES,
                          "net_income_per_books": extraction._BOOK_NET_INCOME_ALIASES}.get(income_field, (income_field,))
        for key, label in (("income", f"Tax income ({year.get('income_basis') or 'unknown basis'})"), ("interest", "Tax interest deduction"), ("depreciation", "Tax current-year depreciation and amortization")):
            aliases = income_aliases if key == "income" else tuple((year.get("component_fact_paths") or {}).get(key) or _TAX_LINE_ALIASES[key])
            year[key] = operand(f"{prefix}.{key}", label, year.get(key), source_refs=source_refs, period=period,
                                fact_keys=aliases, canonical=income_field if key == "income" else key)
        year["gross_receipts"] = operand(f"{prefix}.gross_receipts", "Business tax-return revenue", year.get("gross_receipts"),
            source_refs=source_refs, period=period, fact_keys=extraction._PL_REVENUE_ALIASES,
            canonical="revenue", minimum=0)
        year["ebitda"] = round(year["income"] + (year["interest"] or 0) + (year["depreciation"] or 0), 2) if year["income"] is not None else None
        depreciation_input = next(i for i in inputs if i["key"] == f"{prefix}.depreciation")
        year["ebitda_complete"] = all(year.get(key) is not None for key in ("income", "interest", "depreciation")) and bool(year.get("depreciation_complete") or depreciation_input.get("correction"))
        missing = [key for key in keys if next(i for i in inputs if i["key"] == key)["missing"]]
        notes = ["Tax-derived earnings estimate: tax income + known interest + known current-year depreciation and amortization. This is not a reconciliation from book net income to full EBITDA; unknown components remain flagged for review."]
        if year.get("depreciation") is not None and not year["ebitda_complete"]:
            notes.append("Only the known current-year depreciation/amortization components are included; the remaining component has not been established.")
        if tax_months != 12:
            notes.append("The tax reporting duration is unknown or is not a full twelve months. This period estimate is not annualized; existing DSCR still uses its reported tax basis without a new annualization assumption.")
        source_entity = source_refs[0].get("entity")
        from app.services.document_associations import identity_matches

        identity_known = bool(source_entity or (source_refs[0].get("identity_association") or {}).get("status") == "accepted")
        mismatch = bool(entity and identity_known and not identity_matches(entity, source_entity, source_refs[0].get("identity_association")))
        if not source_entity:
            notes.append("The source business identity is not established.")
        if mismatch:
            notes.append("The source names a different business; excluded from business earnings and DSCR.")
        result = {"key": prefix, "label": "Tax-derived earnings estimate", "value": year["ebitda"],
                  "annualized_value": year["ebitda"] if tax_months == 12 else None, "period": period, "months_covered": tax_months,
                  "period_start": year.get("period_start"), "period_end": year.get("period_end"),
                  "annualization_factor": 1 if tax_months == 12 else None, "kind": "tax_estimate", "complete": False, "provisional": True,
                  "formula": "tax income + known interest + known current-year depreciation and amortization", "input_keys": keys,
                  "missing_inputs": missing, "notes": notes, "source_refs": source_refs, "entity_mismatch": mismatch}
        result["entity_comparable"] = bool(entity and identity_known and not mismatch)
        result["currency"], result["basis"] = source_refs[0].get("currency"), source_refs[0].get("basis")
        if not _currency(result["currency"]):
            notes.append("The source currency is not established; this amount retains its source denomination without currency conversion.")
        results.append(result)
        calculations.append(dict(result))

    if statement:
        source_refs = refs(statement)
        raw = (by_file.get(str(statement.get("file_id")), {}).get("key_facts") or {})
        period = statement.get("period_label") or "Unknown statement period"
        for key, label, canonical in (
            ("gross_revenue", "P&L operating revenue", "revenue"),
            ("cost_of_goods_sold", "P&L cost of goods sold", "cogs"),
            ("total_operating_expenses", "P&L operating expenses (including expensed D&A)", "operating_expenses"),
        ):
            aliases = extraction._PL_REVENUE_ALIASES if key == "gross_revenue" else (key, "cogs") if key == "cost_of_goods_sold" else (key, "operating_expenses")
            source_value = next((value for alias in aliases if (value := source_number(raw.get(alias))) is not None), None)
            statement[key] = operand(f"profit_and_loss.{key}", label, source_value, source_refs=source_refs, period=period,
                fact_keys=aliases, canonical=canonical)
        statement["reported_operating_income"] = operand("profit_and_loss.reported_operating_income", "Source-reported operating income (NOI)",
            next((value for alias in ("operating_income", "operating_profit", "net_operating_income") if (value := source_number(raw.get(alias))) is not None), None), source_refs=source_refs, period=period,
            fact_keys=("operating_income", "operating_profit", "net_operating_income"), canonical="operating_income")
        original_months = statement.get("months_covered")
        months_value = operand("profit_and_loss.months_covered", "P&L reporting months", original_months, source_refs=source_refs, period=period, unit="months", minimum=1, maximum=12)
        statement["months_covered"] = int(months_value) if months_value is not None else None
        fields = ("net_income", "interest", "income_taxes", "depreciation_and_amortization")
        labels = ("P&L net income", "P&L interest", "P&L income taxes (excluding taxes and licenses)", "P&L depreciation and amortization")
        for key, label in zip(fields, labels, strict=True):
            statement[key] = operand(f"profit_and_loss.{key}", label, statement.get(key), source_refs=source_refs, period=period,
                                     fact_keys=extraction._PL_NET_INCOME_ALIASES if key == "net_income" else (key,))
        reported = operand("profit_and_loss.reported_ebitda", "Source-reported EBITDA", source_number(raw.get("ebitda")), source_refs=source_refs, period=period)
        computed = round(sum(statement[key] for key in fields), 2) if all(statement.get(key) is not None for key in fields) else None
        value = reported if reported is not None else computed
        statement["ebitda"] = value
        covered = statement["months_covered"]
        factor = 12 / covered if covered is not None and 6 <= covered <= 11 else (1 if covered == 12 else None)
        statement["annualized_ebitda"] = round(value * factor, 2) if value is not None and factor is not None else None
        # Preserve the separate legacy net-income annualization rule.
        net_factor = 12 / covered if covered and 0 < covered < 12 else 1
        statement["annualized_net_income"] = round(statement["net_income"] * net_factor, 2) if statement["net_income"] is not None else None
        if statement.get("gross_revenue"):
            statement["net_margin"] = round(statement["net_income"] / statement["gross_revenue"], 4) if statement["net_income"] is not None else None
        keys = [f"profit_and_loss.{key}" for key in fields]
        missing = [key for key in keys if next(i for i in inputs if i["key"] == key)["missing"]]
        notes = []
        financing_expense = source_number((raw.get("notable_expense_lines") or {}).get("bank_loan_payments_ytd"))
        if financing_expense is not None and financing_expense > 0:
            statement["dscr_reconciliation_required"] = True
            notes.append(f"The P&L includes {financing_expense:,.2f} of bank loan payments in expenses. Reconcile principal versus interest before relying on EBITDA; no automatic loan-payment addback has been applied.")
        if factor is None:
            notes.append("Period EBITDA is shown without annualization; DSCR requires a known 6–12 month P&L period.")
        if months_value != original_months:
            notes.append("Staff corrected the reporting-month count; original source dates remain unchanged.")
        if reported is not None and computed is not None and abs(reported - computed) > 0.01:
            notes.append("Source-reported EBITDA differs from net income + interest + income taxes + D&A. Reported EBITDA remains the existing underwriting basis; reconcile the difference.")
        if reported is not None and computed is None:
            notes.append("Source-reported EBITDA cannot yet be reconciled to net income because one or more reconciliation components are unknown.")
        source_entity = source_refs[0].get("entity")
        from app.services.document_associations import identity_matches

        identity_known = bool(source_entity or (source_refs[0].get("identity_association") or {}).get("status") == "accepted")
        mismatch = bool(entity and identity_known and not identity_matches(entity, source_entity, source_refs[0].get("identity_association")))
        if not source_entity:
            notes.append("The source business identity is not established.")
        if mismatch:
            notes.append("The source names a different business or location; confirm its association in Evidence before using it for business DSCR.")
        currency_known = _currency(source_refs[0].get("currency")) is not None
        basis_known = str(source_refs[0].get("basis") or "").strip().casefold() in {"cash", "accrual"}
        if not currency_known:
            notes.append("The source currency is not established; confirm its denomination before comparing this EBITDA with other figures.")
        if not basis_known:
            notes.append("The source accounting basis is unconfirmed; this EBITDA remains provisional.")
        result = {"key": "profit_and_loss", "label": "Reported EBITDA" if reported is not None else "EBITDA",
                  "value": value, "annualized_value": statement["annualized_ebitda"], "period": period,
                  "months_covered": covered, "annualization_factor": factor,
                  "kind": "reported_ebitda" if reported is not None else "computed_ebitda",
                  "complete": computed is not None, "provisional": not bool(source_refs[0].get("accepted")) or not identity_known or not entity or computed is None or not currency_known or not basis_known or bool(statement.get("dscr_reconciliation_required")),
                  "formula": "source-reported EBITDA" if reported is not None else "net income + interest + income taxes + depreciation and amortization",
                  "input_keys": ["profit_and_loss.reported_ebitda"] if reported is not None else keys,
                  "missing_inputs": missing, "notes": notes, "source_refs": source_refs,
                  "computed_value": computed, "reported_value": reported, "entity_mismatch": mismatch}
        result["entity_comparable"] = bool(entity and identity_known and not mismatch)
        result["currency"], result["basis"] = source_refs[0].get("currency"), source_refs[0].get("basis")
        results.append(result)
        calculations.append(dict(result))
        calculations.append({"key": "profit_and_loss.reconciliation", "label": "EBITDA reconciliation from net income",
                             "formula": "net income + interest + income taxes + depreciation and amortization",
                             "value": computed, "reported_value": reported,
                             "difference": round(reported - computed, 2) if reported is not None and computed is not None else None,
                             "input_keys": keys, "period": period, "annualization_factor": 1,
                             "missing_inputs": missing, "notes": notes, "source_refs": source_refs})

    bank_ids = {str(key) for month in months for key in month.get("file_ids", [])}
    bank_refs = [refs({"file_id": key, "period_start": (by_file.get(key, {}).get("key_facts") or {}).get("period_start"),
                       "period_end": (by_file.get(key, {}).get("key_facts") or {}).get("period_end")})[0] for key in sorted(bank_ids)]
    bank_period = f"{bank.get('first_month') or '?'} – {bank.get('last_month') or '?'}"
    bank["avg_net"] = operand("bank.avg_net", "Average monthly bank cash retained", bank.get("avg_net"), source_refs=bank_refs, period=bank_period,
                              fact_keys=("derived:average(monthly_deposits-minus-withdrawals)",),
                              canonical=("total_deposits_and_credits", "total_withdrawals_and_debits"),
                              details={"months": [{key: month.get(key) for key in ("month", "accounts", "file_ids", "deposits", "withdrawals", "net")} for month in months],
                                       "included_net_months": sum(month.get("net") is not None for month in months),
                                       "counted_accounts": context["accounts"]})
    financing = bank.get("financing_adjustment") or {}
    operand("bank.identified_financing_credits_monthly", "Identified financing credits (monthly equivalent; excluded)",
            financing.get("identified_monthly"), source_refs=bank_refs, period=bank_period, editable=False,
            fact_keys=("derived:identified_financing_credits_over_observed_net_months",), canonical="recurring_credits",
            details=financing)
    if bank.get("estimates_available"):
        bank["annualized_net"] = round(bank["avg_net"] * 12, 2) if bank["avg_net"] is not None else None
    for key, label in (("avg_deposits", "Average monthly deposits"), ("avg_withdrawals", "Average monthly withdrawals")):
        fact_key = "total_deposits_and_credits" if key == "avg_deposits" else "total_withdrawals_and_debits"
        bank[key] = operand(f"bank.{key}", label, bank.get(key), source_refs=bank_refs, period=bank_period, fact_keys=(fact_key,), canonical=fact_key,
                            minimum=0, details={"months": months, "counted_accounts": context["accounts"]})
    bank["average_balance"] = operand("bank.average_balance", "Reviewed average account balance", bank.get("average_balance"),
        source_refs=bank_refs, period=bank_period, fact_keys=("average_daily_balance", "average_ledger_balance", "ending_balance"),
        canonical="ending_balance" if bank.get("average_balance_basis") == "ending_balance" else "average_daily_balance",
        details={"months": months, "basis": bank.get("average_balance_basis"), "counted_accounts": context["accounts"]})
    known_nsf = [number(month.get("nsf")) for month in months]
    known_nsf = [value for value in known_nsf if value is not None and value >= 0 and value == int(value)]
    bank["nsf_total"] = operand("bank.nsf_total", "Reviewed NSF and overdraft item count", sum(known_nsf) if known_nsf else None,
        source_refs=bank_refs, period=bank_period, unit="number", minimum=0, whole_number=True,
        fact_keys=("nsf_or_overdraft_count",), canonical="nsf_or_overdraft_count", details={"months": months, "counted_accounts": context["accounts"]})
    if bank.get("estimates_available"):
        for source, annual in (("avg_deposits", "annualized_deposits"), ("avg_withdrawals", "annualized_withdrawals")):
            bank[annual] = round(bank[source] * 12, 2) if bank.get(source) is not None else None
    operand("debt.monthly", "Published / provisional monthly debt service", service.get("monthly"), source_refs=[{"version_id": service.get("version_id"), "basis": service.get("source"), "currency": "USD"}], period="Monthly", editable=False, editor_action="debt_schedule",
            details={"rows": debt_rows or [], "service": dict(service)})
    operand("debt.annual", "Annual debt service", service.get("annual"), source_refs=[{"version_id": service.get("version_id"), "basis": service.get("source"), "currency": "USD"}], period="Annual", editable=False)
    active_inputs = {i["key"]: i for i in inputs}
    stale = [event for key, event in latest.items() if event.get("action") == "correct" and (key not in active_inputs or active_inputs[key]["source_stamp"] != event.get("source_stamp"))]
    eligible = [result for result in results if result["value"] is not None and not result["entity_mismatch"]
                and (not entity or result.get("entity_comparable"))]
    headline = next((r for r in eligible if r["kind"] != "tax_estimate"), None) or next((r for r in reversed(eligible) if r["kind"] == "tax_estimate"), None)
    return {"revision": state.get("revision", 0), "source_stamp": stamp([i["source_stamp"] for i in inputs]),
            "inputs": inputs, "calculations": calculations, "history": [event for event in state.get("events", []) if event.get("input_key")],
            "stale_corrections": [{**event, "stale": True} for event in stale],
            "_bank": bank,
            "scope": "Effective cockpit facts and downstream cockpit consumers. Original documents, financial form versions and hypothetical plan overrides are unchanged.",
            "ebitda": {"headline": headline, "candidates": results, "reason": None if headline else "No source-supported EBITDA or tax-derived estimate is available. Bank cash flow is not EBITDA."}}


def finish(review, dscr, service, bank):
    """Trace the actual selected DSCR, including its bank debt-payment addback."""
    for candidate in dscr.get("candidates", []):
        if candidate["key"] == "tax_return":
            earnings = next((r for r in reversed(review["calculations"]) if r.get("kind") == "tax_estimate"
                             and r.get("value") is not None and (not candidate.get("source_file_id")
                             or any(str(ref.get("file_id")) == str(candidate["source_file_id"]) for ref in r.get("source_refs", [])))), None)
            keys = earnings["input_keys"] if earnings else []
            formula = "(tax income + known interest + known current-year depreciation and amortization) / annual debt service"
        elif candidate["key"] == "profit_and_loss":
            earnings = next((r for r in review["calculations"] if r.get("key") == "profit_and_loss"), None)
            keys = (earnings["input_keys"] + ["profit_and_loss.months_covered"]) if earnings else []
            formula = "(period EBITDA × eligible annualization factor) / annual debt service"
        else:
            earnings = None
            keys = ["bank.avg_net", "debt.monthly", "bank.identified_financing_credits_monthly"]
            formula = "((average monthly bank cash retained + scheduled monthly debt service − identified financing credits monthly equivalent) × 12) / annual debt service"
        candidate["input_keys"] = keys + ["debt.annual"]
        candidate["source_refs"] = earnings["source_refs"] if earnings else next(i["source_refs"] for i in review["inputs"] if i["key"] == "bank.avg_net")
        comparison = _currency_comparison(candidate["source_refs"])
        candidate.update(comparison)
        if comparison["currency_status"] == "mismatch":
            candidate["source_numerator"] = candidate["numerator"]
            candidate["numerator"] = None
            candidate["value"] = candidate["pro_forma_value"] = None
            candidate["status"] = candidate["pro_forma_status"] = "unavailable"
            candidate["ineligible_reason"] = "Source currency differs from debt service; no currency conversion has been reviewed."
        if comparison["currency_note"]:
            candidate["note"] = " ".join(part for part in (candidate.get("note"), comparison["currency_note"]) if part)
        review["calculations"].append({"key": f"dscr.{candidate['key']}", "label": candidate["label"] + " DSCR", "formula": formula,
                                       "value": candidate.get("value"), "numerator": candidate.get("numerator"), "denominator": service.get("annual"),
                                       "input_keys": candidate["input_keys"], "period": candidate.get("period"),
                                       "annualization_factor": 1 if candidate["key"] == "tax_return" else earnings.get("annualization_factor") if earnings else 12,
                                       "missing_inputs": [i["key"] for i in review["inputs"] if i["key"] in candidate["input_keys"] and i["missing"]],
                                       "notes": ([candidate["note"]] if candidate.get("note") else []) + (earnings.get("notes", []) if earnings else []),
                                       "source_refs": candidate["source_refs"], "selected": candidate is dscr.get("headline"),
                                       "basis_status": service.get("basis_status"), "debt_version_id": service.get("version_id"),
                                       "entity_comparable": earnings.get("entity_comparable", False) if earnings else False,
                                       **comparison,
                                       "observed_bank_months": bank.get("months")})
    for item in review["inputs"]:
        item["used_by"] = [calculation["key"] for calculation in review["calculations"] if item["key"] in calculation.get("input_keys", [])]
        item["used_by_headline"] = [calculation["key"] for calculation in review["calculations"] if calculation.get("selected") and item["key"] in calculation.get("input_keys", [])]
    dscr["headline"] = next((candidate for candidate in dscr.get("candidates", []) if candidate.get("value") is not None), None)
    dscr["pro_forma_headline"] = next((candidate for candidate in dscr.get("candidates", []) if candidate.get("pro_forma_value") is not None), None)
    if dscr["headline"] is None and any(candidate.get("ineligible_reason") for candidate in dscr.get("candidates", [])):
        dscr["reason"] = "Available earnings and debt service have different currencies; reconcile the source currency before calculating coverage."
    for calculation in review["calculations"]:
        if str(calculation.get("key", "")).startswith("dscr."):
            calculation["selected"] = bool(dscr["headline"] and calculation["key"] == f"dscr.{dscr['headline']['key']}")
    for item in review["inputs"]:
        item["used_by_headline"] = [calculation["key"] for calculation in review["calculations"] if calculation.get("selected") and item["key"] in calculation.get("input_keys", [])]


def redact_for_external(cockpit, actor):
    """Retain existing numeric visibility without exposing internal staff audit."""
    if actor.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        return cockpit
    from copy import deepcopy

    result = deepcopy(cockpit)
    from app.services.financing_budget_disclosure import public_budget

    budget = result.get("deal_budget") or {}
    safe_budget = public_budget({"current": budget, "basis": "staff_proposal", "calculation": budget.get("calculation")}) if budget else {}
    private_budget_keys = {"deal_budget", "deal_budget_snapshot", "deal_budget_calculation", "current_deal_budget", "current_deal_budget_calculation", "financing_budget_calculation"}
    def remove_private_budget(value):
        if isinstance(value, dict):
            for key in list(value):
                if key in private_budget_keys:
                    value.pop(key)
                elif key == "principal_solution" and isinstance(value[key], dict):
                    value[key].pop("issues", None)
                else:
                    remove_private_budget(value[key])
        elif isinstance(value, list):
            for item in value:
                remove_private_budget(item)
    remove_private_budget(result)
    result["deal_budget"] = safe_budget
    # These roles retain their pre-existing financial/audit visibility, while
    # cost recipient identities use the dedicated budget endpoint's narrower ACL.
    if actor.role in {Role.REGIONAL_MANAGER, Role.BROKER, Role.FIELD_REP}:
        return result
    # The new staff source picker contains the private file inventory and
    # review concurrency fingerprints. Client forms use their own scoped links.
    result["operating_evidence"] = {}
    noi = result.get("noi") or {}
    noi.pop("review", None)
    for calculation in [noi.get("headline"), *((result.get("metric_review") or {}).get("calculations") or [])]:
        if isinstance(calculation, dict):
            calculation.pop("review_receipt", None)
    operating = result.get("operating_plan")
    if operating:
        from app.capital_partners.services.listings import partner_operating_earnings

        safe = partner_operating_earnings({"improvement_plan": operating.get("package")}) or {}
        result["operating_plan"] = {key: operating[key] for key in ("available", "state", "version_number", "saved_at") if key in operating}
        result["operating_plan"]["package"] = safe.get("improvement_plan")
    review = result.get("metric_review") or {}
    review["history"] = []
    review["composition_history"] = []
    review["document_associations"] = []
    selection = (result.get("ebitda") or {}).get("source_selection")
    if selection:
        selection["history"] = []
    review["stale_correction_count"] = len(review.get("stale_corrections", []))
    review["stale_corrections"] = []
    for item in review.get("inputs", []):
        item["editable"] = False
        if item.get("correction"):
            item["correction"] = {key: item["correction"].get(key) for key in ("value", "at")}
    review["stale_compositions"] = []
    for calculation in review.get("calculations", []):
        # Recipe adjustment labels are arbitrary internal staff text. The
        # numeric source/reviewed results remain visible without that audit.
        calculation.pop("recipe", None)
    for candidate in (result.get("dscr") or {}).get("candidates", []):
        if candidate.get("calculation_review"):
            candidate["calculation_review"] = {"revision": candidate["calculation_review"].get("revision"), "status": "staff_adjusted"}
    return result


def change(state, review, *, input_key, action, value, reason, expected_revision, source_stamp, idempotency_key, actor, mark_unknown=False, now=None):
    """Pure audited transition, allowing receipt replay before stale-source checks."""
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    state = state or {}
    request = {"input_key": input_key, "action": action, "value": value, "mark_unknown": mark_unknown, "reason": reason.strip(), "source_stamp": source_stamp, "expected_revision": expected_revision}
    request_stamp = stamp(request)
    for event in state.get("events", []):
        if event.get("idempotency_key") == idempotency_key:
            if event.get("request_stamp") != request_stamp:
                raise HTTPException(409, "Idempotency key already used for a different correction")
            return state, False
    if state.get("revision", 0) != expected_revision:
        raise HTTPException(409, "Financial corrections changed; reload before saving")
    target = next((i for i in review["inputs"] if i["key"] == input_key), None)
    if target is None or not target["editable"]:
        raise HTTPException(422, "This calculation input must be corrected in its source workspace")
    if target["source_stamp"] != source_stamp:
        raise HTTPException(409, "Source evidence changed; review the current input before saving")
    if not reason.strip() or len(reason) > 2000:
        raise HTTPException(422, "A correction reason is required")
    if action not in {"correct", "restore"}:
        raise HTTPException(422, "Unknown correction action")
    numeric = number(value)
    if mark_unknown and (action != "correct" or value is not None):
        raise HTTPException(422, "Marking an input unknown requires a correction with an explicitly empty value")
    if action == "correct" and numeric is None and not mark_unknown:
        raise HTTPException(422, "A finite numeric value is required; unknown values cannot be invented")
    if numeric is not None and abs(numeric) > 1e15:
        raise HTTPException(422, "The correction exceeds the supported amount range")
    if target["unit"] == "months" and action == "correct" and numeric is not None and (numeric != int(numeric) or not 1 <= numeric <= 12):
        raise HTTPException(422, "Reporting months must be a whole number from 1 to 12")
    if numeric is not None and target["unit"] == "currency":
        numeric = float(Decimal(str(numeric)).quantize(Decimal("0.01")))
    if numeric is not None and action == "correct":
        if target.get("minimum") is not None and numeric < target["minimum"]:
            raise HTTPException(422, "This input cannot be below its permitted minimum")
        if target.get("whole_number") and numeric != int(numeric):
            raise HTTPException(422, "This count must be a whole number")
    revision = expected_revision + 1
    event = {"id": str(uuid.uuid4()), **request, "value": numeric if action == "correct" else None,
             "previous_value": target["value"], "original_value": target["original_value"],
             "effective_value": numeric if action == "correct" else target["original_value"],
             "source_refs": target["source_refs"], "period": target["period"],
             "actor_id": str(actor.id), "actor_name": getattr(actor, "full_name", None) or getattr(actor, "name", None),
             "at": (now or datetime.now(UTC)).isoformat(), "revision": revision,
             "idempotency_key": idempotency_key, "request_stamp": request_stamp}
    return {"revision": revision, "events": [*state.get("events", []), event]}, True


def change_many(state, review, *, corrections, reason, expected_revision, idempotency_key, actor):
    """All fields in an explicit review save succeed together or none do."""
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    state = state or {}
    request_stamp = stamp({"corrections": corrections, "reason": reason.strip(), "expected_revision": expected_revision})
    for event in state.get("events", []):
        if event.get("batch_idempotency_key") == idempotency_key:
            if event.get("batch_request_stamp") != request_stamp:
                raise HTTPException(409, "Idempotency key already used for a different review batch")
            return state, False
    keys = [row["input_key"] for row in corrections]
    if not 1 <= len(keys) <= 20 or len(keys) != len(set(keys)):
        raise HTTPException(422, "Review between one and twenty distinct calculation inputs")
    current = state
    for index, payload in enumerate(corrections):
        current, _ = change(current, review, **payload, reason=reason, expected_revision=expected_revision + index,
                            idempotency_key=idempotency_key if index == 0 else f"{idempotency_key}:{index}", actor=actor)
        current["events"][-1].update(batch_idempotency_key=idempotency_key, batch_request_stamp=request_stamp)
    return current, True


async def _persist_review(db, profile, actor, state, *, action, summary, event_prefix="financial-review"):
    from app.services import application_profiles as profiles
    from app.services import financial_cockpit
    from app.services.financial_publication import (
        invalidate_financial_review_caches,
        refresh_advisory_projections,
    )

    profile.financial_metric_review = state
    await profiles.log_profile_action(db, profile, actor, action, summary, metadata=state["events"][-1])
    await db.flush()
    # Invalidation is authoritative and must commit with the reviewed receipt.
    # A failed advisory recomputation must not roll back these stale markers.
    await invalidate_financial_review_caches(db, profile, correction_revision=state["revision"])
    try:
        async with db.begin_nested():
            await refresh_advisory_projections(db, profile, event_key=f"{event_prefix}:{state['events'][-1]['id']}")
    except Exception:  # noqa: BLE001 - the reviewed receipt, not a failed advisory refresh, is authoritative
        log.exception("Financial review projection refresh failed profile=%s revision=%s", profile.id, state["revision"])
        await db.refresh(profile)
    return await financial_cockpit.load_cockpit(db, profile)


async def save_review(db, profile, actor, *, composition=False, **payload):
    from app.services import financial_cockpit
    from app.services.financial_workspace import lock_financial_profile

    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    await lock_financial_profile(db, profile)
    profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).execution_options(populate_existing=True))).scalar_one()
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    if composition:
        from app.services.financial_metric_composition import change as compose

        state, changed = compose(getattr(profile, "financial_metric_review", None), cockpit["metric_review"], actor=actor, **payload)
    else:
        state, changed = change_many(getattr(profile, "financial_metric_review", None), cockpit["metric_review"], actor=actor, **payload)
    return await _persist_review(db, profile, actor, state, action="financial_metrics.composition" if composition else "financial_metrics.reviewed",
                                 summary="Saved a source-bound financial calculation review") if changed else cockpit


async def correct(db, profile, actor, **payload):
    from app.services import financial_cockpit
    from app.services.financial_workspace import lock_financial_profile

    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    await lock_financial_profile(db, profile)
    # A loaded ORM profile can predate waiting for the lock. Refresh its state
    # under the lock before reading revision or idempotency receipts.
    profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).execution_options(populate_existing=True))).scalar_one()
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    state, changed = change(getattr(profile, "financial_metric_review", None), cockpit["metric_review"], actor=actor, **payload)
    if changed:
        cockpit = await _persist_review(db, profile, actor, state,
            action="financial_metrics.corrected" if payload["action"] == "correct" else "financial_metrics.restored",
            summary="Reviewed a source-bound financial calculation input", event_prefix="financial-correction")
    return cockpit


async def save_earnings_sources(db, profile, actor, **payload):
    """The source receipt uses the same lock, audit and invalidation as math."""
    from app.services import financial_cockpit, financial_earnings_sources
    from app.services.financial_workspace import lock_financial_profile

    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    await lock_financial_profile(db, profile)
    profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).execution_options(populate_existing=True))).scalar_one()
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    state, changed = financial_earnings_sources.change(getattr(profile, "financial_metric_review", None),
        cockpit["ebitda"]["source_selection"], actor=actor, **payload)
    return await _persist_review(db, profile, actor, state, action="financial_metrics.earnings_sources",
        summary="Reviewed the EBITDA source period and exclusions", event_prefix="financial-earnings-sources") if changed else cockpit


async def save_noi_review(db, profile, actor, **payload):
    """Record an explicit annual NOI basis using the financial review lock."""
    from app.services import financial_cockpit, financial_noi_review
    from app.services.financial_workspace import lock_financial_profile

    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    await lock_financial_profile(db, profile)
    profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).execution_options(populate_existing=True))).scalar_one()
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    state, changed = financial_noi_review.change(getattr(profile, "financial_metric_review", None),
        cockpit["noi"]["review"], actor=actor, **payload)
    return await _persist_review(db, profile, actor, state, action="financial_metrics.noi_reviewed",
        summary="Reviewed an annual business NOI basis", event_prefix="financial-noi") if changed else cockpit
