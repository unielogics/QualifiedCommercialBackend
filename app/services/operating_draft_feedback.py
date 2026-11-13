"""Read-only operating arithmetic, separate from qualification and staff review."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.earnings_improvement import (
    action_missing,
    expense_missing,
    source_duplicate_missing,
    validate_body,
)
from app.services import earnings_improvement as engine
from app.services.financial_metric_review import number
from app.services.operating_metric_effects import metric_value
from app.services.operating_plan_commands import participants, resolve
from app.services.operating_plan_guidance import reconcile_groups

BASIS = "before_implementation_costs_and_timing"
CENT = Decimal(".01")
WORKING_FIELDS = ("basis", "noi", "noi_delta", "goal_gap", "earnings", "earnings_delta", "cash_delta_annual", "cash_delta_monthly",
                  "dscr_current_debt_before", "dscr_current_debt_after", "dscr_current_debt_delta", "first_year_noi", "first_year_noi_delta",
                  "first_year_earnings", "first_year_earnings_delta", "first_year_cash_delta", "first_year_dscr_current_debt_after",
                  "first_year_dscr_current_debt_delta", "loan_cash_change_monthly", "loan_cash_change_annual",
                  "combined_cash_change_monthly", "combined_cash_change_annual", "cash_after_debt_basis", "cash_after_debt_mode",
                  "cash_after_debt_before_annual", "cash_after_debt_after_annual", "cash_after_debt_delta_annual",
                  "cash_after_debt_before_monthly", "cash_after_debt_after_monthly", "cash_after_debt_delta_monthly",
                  "dscr_with_refinance_after", "dscr_with_refinance_delta", "refinance_annual_debt_service", "refinance_coverage_basis")


def _d(value):
    value = number(value)
    return Decimal(str(value)) if value is not None else None


def _money(value):
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def _ratio(value):
    return float(value.quantize(Decimal(".0001"), rounding=ROUND_HALF_UP)) if value is not None else None


def _unique(values):
    return list(dict.fromkeys(values))


def _source_issue(message):
    value = message.casefold()
    return any(word in value for word in ("source", "business association", "reporting period", "currency"))


def _source_checks(action, body, cockpit, resolved, files, groups, *, reviewed_manual=False):
    """Only affected current rows/groups, never unrelated disclosure blanks."""
    rows = participants(action, body)
    if action["kind"] == "unit_volume":
        rows = [row for row in body["expenses"] if not row["excluded"] and row["category"] not in engine.NON_OPERATING | engine.NON_CASH
                and row["cadence"] != "one_time"]
    issues, fields = [], []
    if any(str(fid) not in files for fid in action["source_file_ids"]):
        issues.append("A linked supporting source is unavailable to this preview.")
        fields.append(f"adjustments.{action['id']}.source_file_ids")
    if action["kind"] in {"expense_reduction", "expense_increase"} and not rows:
        issues.append("Link this cost change to its current expense before comparing it with source NOI.")
        fields.append(f"adjustments.{action['id']}.expense_row_id")
    references = {row["key"]: row for row in ((cockpit.get("operating_evidence") or {}).get("guidance") or {}).get("expense_groups", [])}
    duplicate_ids = {path.split(".")[1] for path in source_duplicate_missing(body)}
    ids = {row["id"] for row in rows}
    for split in resolved["itemization"]:
        if ids & set(split["child_expense_row_ids"]) and (split["status"] != "balanced" or not split["source_comparable"]):
            issues.append("Reconcile the affected source split before adding its cost changes to source NOI.")
            fields.append(f"expenses.{split['parent_expense_row_id']}.itemization.{split['status']}")
    for row in rows:
        key = row["source_group_key"]
        support = {*action["source_file_ids"], *row["source_file_ids"]}
        manual_support = bool(support and all(files.get(str(fid), {}).get("eligible_for_review") for fid in support))
        if not key and reviewed_manual and manual_support and not row["excluded"] and row["id"] not in duplicate_ids:
            # The existing fresh staff review binds this exact current row and
            # source context. A populated changed source group never uses this.
            continue
        reference = references.get(key) or {}
        scope = all(reference.get(field) == body.get(field) for field in ("period_start", "period_end", "currency", "basis"))
        accepted = bool(reference.get("source_file_ids") and all(files.get(str(fid), {}).get("eligible_for_review") for fid in reference["source_file_ids"]))
        if row["excluded"] or row["id"] in duplicate_ids or not key or not scope or reference.get("category") != row["category"] or not accepted or groups.get(key, {}).get("status") != "matches_source":
            issues.append("Confirm the affected current cost against its same-business, same-period source group before showing a source NOI change.")
            fields.append(f"expenses.{row['id']}.source_group_key")
    return _unique(issues), _unique(fields)


def evaluate(raw, cockpit, evaluation, *, source_file_ids=None, now=None):
    """All amounts come from the same resolver/_impact as qualified results."""
    original = validate_body(raw)
    resolved = resolve(original, cockpit)
    body = resolved["body"]
    expenses = {row["id"]: row for row in body["expenses"]}
    active = [row for row in body["adjustments"] if not row["excluded"] and row["status"] != "rejected"]
    intents = {row["id"]: row for row in original["adjustments"]}
    commands = {row["action_id"]: row for row in resolved["commands"]}
    results = {row["id"]: row for row in evaluation.get("actions", [])}
    files, allowed = engine._scope_files(cockpit, source_file_ids)
    conflicts = engine._conflicts(active, expenses, body)
    groups = {row["key"]: row for row in reconcile_groups(original, cockpit.get("operating_evidence") or {})}
    now = now or datetime.now(UTC)
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime.combine(now, datetime.min.time(), UTC)
    rows = []
    for action in active:
        identifier = action["id"]
        original_action = intents[identifier]
        missing = action_missing(action)
        qualification = [f"adjustments.{identifier}.{key}" for key in missing if key in engine.QUALIFICATION_FIELDS]
        math_missing = [f"adjustments.{identifier}.{key}" for key in missing if key not in engine.QUALIFICATION_FIELDS]
        if action["kind"] in {"expense_reduction", "expense_increase", "expense_group_change"}:
            for affected in participants(original_action, original):
                math_missing.extend(f"expenses.{affected['id']}.{key}" for key in expense_missing(affected) if key != "label")
                if affected["cadence"] not in engine.FACTORS or affected["cadence"] == "one_time":
                    math_missing.append(f"expenses.{affected['id']}.cadence")
        impact = engine._impact(action, body, expenses, cockpit, allowed, now, commands.get(identifier), arithmetic_only=True)
        math_missing.extend(((impact.get("calculation") or {}).get("unit_economics") or {}).get("missing") or [])
        old = results.get(identifier) or {}
        metadata_messages = {f"Missing {key.replace('_', ' ')}" for key in missing if key in engine.QUALIFICATION_FIELDS}
        reviewed_manual = bool(old.get("included_in_approved_projection") is True and old.get("review_state") == "approved"
            and original_action["kind"] in {"expense_reduction", "expense_increase"} and original_action.get("expense_row_id")
            and original_action.get("baseline_treatment") in {"included_in_baseline", "incremental_to_baseline"}
            and original_action.get("baseline_reconciliation_note"))
        source_issues, fix_fields = _source_checks(original_action, original, cockpit, resolved, files, groups, reviewed_manual=reviewed_manual)
        prior_issues = [issue for issue in old.get("issues", []) if not issue.startswith("Missing ") and issue not in conflicts.get(identifier, [])]
        source_issues.extend(issue for issue in prior_issues if _source_issue(issue))
        # Existing source/overlap checks still bind the preview. New arithmetic
        # errors are not disguised as missing explanatory prose.
        for issue in [*impact["issues"], *((commands.get(identifier) or {}).get("issues") or []), *prior_issues]:
            if issue.startswith("Missing ") or issue in metadata_messages:
                continue
            if _source_issue(issue):
                source_issues.append(issue)
            elif not (issue.startswith("Link this expense adjustment") and not action.get("expense_row_id")):
                math_missing.append(issue)
        linked = expenses.get(action.get("expense_row_id")) or {}
        outside = linked.get("category") in engine.NON_OPERATING
        gross = _d(impact.get("accounting_gross_annual_impact", impact.get("gross_annual_impact")))
        one_time = _d(impact.get("gross_one_time_effect"))
        if gross is None:
            math_missing.append(f"adjustments.{identifier}.current_proposed_math")
        conflict = conflicts.get(identifier) or []
        status = "outside_operating" if outside else "conflict" if conflict else "incomplete" if math_missing else "calculated"
        safe_math = status == "calculated"
        net = _d(impact.get("net_annual_impact")) if safe_math else None
        explicit_window = bool(body.get("projection_start") and action.get("effective_date") and action.get("implementation_cost") is not None)
        first_year = _d(impact.get("first_year_impact")) if safe_math and explicit_window else None
        if not body.get("projection_start"):
            qualification.append("projection_start")
        metrics = {"effect_type": impact.get("effect_type", "operating_cash"),
                   "metric_issues": deepcopy(impact.get("metric_issues") or {"noi": [], "earnings": [], "cash": []})}
        for metric in ("noi", "earnings", "cash"):
            for period, field in (("gross_annual", "gross_annual_delta"), ("net_annual", "net_annual_delta"), ("first_year", "first_year_delta")):
                value = _d(metric_value(impact, metric, period)) if safe_math and (period != "first_year" or explicit_window) else None
                metrics[metric + "_" + field] = _money(value)
        rows.append({"action_id": identifier, "expense_row_id": action.get("expense_row_id"), "kind": action["kind"],
            "math_status": status, "gross_annual_delta": _money(gross) if safe_math else None,
            "gross_monthly_run_rate_delta": _money(gross / 12) if safe_math and gross is not None else None,
            "gross_one_time_delta": _money(one_time) if safe_math else None,
            "net_annual_delta": _money(net), "first_year_delta": _money(first_year),
            "math_missing": _unique([*math_missing, *conflict]), "qualification_missing": _unique(qualification),
            "source_issues": _unique(source_issues), "fix_fields": fix_fields, **metrics})
    complete = all(row["math_status"] == "calculated" for row in rows)
    known = [_d(row["gross_annual_delta"]) for row in rows if row["math_status"] == "calculated"]
    subtotal = sum(known, Decimal(0)) if known or not rows else None
    gross = subtotal if complete else None
    def metric_sum(metric, field):
        values = [_d(row[metric + "_" + field]) for row in rows]
        return sum(values, Decimal(0)) if complete and all(value is not None for value in values) else None

    noi_gross = metric_sum("noi", "gross_annual_delta")
    earnings_gross = metric_sum("earnings", "gross_annual_delta")
    cash_gross = metric_sum("cash", "gross_annual_delta")
    earnings_ok, noi_ok, _ = engine.comparison_scope(body, cockpit)
    baseline = evaluation.get("baseline") or {}
    current_noi, earnings, debt = _d(baseline.get("noi")), _d(baseline.get("earnings")), _d(baseline.get("annual_debt_service"))
    source_issues = _unique(issue for row in rows for issue in row["source_issues"])
    source_ok = not source_issues
    draft_noi = current_noi + noi_gross if complete and source_ok and noi_ok and current_noi is not None and noi_gross is not None else None
    goal = cockpit.get("_operating_goal") or {}
    target = _d(goal.get("amount")) if goal.get("status") == "set" and goal.get("currency") == baseline.get("noi_currency") else None
    debt_currency = ((cockpit.get("debt") or {}).get("service") or {}).get("currency")
    debt_currency_ok = debt_currency == baseline.get("currency") == body["currency"] == "USD"
    before_ratio = earnings / debt if earnings_ok and debt_currency_ok and earnings is not None and debt is not None and debt > 0 else None
    after_ratio = (earnings + earnings_gross) / debt if complete and source_ok and before_ratio is not None and earnings_gross is not None else None
    qualified = (evaluation.get("projection") or {}).get("proposed") or {}
    qualified_noi = _d(qualified.get("noi"))
    explicit_windows = bool(body.get("projection_start") and all(row.get("effective_date") and row.get("implementation_cost") is not None for row in active))
    qualified_first = _d(qualified.get("first_year_noi")) if explicit_windows else None
    qualified_earnings = _d(qualified.get("earnings"))
    qualified_first_earnings = _d(qualified.get("first_year_earnings")) if explicit_windows else None
    qualified_coverage = _d(qualified.get("first_year_coverage_earnings")) if explicit_windows else None
    qualified_ratio = qualified_earnings / debt if complete and source_ok and before_ratio is not None and qualified_earnings is not None else None
    qualified_first_ratio = qualified_coverage / debt if complete and source_ok and before_ratio is not None and qualified_coverage is not None else None
    draft_earnings = earnings + earnings_gross if complete and source_ok and earnings_ok and earnings is not None and earnings_gross is not None else None
    earnings_safe = complete and source_ok and earnings_ok and earnings is not None
    setup_known = all(action.get("implementation_cost") is not None for action in active)
    working_period = "net_annual_delta" if setup_known else "gross_annual_delta"
    working_noi_delta = metric_sum("noi", working_period) if source_ok and noi_ok and current_noi is not None else None
    working_earnings_delta = metric_sum("earnings", working_period) if source_ok and earnings_ok and earnings is not None else None
    working_cash_delta = metric_sum("cash", working_period) if source_ok and (earnings_ok or noi_ok) else None
    working_noi = current_noi + working_noi_delta if working_noi_delta is not None else None
    working_earnings = earnings + working_earnings_delta if working_earnings_delta is not None else None
    working_ratio = working_earnings / debt if before_ratio is not None and working_earnings is not None else None
    first_noi_delta = metric_sum("noi", "first_year_delta") if source_ok and noi_ok and current_noi is not None else None
    first_earnings_delta = metric_sum("earnings", "first_year_delta") if source_ok and earnings_ok and earnings is not None else None
    first_cash_delta = metric_sum("cash", "first_year_delta") if source_ok and (earnings_ok or noi_ok) else None
    coverage_rows = [row for row in rows if intents[row["action_id"]]["kind"] != "one_time_adjustment"
                     and intents[row["action_id"]]["cadence"] != "one_time"]
    coverage_values = [_d(row["earnings_first_year_delta"]) for row in coverage_rows]
    first_coverage_delta = sum(coverage_values, Decimal(0)) if first_earnings_delta is not None and all(value is not None for value in coverage_values) else None
    first_working_ratio = (earnings + first_coverage_delta) / debt if before_ratio is not None and first_coverage_delta is not None else None
    cash_bridge = evaluation.get("cash_bridge") or {}
    loan_cash = _d(cash_bridge.get("net_loan_cash_change_monthly")) if body["currency"] == cash_bridge.get("currency") == "USD" else None
    working_combined = loan_cash + working_cash_delta / 12 if loan_cash is not None and working_cash_delta is not None else None
    cash_basis_known = baseline.get("earnings_kind") in {"computed_ebitda", "reported_ebitda", "tax_estimate"}
    cash_before = earnings - debt if cash_basis_known and earnings_ok and debt_currency_ok and earnings is not None and debt is not None and debt >= 0 else None
    saved_loan = bool((cockpit.get("plan") or {}).get("record", {}).get("updated_at") and (cockpit.get("plan") or {}).get("evaluation", {}).get("active"))
    cash_loan_delta = loan_cash * 12 if loan_cash is not None else None if saved_loan else Decimal(0)
    cash_level_delta = working_cash_delta + cash_loan_delta if working_cash_delta is not None and cash_loan_delta is not None and cash_before is not None else None
    cash_after = cash_before + cash_level_delta if cash_level_delta is not None else None
    loan_evaluation = (cockpit.get("plan") or {}).get("evaluation") or {}
    refinance_debt = _d(loan_evaluation.get("debt_service_after_annual"))
    loan_operating = _d(loan_evaluation.get("annual_new_operating_expenses"))
    refinance_ready = bool(saved_loan and cash_bridge.get("available") and cash_bridge.get("loan_status") == "calculated"
                           and not loan_evaluation.get("coverage_unavailable_reason") and debt_currency_ok
                           and loan_operating is not None and loan_operating >= 0
                           and refinance_debt is not None and refinance_debt >= 0)
    # Coverage uses the canonical annual stress/coverage window, not twelve
    # regular payments. Financing fees already in that denominator stay there;
    # only separately classified operating charges reduce earnings once.
    refinance_ratio = ((working_earnings - loan_operating) / refinance_debt
                       if refinance_ready and working_earnings is not None and refinance_debt > 0 else None)
    working = {"basis": "after_setup_before_timing" if setup_known else BASIS,
        "noi": _money(working_noi), "noi_delta": _money(working_noi_delta),
        "goal_gap": _money(max(Decimal(0), target - working_noi)) if target is not None and working_noi is not None else None,
        "earnings": _money(working_earnings), "earnings_delta": _money(working_earnings_delta),
        "cash_delta_annual": _money(working_cash_delta), "cash_delta_monthly": _money(working_cash_delta / 12) if working_cash_delta is not None else None,
        "dscr_current_debt_before": _ratio(before_ratio), "dscr_current_debt_after": _ratio(working_ratio),
        "dscr_current_debt_delta": _ratio(working_ratio - before_ratio) if working_ratio is not None else None,
        "first_year_noi": _money(current_noi + first_noi_delta) if first_noi_delta is not None else None, "first_year_noi_delta": _money(first_noi_delta),
        "first_year_earnings": _money(earnings + first_earnings_delta) if first_earnings_delta is not None else None,
        "first_year_earnings_delta": _money(first_earnings_delta), "first_year_cash_delta": _money(first_cash_delta),
        "first_year_dscr_current_debt_after": _ratio(first_working_ratio),
        "first_year_dscr_current_debt_delta": _ratio(first_working_ratio - before_ratio) if first_working_ratio is not None else None,
        "loan_cash_change_monthly": _money(loan_cash), "loan_cash_change_annual": _money(loan_cash * 12) if loan_cash is not None else None,
        "combined_cash_change_monthly": _money(working_combined), "combined_cash_change_annual": _money(working_combined * 12) if working_combined is not None else None,
        "cash_after_debt_basis": "earnings_less_debt_service_illustration",
        "cash_after_debt_mode": "unavailable" if cash_after is None else "saved_refinance_and_operations" if saved_loan else "current_debt_operations_only",
        "cash_after_debt_before_annual": _money(cash_before), "cash_after_debt_after_annual": _money(cash_after),
        "cash_after_debt_delta_annual": _money(cash_level_delta),
        "cash_after_debt_before_monthly": _money(cash_before / 12) if cash_before is not None else None,
        "cash_after_debt_after_monthly": _money(cash_after / 12) if cash_after is not None else None,
        "cash_after_debt_delta_monthly": _money(cash_level_delta / 12) if cash_level_delta is not None else None}
    working.update(dscr_with_refinance_after=_ratio(refinance_ratio),
                   dscr_with_refinance_delta=_ratio(refinance_ratio - before_ratio) if refinance_ratio is not None and before_ratio is not None else None,
                   refinance_annual_debt_service=_money(refinance_debt) if refinance_ready else None,
                   refinance_coverage_basis="saved_coverage_window_after_new_operating_charges")
    missing = _unique(path for row in rows for path in [*row["math_missing"], *row["fix_fields"]])
    if not noi_ok or current_noi is None:
        missing.append("noi_source_comparability")
    if not earnings_ok or earnings is None:
        missing.append("earnings_source_comparability")
    if debt is None or debt < 0:
        missing.append("annual_debt_service")
    if not debt_currency_ok:
        missing.append("debt_service_currency")
    metric_issues = {metric: _unique(issue for row in rows for issue in row["metric_issues"][metric]) for metric in ("noi", "earnings", "cash")}
    status = "no_changes" if not rows else "incomplete" if not complete else "source_blocked" if not source_ok or not noi_ok or current_noi is None or noi_gross is None else "calculated"
    return {"basis": BASIS, "actions": rows, "summary": {"status": status, "complete": complete,
        "currency": body["currency"] or None, "noi_currency": baseline.get("noi_currency"),
        "current_earnings": _money(earnings), "earnings_currency": baseline.get("currency"), "earnings_kind": baseline.get("earnings_kind"),
        "draft_earnings": _money(draft_earnings), "draft_earnings_delta": _money(earnings_gross) if draft_earnings is not None else None,
        "qualified_earnings": _money(qualified_earnings) if earnings_safe else None,
        "qualified_earnings_delta": _money(qualified_earnings - earnings) if earnings_safe and qualified_earnings is not None else None,
        "qualified_first_year_earnings": _money(qualified_first_earnings) if earnings_safe else None,
        "qualified_first_year_earnings_delta": _money(qualified_first_earnings - earnings) if earnings_safe and qualified_first_earnings is not None else None,
        "source_comparable": bool(source_ok and noi_ok and current_noi is not None and noi_gross is not None), "current_noi": _money(current_noi),
        "metric_issues": metric_issues, "working": working,
        "known_gross_annual_subtotal": _money(subtotal), "gross_annual_delta": _money(gross),
        "gross_monthly_run_rate_delta": _money(gross / 12) if gross is not None else None,
        "draft_noi": _money(draft_noi), "draft_noi_delta": _money(noi_gross) if draft_noi is not None else None,
        "gross_earnings_annual_delta": _money(earnings_gross) if source_ok and earnings_ok else None,
        "gross_cash_annual_delta": _money(cash_gross) if source_ok and (earnings_ok or noi_ok) else None,
        "net_cash_annual_delta": _money(metric_sum("cash", "net_annual_delta")) if source_ok and (earnings_ok or noi_ok) else None,
        "first_year_cash_delta": _money(metric_sum("cash", "first_year_delta")) if source_ok and (earnings_ok or noi_ok) else None,
        "baseline_ready": bool(noi_ok and current_noi is not None),
        "baseline_missing": [] if noi_ok and current_noi is not None else ["Confirm current business NOI, reporting period and currency before planning its changes."],
        "baseline_fix_fields": [] if noi_ok and current_noi is not None else ["noi_source_comparability"],
        "draft_gap": _money(max(Decimal(0), target - draft_noi)) if target is not None and draft_noi is not None else None,
        "qualified_noi_delta": _money(qualified_noi - current_noi) if complete and source_ok and noi_ok and qualified_noi is not None and current_noi is not None else None,
        "qualified_first_year_noi_delta": _money(qualified_first - current_noi) if complete and source_ok and noi_ok and qualified_first is not None and current_noi is not None else None,
        "dscr_current_debt_before": _ratio(before_ratio), "dscr_current_debt_after": _ratio(after_ratio),
        "dscr_current_debt_delta": _ratio(after_ratio - before_ratio) if after_ratio is not None and before_ratio is not None else None,
        "qualified_dscr_current_debt_after": _ratio(qualified_ratio),
        "qualified_dscr_current_debt_delta": _ratio(qualified_ratio - before_ratio) if qualified_ratio is not None and before_ratio is not None else None,
        "qualified_first_year_dscr_current_debt_after": _ratio(qualified_first_ratio),
        "qualified_first_year_dscr_current_debt_delta": _ratio(qualified_first_ratio - before_ratio) if qualified_first_ratio is not None and before_ratio is not None else None,
        "source_issues": source_issues, "missing": _unique(missing),
        "notes": ["Arithmetic before implementation costs and timing; not a qualified or staff-reviewed forecast.",
            "Operating-plan NOI is before additional loan operating charges. Loan cash relief and cash assignments do not increase NOI.",
            "Monthly values are run-rate equivalents, not a forecast of when benefits begin. First-year amounts require explicit dates and setup costs."]}}


def public(value):
    """No staff receipts, reasons, actors, hashes or private quote metadata."""
    action_fields = ("action_id", "expense_row_id", "kind", "math_status", "gross_annual_delta", "gross_monthly_run_rate_delta",
        "gross_one_time_delta", "net_annual_delta", "first_year_delta", "math_missing", "qualification_missing", "source_issues", "fix_fields",
        "effect_type", "metric_issues", "noi_gross_annual_delta", "earnings_gross_annual_delta", "cash_gross_annual_delta",
        "noi_net_annual_delta", "earnings_net_annual_delta", "cash_net_annual_delta", "noi_first_year_delta", "earnings_first_year_delta", "cash_first_year_delta")
    summary_fields = ("status", "complete", "currency", "noi_currency", "source_comparable", "current_noi", "known_gross_annual_subtotal", "gross_annual_delta",
        "gross_monthly_run_rate_delta", "draft_noi", "draft_noi_delta", "draft_gap", "qualified_noi_delta", "qualified_first_year_noi_delta",
        "dscr_current_debt_before", "dscr_current_debt_after", "dscr_current_debt_delta", "qualified_dscr_current_debt_after",
        "qualified_dscr_current_debt_delta", "qualified_first_year_dscr_current_debt_after", "qualified_first_year_dscr_current_debt_delta",
        "source_issues", "missing", "notes", "gross_earnings_annual_delta", "gross_cash_annual_delta", "net_cash_annual_delta", "first_year_cash_delta",
        "baseline_ready", "baseline_missing", "baseline_fix_fields", "metric_issues", "current_earnings", "earnings_currency", "earnings_kind",
        "draft_earnings", "draft_earnings_delta", "qualified_earnings", "qualified_earnings_delta", "qualified_first_year_earnings", "qualified_first_year_earnings_delta")
    summary = {key: deepcopy((value.get("summary") or {}).get(key)) for key in summary_fields}
    summary["working"] = {key: deepcopy(((value.get("summary") or {}).get("working") or {}).get(key)) for key in WORKING_FIELDS}
    return {"basis": BASIS, "actions": [{key: deepcopy(row.get(key)) for key in action_fields} for row in value.get("actions", [])], "summary": summary}
