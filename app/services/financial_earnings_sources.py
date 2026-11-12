"""Audited EBITDA source choice and separately labelled revenue illustrations.

The latest business period is always the baseline; earlier business returns
are comparison evidence only. Excluding or failing to reconcile the latest
period never substitutes an older, more favourable year. A forecast is never
an accepted fact.
"""

from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import UTC, date, datetime

from fastapi import HTTPException

from app.enums import Role
from app.services.financial_metric_review import number, stamp


def _classes(row):
    return {row.get("classification"), *(row.get("classifications") or [])}


def _identity(facts):
    return next((facts.get(key) for key in ("entity_name", "business_name", "entity", "taxpayer") if facts.get(key)), None)


def _file(candidate):
    return str(next((ref.get("file_id") for ref in candidate.get("source_refs", []) if ref.get("file_id")), ""))


def provenance_order(row):
    """Stable oldest-to-newest evidence order before period extraction/ties."""
    return (str(row.get("document_created_at") or ""), str(row.get("analyzed_at") or ""),
            int(row.get("document_version_number") or 0), int(row.get("analysis_version") or 0),
            str(row.get("file_id") or ""), str(row.get("analysis_id") or ""))


def _source_order(row):
    # Equal reporting periods need persisted provenance tie-breaks. Database
    # retrieval order is not a financial source-selection decision.
    provenance = provenance_order(row)
    try:
        end = date.fromisoformat(str(row.get("period_end")))
        return (end.year, end.month, end.day, row["kind"] == "profit_and_loss", *provenance)
    except (TypeError, ValueError):
        year = str(row.get("tax_year") or "")
        return (int(year) if len(year) == 4 and year.isdigit() else 0, 0, 0, False, *provenance)


