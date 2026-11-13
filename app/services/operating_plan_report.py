"""Immutable Step 4 review figures, captured by the normal Save version flow."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from html import escape

SCHEMA = "operating-plan-review-v1"
SUMMARY_FIELDS = ("status", "complete", "currency", "noi_currency", "earnings_currency", "earnings_kind",
    "current_noi", "current_earnings", "source_comparable", "source_issues", "missing", "notes")
WORKING_FIELDS = ("basis", "noi", "noi_delta", "earnings", "earnings_delta", "first_year_noi",
    "first_year_noi_delta", "first_year_earnings", "first_year_earnings_delta", "dscr_current_debt_before",
    "dscr_current_debt_after", "dscr_current_debt_delta", "dscr_with_refinance_after", "dscr_with_refinance_delta",
    "first_year_dscr_current_debt_after", "first_year_dscr_current_debt_delta", "cash_after_debt_before_annual",
    "cash_after_debt_after_annual", "cash_after_debt_delta_annual", "cash_after_debt_before_monthly",
    "cash_after_debt_after_monthly", "cash_after_debt_delta_monthly", "loan_cash_change_monthly",
    "loan_cash_change_annual", "refinance_coverage_basis")


def capture(evaluation, *, version_id, version_number: int, saved_at: datetime):
    summary = (evaluation.get("draft_feedback") or {}).get("summary") or {}
    return {"schema_version": SCHEMA, "version_id": str(version_id), "version_number": version_number,
        "saved_at": saved_at.isoformat(), "historical_figures_unchanged": True,
        "summary": {key: deepcopy(summary.get(key)) for key in SUMMARY_FIELDS},
        "working": {key: deepcopy((summary.get("working") or {}).get(key)) for key in WORKING_FIELDS},
        "source": {key: deepcopy((evaluation.get("baseline") or {}).get(key)) for key in ("period", "basis", "currency", "provisional")},
        "all_actions_reviewed": bool(evaluation.get("all_actions_reviewed")),
        "risk_flags": deepcopy(evaluation.get("risk_flags") or []),
        "current_debt_service_annual": (evaluation.get("baseline") or {}).get("annual_debt_service")}


def _text(value):
    return escape(str(value if value not in (None, "") else "Not supplied"))


def build_html(report):
    """Display stored engine outputs only; a download never evaluates today's draft."""
    if not report or report.get("schema_version") != SCHEMA:
        return ""
    summary, working = report.get("summary") or {}, deepcopy(report.get("working") or {})
    # Match the review screen's visibility guards; do not present a partial
    # subtotal as a complete plan when its source or inputs remain unresolved.
    safe = summary.get("complete") and summary.get("status") != "source_blocked" and summary.get("source_comparable") is not False
    if not safe:
        for key in ("noi", "noi_delta", "first_year_noi", "first_year_noi_delta"):
            working[key] = None
    if not summary.get("complete"):
        for key in ("earnings", "earnings_delta", "first_year_earnings", "first_year_earnings_delta"):
            working[key] = None
    if not summary.get("complete") or summary.get("status") == "incomplete":
        for key in ("dscr_current_debt_after", "dscr_current_debt_delta", "first_year_dscr_current_debt_after", "first_year_dscr_current_debt_delta"):
            working[key] = None
    kind = str(summary.get("earnings_kind") or "")
    earnings = "Annual earnings (tax estimate)" if "tax" in kind else "Annual EBITDA" if "ebitda" in kind else "Annual coverage earnings"
    rows = [
        ("Annual NOI", summary.get("current_noi"), working.get("noi"), working.get("noi_delta"), False, summary.get("noi_currency")),
        (earnings, summary.get("current_earnings"), working.get("earnings"), working.get("earnings_delta"), False, summary.get("earnings_currency")),
        ("Cash after debt / year (illustration)", working.get("cash_after_debt_before_annual"), working.get("cash_after_debt_after_annual"), working.get("cash_after_debt_delta_annual"), False, summary.get("earnings_currency")),
        ("Cash after debt / month (illustration)", working.get("cash_after_debt_before_monthly"), working.get("cash_after_debt_after_monthly"), working.get("cash_after_debt_delta_monthly"), False, summary.get("earnings_currency")),
        ("First-year NOI", summary.get("current_noi"), working.get("first_year_noi"), working.get("first_year_noi_delta"), False, summary.get("noi_currency")),
        ("First-year earnings", summary.get("current_earnings"), working.get("first_year_earnings"), working.get("first_year_earnings_delta"), False, summary.get("earnings_currency")),
        ("DSCR - current debt", working.get("dscr_current_debt_before"), working.get("dscr_current_debt_after"), working.get("dscr_current_debt_delta"), True, None),
        ("DSCR - saved refinance", working.get("dscr_current_debt_before"), working.get("dscr_with_refinance_after"), working.get("dscr_with_refinance_delta"), True, None),
        ("First-year DSCR - current debt", working.get("dscr_current_debt_before"), working.get("first_year_dscr_current_debt_after"), working.get("first_year_dscr_current_debt_delta"), True, None),
    ]

    def value(amount, ratio, currency, *, delta=False):
        if amount is None:
            return "Needs source inputs"
        if ratio:
            return f"{'+' if delta and amount > 0 else ''}{amount:.4f}x"
        if not currency:
            return "Currency needs confirmation"
        sign = "-" if amount < 0 else "+" if delta and amount > 0 else ""
        prefix = "$" if str(currency).upper() == "USD" else escape(str(currency).upper()) + " "
        return f"{sign}{prefix}{abs(amount):,.2f}"

    table_rows = []
    for label, before, after, delta, ratio, currency in rows:
        if label != "Annual NOI" and before is None and after is None and delta is None:
            continue
        tone = "positive" if delta is not None and delta > 0 else "negative" if delta is not None and delta < 0 else ""
        table_rows.append(f"<tr><td>{_text(label)}</td><td class='num'>{value(before, ratio, currency)}</td>"
            f"<td class='num'>{value(after, ratio, currency)}</td><td class='num {tone}'>{value(delta, ratio, currency, delta=True)}</td></tr>")
    basis = "After entered setup costs, before implementation timing." if working.get("basis") == "after_setup_before_timing" else "Before setup costs and implementation timing; inputs remain subject to review."
    issues = "" if summary.get("complete") else "<p class='negative'>Some saved inputs still need review. Unavailable figures are not treated as zero.</p>"
    source = report.get("source") or {}
    source_note = (f"<p class='muted'>Saved source period: {_text(source.get('period'))}; {_text(source.get('currency'))}; {_text(source.get('basis'))} basis. "
        + ("The earnings source is provisional. " if source.get("provisional") else "")
        + ("All active actions have a recorded staff review; rely only on the recorded review scope." if report.get("all_actions_reviewed") else "Proposed actions remain subject to staff review.") + "</p>")
    risks = "<ul>" + "".join(f"<li>{_text(item)}</li>" for item in report.get("risk_flags") or []) + "</ul>" if report.get("risk_flags") else ""
    return (f"<div class='notice'><b>Saved plan v{_text(report.get('version_number'))}</b> | Saved {_text(report.get('saved_at'))}"
        "<br>This PDF preserves this saved version. Later edits create a new version and do not change this document.</div>"
        f"<h2>Saved working plan - before and after</h2><p>{basis} Conditional operating changes for execution with underwriting over the coming months; subject to review.</p>"
        "<table><thead><tr><th>Metric</th><th>Current</th><th>Working plan</th><th>Change</th></tr></thead><tbody>" + "".join(table_rows) + "</tbody></table>" + issues +
        "<p class='muted'>Cash after debt is an earnings-less-debt-service illustration, not a bank balance. Monthly amounts are run-rate equivalents. Loan payment relief and cash assignments do not increase NOI. Saved-refinance DSCR uses the coverage window and maturity stress after additional operating charges. Historical source figures are unchanged; this plan does not file or amend tax returns.</p>" + source_note + risks)
