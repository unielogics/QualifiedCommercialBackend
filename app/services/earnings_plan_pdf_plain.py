"""A v2 earnings plan as plain tables, for the fallback PDF engine.

The service draws the plan's document with its main PDF engine
(``earnings_plan_pdf``). Where that engine cannot run, the fallback lays out
text and tables only, so the same stored facts are printed here without charts
or columns: every figure, in the same order, nothing designed.
"""
from __future__ import annotations

import html

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SIX = (("ebitda", "EBITDA"), ("noi", "NOI"), ("dscr", "DSCR"), ("free_cash_flow", "Free cash flow"), ("operating_margin", "Operating margin"), ("debt_service", "Debt service"))
BEHAVIORS = {"fixed": "Fixed", "unit": "Per unit", "pct": "Percent of revenue"}
ANSWERS = {"included": "Included in another line", "not_applicable": "Not applicable", "help": "Team will help"}
STATUS = {"approved": "Reviewed", "rejected": "Rejected", "proposed": "Proposed"}


def _text(value) -> str:
    return html.escape(str(value if value not in (None, "") else "Not supplied"))


def _money(value, signed: bool = False) -> str:
    if value is None:
        return "Not available"
    sign = "-" if value <= -0.5 else "+" if signed and value >= 0.5 else ""
    return f"{sign}${abs(value):,.0f}"


def _ratio(value, signed: bool = False) -> str:
    if value is None:
        return "Not available"
    return f"{'-' if value < 0 else '+' if signed else ''}{abs(value):.2f}x"


def _percent(value) -> str:
    return "Not available" if value is None else f"{'-' if value < 0 else ''}{abs(value) * 100:.1f}%"


def _figure(key: str, value) -> str:
    return _ratio(value) if key == "dscr" else _percent(value) if key == "operating_margin" else _money(value)


def _month(plan_start: str | None, index: int) -> str:
    if not plan_start:
        return f"Month {index}"
    year, first = (int(part) for part in plan_start.split("-"))
    at = first - 1 + index - 1
    return f"{MONTHS[at % 12]} {year + at // 12}"


def _table(head: list[str], rows: list[list[str]], numeric_from: int = 1) -> str:
    if not rows:
        return ""
    cells = "".join(f"<th{' class=num' if index >= numeric_from else ''}>{_text(label)}</th>" for index, label in enumerate(head))
    body = "".join("<tr>" + "".join(f"<td{' class=num' if index >= numeric_from else ''}>{cell}</td>" for index, cell in enumerate(row)) + "</tr>" for row in rows)
    return f"<table><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>"