def attach(ebitda, review, analyses, years, statement, bank, *, state=None, entity=None, now=None):
    """Apply a current source receipt after operand/recipe reviews, before plans."""
    from app.services.document_associations import identity_matches, source_excluded
    from app.services.financial_tax_earnings import normalize_tax_earnings, tax_source_is_business

    state = state or {}
    candidates = ebitda.get("candidates") or []
    by_file = {_file(row): row for row in candidates}
    tax_year_by_file = {str(row.get("file_id")): str(row.get("year")) for row in years}
    sources = []
    business_periods = []
    comparison_sources = {}
    for row in analyses:
        classes = _classes(row)
        is_tax = bool(classes.intersection({"tax_return", "personal_tax_return", "business_tax_return"}))
        if not is_tax and not classes.intersection({"current_p_and_l", "income_statement", "profit_and_loss"}):
            continue
        facts = row.get("key_facts") or {}
        business_tax = tax_source_is_business(facts) and "personal_tax_return" not in classes
        tax_period = normalize_tax_earnings(facts) if is_tax and business_tax else {}
        candidate = by_file.get(str(row.get("file_id")))
        identity = _identity(facts)
        scope_reason = (
            "Personal or unscoped tax return; never used for business EBITDA."
            if is_tax and not business_tax else
            "This document's business association requires review in Evidence."
            if source_excluded(row.get("identity_association")) else
            "The document does not establish this business's identity."
            if entity and not identity_matches(entity, identity, row.get("identity_association")) else None
        )
        reason = scope_reason or ("This source is not the current parsed earnings record for its period." if candidate is None else None)
        source_refs = candidate.get("source_refs", []) if candidate else []
        if scope_reason is None:
            comparison_sources[str(row.get("file_id"))] = row
        sources.append({
            "file_id": str(row.get("file_id")), "file_name": row.get("file_name"),
            "analysis_id": row.get("analysis_id"), "content_hash": row.get("content_hash"),
            "analyzed_at": row.get("analyzed_at"),
            "document_created_at": row.get("document_created_at"),
            "document_version_number": row.get("document_version_number"), "analysis_version": row.get("analysis_version"),
            "entity": identity, "period": candidate.get("period") if candidate else str(facts.get("tax_year") or facts.get("period_end") or "Unknown period"),
            "period_end": (candidate or {}).get("period_end") or next((ref.get("period_end") for ref in source_refs if ref.get("period_end")), None) or facts.get("period_end") or tax_period.get("period_end"),
            "tax_year": tax_year_by_file.get(str(row.get("file_id"))) or (str(tax_period["period_end"])[:4] if tax_period.get("period_end") else facts.get("tax_year")),
            "kind": "personal_tax_return" if is_tax and not business_tax else "tax_return" if is_tax else "profit_and_loss",
            "currency": candidate.get("currency") if candidate else facts.get("currency"),
            "eligible": reason is None, "reason": reason, "source_refs": source_refs,
            "ebitda_value": candidate.get("value") if candidate else None,
            "annualized_ebitda": candidate.get("annualized_value") if candidate else None,
            "missing_inputs": candidate.get("missing_inputs", []) if candidate else [],
            "source_stamp": stamp({"analysis": row.get("analysis_id"), "hash": row.get("content_hash"), "facts": facts,
                                   "association": row.get("identity_association"), "entity": entity}),
        })
        period_scope = (not is_tax or business_tax) and (
            scope_reason is None or not identity or not entity or identity_matches(entity, identity))
        if period_scope:
            # A matching or identity-unresolved business document still marks
            # the latest period when financial inputs or its association need
            # review. A known different business never displaces this one.
            business_periods.append(sources[-1])
    # Preserve every source and its flags in one deterministic catalog. The
    # reporting snapshot is bound by PPM/package freshness guards.
    sources.sort(key=lambda row: (row["file_id"], str(row.get("analysis_id") or "")))
    catalog_stamp = stamp([{key: row.get(key) for key in ("file_id", "source_stamp", "eligible")} for row in sources])
    history = [event for event in state.get("events", []) if event.get("source_metric") == "ebitda"]
    event = history[-1] if history else None
    stale = bool(event and event.get("source_stamp") != catalog_stamp)
    excluded = set(event.get("excluded_file_ids", [])) if event else set()
    mode = event.get("mode", "automatic") if event else "automatic"
    latest = max(business_periods, key=_source_order) if business_periods else None
    current_period = _source_order(latest)[:3] if latest else None
    for row in sources:
        row["current_period"] = bool(row in business_periods and _source_order(row)[:3] == current_period)
        row["baseline_eligible"] = bool(row["eligible"] and row["current_period"])
        row["comparative_only"] = bool(row in business_periods and not row["current_period"])
    preferred = event.get("selected_file_id") if event and mode == "manual" else None
    available = [row for row in sources if row["baseline_eligible"] and row["file_id"] not in excluded]
    selected = None
    manual_none = bool(event and mode == "manual" and preferred is None)
    if not manual_none and available:
        chosen = next((row for row in available if row["file_id"] == preferred), None) or max(available, key=_source_order)
        selected = chosen["file_id"]
    legacy_selection_replaced = bool(preferred and selected and preferred != selected)
    forecast_assumptions = (event.get("forecast_assumptions") if event and not stale and selected
                            and not legacy_selection_replaced else None)
    for row in sources:
        row["included"] = bool(row["baseline_eligible"] and row["file_id"] not in excluded)
        row["selected"] = bool(not stale and row["file_id"] == selected and row["included"])
        if row["eligible"] and row["file_id"] in excluded:
            row["reason"] = "Excluded from the EBITDA baseline by staff; retained for historical comparison."
        elif row["eligible"] and not row["selected"]:
            row["reason"] = ("Historical comparison only; the latest business reporting period supplies the baseline."
                             if row["comparative_only"] else "Alternative source for the latest reporting period; not added to the selected baseline.")
    selected_row = next((row for row in sources if row["selected"]), None)
    for candidate in candidates:
        candidate["selected_for_ebitda"] = bool(selected_row and _file(candidate) == selected_row["file_id"])
        candidate["excluded_from_ebitda"] = bool(_file(candidate) in excluded)
    ebitda["headline"] = by_file.get(selected_row["file_id"]) if selected_row else None
    headline = ebitda["headline"]
    if headline and headline.get("value") is None:
        ebitda["headline"] = None
        ebitda["reason"] = "The selected business period has missing earnings inputs. Review its source components; an older period is not substituted automatically."
    elif stale:
        ebitda["reason"] = "Financial source evidence changed. Review and save the EBITDA source selection again."
    elif not headline:
        ebitda["reason"] = ("The latest business reporting period is not selected, is excluded, or has no usable earnings record. Review that period; an older year is not substituted."
                            if latest else "No eligible business source is selected for EBITDA. Personal returns and bank deposits cannot supply business EBITDA.")
    else:
        ebitda["reason"] = None
    ebitda["source_selection"] = {
        "revision": state.get("revision", 0), "source_stamp": catalog_stamp,
        "mode": mode, "selected_file_id": selected, "excluded_file_ids": sorted(excluded),
        "stale": stale, "sources": sources, "history": history,
        "forecast_assumptions": forecast_assumptions,
        "baseline_policy": "latest_business_period", "latest_file_id": latest["file_id"] if latest else None,
        "latest_period_end": latest.get("period_end") if latest else None,
        "legacy_selection_replaced": legacy_selection_replaced,
    }
    comparative_statements = []
    for observed in (statement.get("statements") or [statement]) if statement else []:
        if (str(observed.get("file_id")) == str(statement.get("file_id"))
                and observed.get("period_start") == statement.get("period_start")
                and observed.get("period_end") == statement.get("period_end")):
            # Reviewed current operands, including an explicit unknown, take
            # precedence over the retained raw history for that exact period.
            observed = statement
        original = comparison_sources.get(str(observed.get("file_id")))
        if not original or entity and not identity_matches(entity, observed.get("business_name"), original.get("identity_association")):
            continue
        facts = original.get("key_facts") or {}
        comparative_statements.append({**observed, "file_name": original.get("file_name"),
            "comparison_ebitda_kind": "reported_ebitda" if number(facts.get("ebitda")) is not None else "computed_ebitda",
            "comparison_source_refs": [{"file_id": str(original.get("file_id")), "file_name": original.get("file_name"),
                "analysis_id": original.get("analysis_id"), "content_hash": original.get("content_hash"),
                "accepted": bool(original.get("accepted")), "entity": observed.get("business_name") or _identity(facts),
                "period_start": observed.get("period_start"), "period_end": observed.get("period_end"),
                "currency": observed.get("currency"), "basis": observed.get("basis")} ]})
    ebitda["revenue_trend"] = revenue_trend(years, statement, candidates, bank, now=now,
        assumptions=forecast_assumptions,
        selected_file_id=selected_row["file_id"] if selected_row else None,
        comparative_statements=comparative_statements)


def revenue_trend(years, statement, candidates, bank, *, now=None, assumptions=None, selected_file_id=None, comparative_statements=None):
    """Compare comparable full business periods, never deposits as revenue."""
    candidate_files = {_file(row): row for row in candidates}
    periods = []
    for row in years:
        candidate = candidate_files.get(str(row.get("file_id")), {})
        first, last = row.get("period_start"), row.get("period_end")
        try:
            start, end = date.fromisoformat(first), date.fromisoformat(last)
            complete = (start.day == 1 and start.month == end.month % 12 + 1 and end.day == monthrange(end.year, end.month)[1]
                        and (end.year - start.year) * 12 + end.month - start.month + 1 == 12)
        except (TypeError, ValueError):
            complete = False
        revenue = number(row.get("gross_receipts"))
        periods.append({
            "year": str(row.get("year") or ""), "period_start": first, "period_end": last,
            "months_covered": 12 if complete else None, "revenue": revenue,
            "average_monthly_revenue": round(revenue / 12, 2) if complete and revenue is not None else None,
            "ebitda": candidate.get("value"), "ebitda_kind": candidate.get("kind"),
            "currency": row.get("currency"), "basis": row.get("basis"),
            "file_id": str(row.get("file_id")), "file_name": next((ref.get("file_name") for ref in candidate.get("source_refs", [])), None),
            "source_refs": candidate.get("source_refs", []),
        })
    selected_kind = candidate_files.get(selected_file_id, {}).get("kind")
    prefer_pl = selected_kind in {"computed_ebitda", "reported_ebitda"}
    for observed in comparative_statements if comparative_statements is not None else [statement] if statement else []:
        candidate = candidate_files.get(str(observed.get("file_id")), {})
        first, last = observed.get("period_start"), observed.get("period_end")
        if candidate and (candidate.get("period_start"), candidate.get("period_end")) != (first, last):
            candidate = {}  # A multi-period file's current result is not prior-year earnings.
        try:
            start, end = date.fromisoformat(first), date.fromisoformat(last)
            complete = (start.day == 1 and start.month == end.month % 12 + 1 and end.day == monthrange(end.year, end.month)[1]
                        and (end.year - start.year) * 12 + end.month - start.month + 1 == 12 and observed.get("months_covered") == 12)
        except (TypeError, ValueError):
            complete = False
        if complete and not candidate.get("entity_mismatch"):
            revenue = number(observed.get("gross_revenue"))
            comparative_ebitda = candidate.get("value") if candidate else number(observed.get("ebitda"))
            comparative_kind = candidate.get("kind") if candidate else observed.get("comparison_ebitda_kind") if comparative_ebitda is not None else None
            pl_period = {"year": str(end.year), "period_start": first, "period_end": last, "months_covered": 12,
                         "revenue": revenue, "average_monthly_revenue": round(revenue / 12, 2) if revenue is not None else None,
                         "ebitda": comparative_ebitda, "ebitda_kind": comparative_kind,
                         "currency": observed.get("currency"), "basis": observed.get("basis"),
                         "file_id": str(observed.get("file_id")), "file_name": next((ref.get("file_name") for ref in candidate.get("source_refs", [])), observed.get("file_name")),
                         "source_refs": candidate.get("source_refs") or observed.get("comparison_source_refs", [])}
            duplicate = next((row for row in periods if row["period_start"] == first and row["period_end"] == last), None)
            if duplicate is None:
                periods.append(pl_period)
            elif pl_period["file_id"] == selected_file_id or prefer_pl and not duplicate.get("is_pl"):
                periods[periods.index(duplicate)] = pl_period
            pl_period["is_pl"] = True
    periods.sort(key=lambda row: row.get("period_end") or row["year"])
    full = [row for row in periods if row["months_covered"] == 12 and row["revenue"] is not None]
    comparison = None
    forecast = {"status": "unavailable", "method": None, "target_year": None, "projected_revenue": None,
                "change_amount": None, "change_pct": None, "projected_ebitda": None, "ebitda_kind": None,
                "margin": None, "assumptions": [], "source_refs": [],
                "reason": "A current, dated P&L or explicitly reviewed revenue assumptions are needed for a current-year outlook. Historical growth alone does not establish future revenue."}
    if len(full) >= 2:
        prior, current = full[-2:]
        comparable = bool(prior["currency"] and prior["currency"] == current["currency"] and prior["basis"] and prior["basis"] == current["basis"]
                          and prior["period_start"][5:] == current["period_start"][5:]
                          and prior["period_end"][5:] == current["period_end"][5:]
                          and int(current["year"]) - int(prior["year"]) == 1)
        delta = round(current["revenue"] - prior["revenue"], 2) if comparable else None
        pct = round(delta / prior["revenue"] * 100, 2) if comparable and prior["revenue"] > 0 else None
        comparison = {"prior_year": prior["year"], "current_year": current["year"], "prior_revenue": prior["revenue"], "current_revenue": current["revenue"],
                      "change_amount": delta, "change_pct": pct, "monthly_change": round(delta / 12, 2) if delta is not None else None,
                      "direction": ("up" if delta > 0 else "down" if delta < 0 else "neutral") if delta is not None else None,
                      "comparable": comparable, "reason": None if comparable else "Match the currency, accounting basis and consecutive full reporting periods before comparing growth."}
    historical_baseline = full[-1] if full else None
    selected_baseline = next((row for row in full if row["file_id"] == selected_file_id), None)
    baseline = selected_baseline or historical_baseline
    assumption_baseline = selected_baseline
    today = now.date() if isinstance(now, datetime) else date.today()
    if statement:
        months = number(statement.get("months_covered"))
        revenue = number(statement.get("gross_revenue"))
        current_earnings = candidate_files.get(str(statement.get("file_id")), {})
        try:
            start, end = date.fromisoformat(statement.get("period_start")), date.fromisoformat(statement.get("period_end"))
            dated = (start.year == end.year == today.year and start.month == 1 and start.day == 1 and end <= today
                     and end.day == monthrange(end.year, end.month)[1] and months == end.month)
        except (TypeError, ValueError):
            dated = False
        comparable = bool(statement.get("currency") and statement.get("basis") and
                          (not baseline or statement.get("currency") == baseline["currency"] and statement.get("basis") == baseline["basis"]))
        if (dated and comparable and current_earnings and str(statement.get("file_id")) == selected_file_id
                and not current_earnings.get("entity_mismatch") and months is not None and 3 <= months < 12 and revenue is not None):
            projected = round(revenue * 12 / months, 2)
            forecast.update(status="illustration", method="p_and_l_run_rate", target_year=today.year,
                            projected_revenue=projected, change_amount=round(projected - baseline["revenue"], 2) if baseline else None,
                            change_pct=round((projected - baseline["revenue"]) / baseline["revenue"] * 100, 2) if baseline and baseline["revenue"] > 0 else None,
                            source_refs=candidate_files.get(str(statement.get("file_id")), {}).get("source_refs", []), reason=None,
                            assumptions=["Illustration only: current P&L revenue per reported month continues for twelve months; no seasonal adjustment is assumed."])
            if current_earnings.get("value") is not None and str(statement.get("file_id")) == selected_file_id:
                forecast.update(projected_ebitda=round(current_earnings["value"] * 12 / months, 2),
                                ebitda_kind=current_earnings.get("kind"), margin=round(current_earnings["value"] / revenue, 6) if revenue > 0 else None)
                forecast["assumptions"].append("Annualize earnings from the same current P&L period; retain its reconciliation and missing-component limitations.")
            if str(statement.get("file_id")) == selected_file_id:
                assumption_baseline = {"file_id": selected_file_id, "year": str(today.year), "revenue": projected,
                    "currency": statement.get("currency"), "basis": statement.get("basis"), "period_label": statement.get("period_label"),
                    "ebitda": forecast["projected_ebitda"], "ebitda_kind": current_earnings.get("kind"), "source_refs": current_earnings.get("source_refs", [])}
    if assumptions and assumption_baseline and assumption_baseline.get("currency") and assumption_baseline.get("basis"):
        baseline = assumption_baseline
        growth = assumptions["revenue_growth_pct"]
        projected = round(baseline["revenue"] * (1 + growth / 100), 2)
        forecast.update(status="illustration", method="reviewed_assumptions", target_year=today.year if baseline.get("period_label") else int(baseline["year"]) + 1,
                        projected_revenue=projected, change_amount=round(projected - baseline["revenue"], 2), change_pct=growth,
                        projected_ebitda=None, ebitda_kind=None, margin=None,
                        source_refs=baseline["source_refs"], reason=None,
                        assumptions=[f"Staff-reviewed illustration: revenue changes by {growth:g}% from the selected business source ({baseline.get('period_label') or baseline['year']}; {baseline['revenue']:,.2f} annual revenue basis). This is an assumption, not observed revenue or accepted earnings for qualification."])
        if baseline.get("period_label"):
            forecast["assumptions"].append("The selected partial P&L is first annualized at its reported monthly run-rate, without a seasonal adjustment.")
    explicit_margin = assumptions.get("ebitda_margin_pct") if assumptions else None
    inferred_margin = bool(baseline and baseline["file_id"] == selected_file_id and baseline["ebitda"] is not None)
    if forecast["status"] == "illustration" and forecast["projected_ebitda"] is None and baseline and baseline["revenue"] > 0 and (inferred_margin or explicit_margin is not None):
        # A provisional tax estimate remains a tax estimate in projection.
        margin = explicit_margin / 100 if explicit_margin is not None else baseline["ebitda"] / baseline["revenue"]
        forecast.update(margin=round(margin, 6), projected_ebitda=round(forecast["projected_revenue"] * margin, 2), ebitda_kind=baseline["ebitda_kind"])
        forecast["source_refs"] = list({stamp(ref): ref for ref in [*forecast["source_refs"], *baseline["source_refs"]]}.values())
        forecast["assumptions"].append("Use the explicitly reviewed earnings margin." if explicit_margin is not None else "Hold the selected annual source earnings margin constant, including a negative margin. No expense efficiencies or loan principal/interest are added to EBITDA.")
        if explicit_margin is not None:
            forecast["ebitda_kind"] = "reviewed_assumption"
    return {"status": "available" if comparison and comparison["comparable"] else "insufficient", "direction": comparison.get("direction") if comparison else None, "periods": periods,
            "comparison": comparison, "forecast": forecast,
            "bank_context": {"average_monthly_deposits": bank.get("avg_deposits"), "annualized_deposits": bank.get("annualized_deposits"),
                             "note": "Deposits are a cash-collection signal, not revenue. Transfers, financing proceeds and owner contributions must be reconciled before bank activity can support a revenue forecast."},
            "notes": ["The selected EBITDA baseline is one business and reporting period. Older business returns remain comparison evidence even when excluded from that baseline.",
                      "Revenue illustrations do not replace historical EBITDA, DSCR or program qualification figures."]}