def build_html(*, body: dict, financial_totals: dict | None = None) -> str:
    facts = financial_totals or {}
    result = facts.get("plan_result") or {}
    baseline = result.get("baseline") or {}
    source = baseline.get("source") or {}
    metrics = result.get("metrics") or {}
    basis = "with_terms" if (result.get("debt_service") or {}).get("with_terms") is not None else "current_debt"
    today = (metrics.get("today") or {}).get("current_debt") or {}
    proposed = (metrics.get("proposed") or {}).get(basis) or {}
    reviewed = (metrics.get("reviewed") or {}).get(basis) or {}
    labels = result.get("labels") or {}
    effects = result.get("effects") or {}
    plan_start = body.get("plan_start")
    review = facts.get("operating_review") or {}
    status = "Client submission, awaiting staff review" if facts.get("proposal_status") == "client_submitted" else "Saved by staff"

    head = (f"<h1>Operating expenses and earnings plan</h1><p><b>{_text(body.get('business_name'))}</b></p>"
            f"<p>Source: {_text(source.get('label'))} | Plan starts {_text(_month(plan_start, 1))} | {_text(body.get('currency'))} | {_text(status)}"
            + (f" | Version {_text(review.get('version_number'))}" if review.get("version_number") else "") + "</p>"
            "<div class='notice'>What the business costs to run today, the changes it proposes, and what they do to earnings and debt coverage. "
            "The source figures stay as filed. Every change is a conditional proposal until staff has reviewed it; nothing here is a funding commitment.</div>")

    six = _table(["Metric", "Today", "With the plan", "Reviewed changes only"],
                 [[label, _figure(key, today.get(key)), _figure(key, proposed.get(key)), _figure(key, reviewed.get(key))] for key, label in SIX])
    terms_note = ("Debt service and coverage after the plan use the saved loan terms." if basis == "with_terms"
                  else "No loan terms are saved, so debt service and coverage use today's obligations.")
    target = result.get("target") or {}
    need = target.get("proposed")
    target_line = "" if need is None else (f"<p>The plan clears {_ratio(target.get('ratio'))} coverage.</p>" if need <= 0
                                           else f"<p>Still to find for {_ratio(target.get('ratio'))} coverage: <b>{_money(need)}</b> of EBITDA a year.</p>")

    confirmations = (body.get("source") or {}).get("confirmations") or {}
    corrections = (body.get("source") or {}).get("corrections") or {}
    group_rows = []
    for group in baseline.get("groups") or []:
        state = "Correction noted" if confirmations.get(group["key"]) == "corrected" else "Confirmed" if confirmations.get(group["key"]) else "Not confirmed"
        group_rows.append([_text(group.get("label")), _money(group.get("amount")), _text(state)])
    correction_notes = "".join(f"<p><b>{_text(next((g.get('label') for g in baseline.get('groups') or [] if g['key'] == key), key))}:</b> "
                               f"{_money(row.get('amount'))} against the source. {_text(row.get('reason'))}</p>" for key, row in corrections.items())
    ladder = _table(["Source figure", "Annual"], [["Revenue", _money(baseline.get("revenue"))], ["Cost of goods sold", _money(baseline.get("cogs"))],
                                                  ["Operating expenses", _money(baseline.get("operating_expenses"))], ["NOI", _money(baseline.get("noi"))],
                                                  ["EBITDA", _money(baseline.get("ebitda"))]])
    answers = body.get("category_answers") or {}
    question_rows = [[_text(row.get("label")), _text(ANSWERS.get(answers.get(row["key"]), "Not answered"))] for row in result.get("questions") or []]

    model = result.get("model") or {}
    stream_rows = []
    for stream in body.get("streams") or []:
        factors = "; ".join(f"{factor.get('label')}: {factor.get('value'):g} {factor.get('unit') or ''}".strip() for factor in stream.get("factors") or [] if factor.get("value") is not None)
        stream_rows.append([_text(stream.get("name")), _text(factors), _money((model.get("revenue") or {}).get(stream["id"])),
                            _money(((result.get("proposed") or {}).get("stream_revenue") or {}).get(stream["id"]))])
    reconcile = ("The revenue model reconciles to the source's revenue." if model.get("reconciles")
                 else f"The revenue model differs from the source's revenue by {_money(model.get('difference'), signed=True)}.")

    names = {stream["id"]: stream.get("name") for stream in body.get("streams") or []}
    behaviors = result.get("behaviors") or {}
    plan_lines = (result.get("proposed") or {}).get("lines") or {}
    line_rows = []
    for line in body.get("lines") or []:
        behavior = behaviors.get(line["id"], line.get("behavior"))
        follows = "" if behavior == "fixed" else f" of {names.get(line.get('link'), 'all revenue') if line.get('link') != 'all' else 'all revenue'}"
        line_rows.append([_text(line.get("label")), _text(BEHAVIORS.get(behavior, behavior) + follows), _money(line.get("base")), _money(plan_lines.get(line["id"]))])

    change_sections = []
    rows_by_id = {str(row["id"]): row for block in ("changes", "growth", "activities") for row in body.get(block) or []}
    items = {row["id"]: row for row in result.get("items") or []}
    for section in result.get("sections") or []:
        rows = []
        for identifier in section.get("items") or []:
            row, item, effect = rows_by_id.get(identifier) or {}, items.get(identifier) or {}, effects.get(identifier) or {}
            state = "Switched off" if not item.get("on", True) else STATUS.get(row.get("status"), "Proposed")
            rows.append([f"{_text(labels.get(identifier))}<br><small>{_text(row.get('note') or 'No explanation given')}"
                         + (f"<br>Responsible: {_text(row.get('owner'))}" if row.get("owner") else "") + "</small>",
                         _text(_month(plan_start, item.get("start_month") or 1)), _money(effect.get("ebitda"), signed=True),
                         _money(effect.get("first_year"), signed=True), _text(state)])
        if rows:
            change_sections.append(f"<h3>{_text(section.get('label'))} <small>EBITDA {_money(section.get('ebitda'), signed=True)} a year</small></h3>"
                                   + _table(["Change", "Starts", "EBITDA a year", "First year", "Status"], rows, numeric_from=2))
    bridge = _table(["From the baseline to the plan", "EBITDA a year"],
                    [["Baseline EBITDA", _money(baseline.get("ebitda"))],
                     *[[_text(step.get("label")), _money(step.get("value"), signed=True)] for step in (result.get("bridge") or {}).get("proposed") or []],
                     ["Plan EBITDA", _money((result.get("proposed") or {}).get("ebitda"))]])
    months = (result.get("months") or {}).get("proposed") or []
    first_year = (result.get("first_year") or {}).get("proposed") or {}
    ramp = _table(["Month", "EBITDA", "NOI"], [[_text(_month(plan_start, index)), _money(row.get("ebitda")), _money(row.get("noi"))] for index, row in enumerate(months, 1)])

    released = result.get("released") or {}
    allocation_rows = [[_text(row.get("label")), _text(str(row.get("purpose") or "other").capitalize()), _money(row.get("monthly_amount"))] for row in body.get("allocations") or []]
    carried = [[f"{_text(row.get('label'))}<br><small>{_text(row.get('detail'))}</small>", _money(row.get("annual_amount"), signed=True) if row.get("annual_amount") is not None else "Not stated"]
               for row in body.get("carried_over") or [] if not row.get("resolved")]
    goal = result.get("goal") or {}
    goal_line = "" if goal.get("amount") is None else (f"<h2>Annual NOI goal</h2><p>Goal {_money(goal.get('amount'))}. With the plan: {_money((result.get('proposed') or {}).get('noi'))}. "
                                                       + (f"Still to find: {_money(goal.get('proposed_gap'))}." if (goal.get("proposed_gap") or 0) > 0 else "The plan reaches the goal.") + "</p>")

    return ("""<!doctype html><html><head><meta charset='utf-8'><style>
    @page {size:Letter; margin:32px;}
    body{font:11px Arial,sans-serif;color:#172033;line-height:1.4}h1{font-size:22px;margin:0 0 8px}h2{font-size:15px;margin:22px 0 8px}h3{font-size:12px;margin:12px 0 6px}
    p{margin:5px 0}small,.muted{font-size:9px;color:#64748b}.notice{padding:10px;background:#f1f5f9;border-left:3px solid #1b4b9e;margin:12px 0}
    table{width:100%;border-collapse:collapse;table-layout:fixed}thead{display:table-header-group}th{font-size:9px;text-align:left;background:#f1f5f9}
    th,td{padding:6px;border-bottom:1px solid #dbe2ec;vertical-align:top;overflow-wrap:anywhere}td.num,th.num{text-align:right}
    </style></head><body>""" + head
            + "<h2>Before and after</h2>" + six + f"<p class='muted'>{_text(terms_note)} With the plan counts every change that is switched on and not rejected, at its full-year run rate.</p>" + target_line
            + "<h2>1. Source figures</h2>" + ladder + (_table(["Source line", "Annual on the source", "Business confirmation"], group_rows) or "") + correction_notes
            + (("<h3>Costs the source prints no line for</h3>" + _table(["Category", "Answer"], question_rows, numeric_from=9)) if question_rows else "")
            + "<h2>2. Revenue model and current costs</h2>" + (_table(["Revenue stream", "How it is made", "Today", "With the plan"], stream_rows, numeric_from=2) or "<p>No revenue stream described.</p>")
            + f"<p class='muted'>{_text(reconcile)}</p>" + (_table(["Cost", "Behaviour", "Today", "With the plan"], line_rows, numeric_from=2) or "<p>No cost lines.</p>")
            + "<h2>3. Plan changes</h2>" + ("".join(change_sections) or "<p>No changes proposed.</p>") + "<h3>How the changes add up</h3>" + bridge
            + "<p class='muted'>The bridge applies the sections in this order and adds up exactly. A change's own figure is what the plan loses without it; changes interact, so those do not add up to the bridge.</p>"
            + (f"<h2>4. First year, month by month</h2>{ramp}<p>First-year EBITDA {_money(first_year.get('ebitda'))} against a full-year run rate of {_money((result.get('proposed') or {}).get('ebitda'))}. "
               "Each change counts from its start month; one-time setup costs fall in that month.</p>" if months else "")
            + goal_line
            + (("<h2>Released cash</h2>" + f"<p>The saved loan terms release {_money(released.get('available_monthly'))} a month. Assigned: {_money(released.get('allocated_monthly'))} a month; the rest stays as cash.</p>"
                + _table(["Use", "Purpose", "Monthly"], allocation_rows, numeric_from=2)) if allocation_rows or released.get("available_monthly") else "")
            + (("<h2>Carried over from the earlier plan</h2><p class='muted'>Kept for re-entry. These are not counted in any figure above.</p>" + _table(["Item", "Annual effect stated"], carried)) if carried else "")
            + f"<h2>Confirmation and notes</h2><p>{'The business confirmed these disclosures and proposals.' if body.get('attestation') else 'The disclosures and proposals have not been confirmed.'}</p>"
            + (f"<p>{_text(body.get('notes'))}</p>" if body.get("notes") else "") + "</body></html>")