def align_coverage(ebitda, dscr, review=None):
    """Use the selected latest business period for factual coverage as well.

    Other business periods and the bank cash proxy remain available as named
    comparisons, outside the active candidates consumed by refinance plans.
    An unavailable latest period must not quietly revive an older numerator.
    """
    selection = ebitda.get("source_selection") or {}
    if not selection:
        return
    selected_id = selection.get("selected_file_id") if not selection.get("stale") else None
    selected = next((row for row in selection.get("sources", []) if row.get("file_id") == selected_id), None)
    has_business_period = bool(selection.get("latest_file_id"))
    active, comparative, seen = [], [], set()
    for candidate in [*dscr.get("candidates", []), *dscr.get("comparative_candidates", [])]:
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        is_bank = candidate.get("key") == "bank_cash_flow"
        source_match = bool(selected_id and (
            str(candidate.get("source_file_id")) == selected_id or any(
                str(ref.get("file_id")) == selected_id for ref in candidate.get("source_refs", []))))
        if source_match and selected and selected.get("kind") == "tax_return" and selected.get("tax_year"):
            source_match = str(candidate.get("period") or "")[:4] == str(selected["tax_year"])
        current = bool((not is_bank and source_match) or (is_bank and not has_business_period))
        candidate.update(baseline_eligible=current, comparative_only=not current)
        if current:
            if is_bank:
                candidate["label"] = "Bank cash flow · provisional proxy"
                proxy_note = "No current business earnings baseline is available. This bank cash-flow proxy is not EBITDA."
                if proxy_note not in (candidate.get("note") or ""):
                    candidate["note"] = " ".join(filter(None, [candidate.get("note"), proxy_note]))
            active.append(candidate)
        else:
            comparative.append(candidate)
    dscr["candidates"] = active
    dscr["comparative_candidates"] = comparative
    dscr["bank_proxy"] = next((row for row in [*active, *comparative] if row.get("key") == "bank_cash_flow"), None)
    dscr["headline"] = next((row for row in active if row.get("value") is not None), None)
    dscr["pro_forma_headline"] = next((row for row in active if row.get("pro_forma_value") is not None), None)
    dscr["baseline_policy"] = "latest_business_period"
    dscr["selected_file_id"] = selected_id
    if dscr["headline"]:
        dscr["reason"] = None
    elif has_business_period and not active:
        dscr["reason"] = ebitda.get("reason") or "The latest business reporting period has no usable coverage numerator. Review that period; prior years and bank cash flow do not replace it."
    if review:
        for calculation in review.get("calculations", []):
            if str(calculation.get("key", "")).startswith("dscr."):
                calculation["selected"] = bool(dscr["headline"] and calculation["key"] == f"dscr.{dscr['headline']['key']}")
                calculation["baseline_eligible"] = any(calculation["key"] == f"dscr.{row['key']}" for row in active)
        for item in review.get("inputs", []):
            item["used_by_headline"] = [row["key"] for row in review.get("calculations", [])
                                       if row.get("selected") and item["key"] in row.get("input_keys", [])]


def change(state, selection, *, mode, selected_file_id, excluded_file_ids, reason, expected_revision, source_stamp, idempotency_key, actor, forecast_assumptions=None, now=None):
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    state = state or {}
    request = {"mode": mode, "selected_file_id": str(selected_file_id) if selected_file_id else None,
               "excluded_file_ids": sorted(str(value) for value in excluded_file_ids), "reason": reason.strip(),
               "expected_revision": expected_revision, "source_stamp": source_stamp, "forecast_assumptions": forecast_assumptions}
    request_stamp = stamp(request)
    for event in state.get("events", []):
        if event.get("idempotency_key") == idempotency_key:
            if event.get("request_stamp") != request_stamp:
                raise HTTPException(409, "Idempotency key already used for another review")
            return state, False
    if state.get("revision", 0) != expected_revision:
        raise HTTPException(409, "Financial reviews changed; reload before saving")
    if selection["source_stamp"] != source_stamp:
        raise HTTPException(409, "Financial source evidence changed; review the current documents")
    if mode not in {"automatic", "manual"} or not reason.strip() or len(reason) > 2000:
        raise HTTPException(422, "A valid source selection and review reason are required")
    if forecast_assumptions is not None:
        growth = number(forecast_assumptions.get("revenue_growth_pct"))
        margin = number(forecast_assumptions.get("ebitda_margin_pct"))
        if growth is None or not -100 <= growth <= 1000 or (forecast_assumptions.get("ebitda_margin_pct") is not None and (margin is None or not -1000 <= margin <= 100)):
            raise HTTPException(422, "Use a finite revenue growth assumption from -100% to 1000% and a margin from -1000% to 100%")
    excluded = request["excluded_file_ids"]
    source_ids = {row["file_id"] for row in selection["sources"]}
    if len(excluded) != len(set(excluded)) or set(excluded) - source_ids:
        raise HTTPException(422, "Review each current source once")
    selected = request["selected_file_id"]
    if mode == "manual" and selected is not None:
        target = next((row for row in selection["sources"] if row["file_id"] == selected), None)
        if not target or not target["eligible"] or selected in excluded:
            raise HTTPException(422, "Select an eligible business source; personal or excluded evidence cannot supply EBITDA")
        if not target.get("baseline_eligible", False):
            raise HTTPException(422, "Use the latest business reporting period as the earnings baseline. Older periods are historical comparison only.")
    elif mode == "automatic" and selected is not None:
        raise HTTPException(422, "Automatic selection does not accept a manual source")
    event = {"id": str(uuid.uuid4()), "source_metric": "ebitda", **request, "revision": expected_revision + 1,
             "actor_id": str(actor.id), "actor_name": getattr(actor, "full_name", None) or getattr(actor, "name", None),
             "at": (now or datetime.now(UTC)).isoformat(), "idempotency_key": idempotency_key, "request_stamp": request_stamp}
    return {"revision": expected_revision + 1, "events": [*state.get("events", []), event]}, True
