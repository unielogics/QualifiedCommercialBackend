"""The saved document of a v2 earnings plan.

Six parts, each starting on its own page: the summary, the source figures, the
revenue model and current costs, the plan's changes with their logic, before
and after with the bridge and the first-year ramp, and review and handoff.

Drawn only from what the version stored: its body and the engine's figures
captured when it was saved (``key_facts.plan_result``). A later download never
re-evaluates today's draft or today's source figures, so none of the plan's
engine lives here: this module places stored figures on a page and does only
the sums a page needs (a subtotal, one stored figure over another). What a
reviewer wrote for staff (who, why, how a change reconciles to the baseline) is
never printed; a change shows its status only.

Figures print the way the plan's screens print them (the same signs, rounding
and words), so the page a business saw and the document it is handed agree.
"""
from __future__ import annotations

import html
import logging
import math
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

log = logging.getLogger(__name__)

MINUS, DASH, ARROW = "−", "—", "→"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
ITEM_BLOCKS = ("changes", "growth", "activities")
ALL, COGS_GROUP = "all", "cogs"
#: The six figures every part of the product reads the plan by, in this order.
SIX = (("ebitda", "EBITDA"), ("noi", "NOI"), ("dscr", "DSCR"), ("free_cash_flow", "Free cash flow"),
       ("operating_margin", "Operating margin"), ("debt_service", "Debt service"))
ANSWERS = {"included": "Included in another line", "not_applicable": "Not applicable", "help": "Team will help"}
PURPOSES = {"existing": "Existing cost", "planned": "Planned increment", "reserve": "Reserve", "other": "Other"}
PARTS = ("Summary", "Source figures", "Revenue model and current costs", "Plan changes", "Before and after", "Review and handoff")


# ---------------------------------------------------------------------------
# Words and figures
# ---------------------------------------------------------------------------

def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value, default: float = 0.0) -> float:
    return float(value) if _finite(value) else default


def _grouped(value: float, digits: int = 0) -> str:
    """|value| with thousands separators, halves rounded away from zero as the screens round them."""
    return f"{Decimal(str(abs(value))).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP):,.{digits}f}"


def _is_zero(text: str) -> bool:
    return float(text.replace(",", "")) == 0


def _usd(value, digits: int = 0) -> str:
    if not _finite(value):
        return DASH
    text = _grouped(value, digits)
    return f"{MINUS if value < 0 and not _is_zero(text) else ''}${text}"


def _signed_usd(value) -> str:
    if not _finite(value):
        return DASH
    text = _grouped(value)
    return "$0" if _is_zero(text) else f"{'+' if value > 0 else MINUS}${text}"


def _times(value) -> str:
    if not _finite(value):
        return DASH
    text = _grouped(value, 2)
    return f"{MINUS if value < 0 and not _is_zero(text) else ''}{text}x"


def _signed_times(value) -> str:
    if not _finite(value):
        return DASH
    text = _grouped(value, 2)
    return "0.00x" if _is_zero(text) else f"{'+' if value > 0 else MINUS}{text}x"


def _percent(value, digits: int = 1) -> str:
    """A ratio as a percent: 0.1158 prints 11.6%."""
    if not _finite(value):
        return DASH
    text = _grouped(value * 100, digits)
    return f"{MINUS if value < 0 and not _is_zero(text) else ''}{text}%"


def _points(value) -> str:
    if not _finite(value):
        return DASH
    text = _grouped(value * 100, 1)
    return "0.0 pts" if _is_zero(text) else f"{'+' if value > 0 else MINUS}{text} pts"


def _plain(value, digits: int = 2) -> str:
    """1250.5 prints 1,250.5; whole numbers print without decimals."""
    if not _finite(value):
        return DASH
    text = _grouped(value, digits)
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return f"{MINUS if value < 0 and not _is_zero(text) else ''}{text}"


def _six(key: str, value) -> str:
    return _times(value) if key == "dscr" else _percent(value) if key == "operating_margin" else _usd(value)


def _six_change(key: str, value) -> str:
    return _signed_times(value) if key == "dscr" else _points(value) if key == "operating_margin" else _signed_usd(value)


def _factor_value(factor: dict, value) -> str:
    """A stream metric in its own unit: $262.00, 95%, 340 units / mo."""
    unit = str(factor.get("unit") or "")
    if not _finite(value):
        return DASH
    if unit == "$":
        return f"${_grouped(value, 2)}"
    if unit == "%":
        return f"{_plain(value, 1)}%"
    return f"{_plain(value)} {unit}".strip()


def _plan_start(value) -> tuple[int, int] | None:
    try:
        year, month = (int(part) for part in str(value).split("-"))
    except (TypeError, ValueError):
        return None
    return (year, month) if 1 <= month <= 12 else None


def _month(plan_start, index: int, short: bool = False) -> str:
    """Month ``index`` (from 0) of a plan: Jan 2027; along the ramp, Jan."""
    start = _plan_start(plan_start)
    if not start:
        return str(index + 1) if short else f"Month {index + 1}"
    at = start[1] - 1 + index
    return MONTHS[at % 12] if short else f"{MONTHS[at % 12]} {start[0] + at // 12}"


def _day(value) -> tuple[int, int, int] | None:
    try:
        year, month, day = (int(part) for part in str(value)[:10].split("-"))
    except (TypeError, ValueError):
        return None
    return (year, month, day) if 1 <= month <= 12 else None


def _period(start, end) -> str | None:
    first, last = _day(start), _day(end)
    if not first or not last:
        return None
    return f"{MONTHS[first[1] - 1]} {first[2]}{'' if first[0] == last[0] else f', {first[0]}'} – {MONTHS[last[1] - 1]} {last[2]}, {last[0]}"


def _saved_on(value) -> str:
    """The day a version was saved, in the firm's own zone."""
    if not value:
        return ""
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    from app.services.local_time import local_stamp

    return local_stamp(moment, fmt="%b %-d, %Y", label=False)


def _count(amount: int, word: str, plural: str | None = None) -> str:
    return f"{amount} {word if amount == 1 else plural or word + 's'}"


def _tag(label: str, tone: str = "mut") -> str:
    return f"<span class='tag {tone}'>{_esc(label)}</span>"


def _tone(value, *, lower_is_better: bool = False) -> str:
    """How a change reads: good, bad or neither."""
    if not _finite(value) or abs(value) < 1e-6:
        return "mut"
    return "ok" if (value < 0) == lower_is_better else "bad"


def _signed_class(value) -> str:
    return " neg" if _finite(value) and value <= -0.5 else ""


def _table(head: list[tuple[str, str]], rows: list[str], cls: str = "") -> str:
    """``head`` is (label, column class); ``rows`` are ready ``<tr>`` strings.

    A band row (``<tr class='band'>``) heads the rows after it. It is grouped with the first of them so a page never
    ends on a heading with nothing under it.
    """
    if not rows:
        return ""
    cells = "".join(f"<th class='{column}'>{_esc(label)}</th>" for label, column in head)
    groups, index = [], 0
    while index < len(rows):
        if rows[index].startswith("<tr class='band'>") and index + 1 < len(rows):
            groups.append(f"<tbody class='keep'>{rows[index]}{rows[index + 1]}</tbody>")
            index += 2
            continue
        start = index
        while index < len(rows) and not (rows[index].startswith("<tr class='band'>") and index + 1 < len(rows)):
            index += 1
        groups.append(f"<tbody>{''.join(rows[start:index])}</tbody>")
    return f"<table class='{cls}'><thead><tr>{cells}</tr></thead>{''.join(groups)}</table>"


def _row(cells: list[tuple[str, str]], cls: str = "") -> str:
    """``cells`` are (ready HTML, cell class)."""
    return f"<tr class='{cls}'>" + "".join(f"<td class='{column}'>{content}</td>" for content, column in cells) + "</tr>"


# ---------------------------------------------------------------------------
# What a saved version holds
# ---------------------------------------------------------------------------

class _Plan:
    """A saved version's body and stored figures, read once for every part of the document."""

    def __init__(self, body: dict | None, facts: dict | None) -> None:
        self.body = body or {}
        self.facts = facts or {}
        self.result = self.facts.get("plan_result") or {}
        self.baseline = self.result.get("baseline") or {}
        self.source = self.baseline.get("source") or {}
        #: False for a version saved before the file had an earnings source: there are no figures to print.
        self.available = bool(self.result) and self.baseline.get("available") is not False
        debt = self.result.get("debt_service") or {}
        self.current_debt, self.terms_debt = debt.get("current"), debt.get("with_terms")
        #: What the "after" figures are measured against: the saved loan terms when there are any.
        self.basis = "with_terms" if _finite(self.terms_debt) else "current_debt"
        self.plan_debt = self.terms_debt if self.basis == "with_terms" else self.current_debt
        #: Operating costs a year the saved terms themselves add. They come off coverage with the terms, not off earnings.
        self.loan_operating = _num(debt.get("loan_operating")) if self.basis == "with_terms" else 0.0
        self.loan_operating_confirmed = self.baseline.get("loan_operating_confirmed") is not False
        metrics = self.result.get("metrics") or {}
        self.today = (metrics.get("today") or {}).get("current_debt") or {}
        self.after = (metrics.get("proposed") or {}).get(self.basis) or {}
        self.after_reviewed = (metrics.get("reviewed") or {}).get(self.basis) or {}
        self.proposed = self.result.get("proposed") or {}
        self.reviewed = self.result.get("reviewed") or {}
        self.model = self.result.get("model") or {}
        self.items = self.result.get("items") or []
        self.effects = self.result.get("effects") or {}
        self.labels = self.result.get("labels") or {}
        self.groups = self.baseline.get("groups") or []
        self.group_labels = {group.get("key"): group.get("label") for group in self.groups}
        self.rows = {str(row.get("id")): row for block in ITEM_BLOCKS for row in self.body.get(block) or []}
        self.streams = {stream.get("id"): stream for stream in self.body.get("streams") or []}
        self.lines = {line.get("id"): line for line in self.body.get("lines") or []}
        self.plan_start = self.body.get("plan_start")
        review = self.facts.get("operating_review") or {}
        self.version = review.get("version_number")
        self.saved = _saved_on(review.get("saved_at"))
        self.client_submission = self.facts.get("proposal_status") == "client_submitted"

    @property
    def business(self) -> str:
        return str(self.body.get("business_name") or "").strip() or "Business name not supplied"

    @property
    def source_short(self) -> str:
        """IRS 2025, P&L, or Source: how the source's own column is headed."""
        if self.source.get("kind") == "tax_return":
            return f"IRS {self.source['year']}" if self.source.get("year") else "IRS return"
        if self.source.get("kind") == "profit_and_loss":
            return "P&L"
        name = self.baseline.get("source_name")
        return str(name) if name and name != "source" else "Source"

    @property
    def period(self) -> str:
        source = self.source
        text = (_period(source.get("period_start"), source.get("period_end"))
                or (f"Tax year {source['year']}" if source.get("year") else None)
                or _period(self.body.get("period_start"), self.body.get("period_end")) or "Period not stated")
        return text + (" · calendar year assumed" if source.get("period_assumed") else "") + (" · annualized" if source.get("annualized") else "")

    @property
    def currency_basis(self) -> str:
        basis = str(self.source.get("basis") or "")
        return " · ".join(part for part in (self.source.get("currency") or self.body.get("currency") or "USD", basis.capitalize()) if part)

    def unit_word(self, stream: dict | None) -> str:
        from app.schemas.earnings_plan_v2 import STREAM_TEMPLATES

        return STREAM_TEMPLATES.get((stream or {}).get("type") or "custom", STREAM_TEMPLATES["custom"])["unit_word"]

    def stream_type(self, stream: dict) -> str:
        from app.schemas.earnings_plan_v2 import STREAM_TEMPLATES

        return STREAM_TEMPLATES.get(stream.get("type") or "custom", STREAM_TEMPLATES["custom"])["label"]

    @property
    def loan_costs_note(self) -> str:
        """Said wherever coverage with the terms is printed, when the loan brings operating costs of its own."""
        if self.loan_operating > 0:
            return (f" The saved loan terms add {_usd(self.loan_operating)} a year of operating costs; they come off DSCR and free cash flow with the terms, "
                    "and leave EBITDA and NOI as the plan produces them.")
        if self.basis == "with_terms" and not self.loan_operating_confirmed:
            return " The saved loan terms list a recurring operating cost whose amount is not confirmed; it is counted as zero until the budget is confirmed."
        return ""

    def status(self, item: dict) -> tuple[str, str]:
        """A change's state in words. Status only: never who reviewed it or why."""
        if not item.get("on", True):
            return "Switched off", "mut"
        if item.get("status") == "approved":
            return "Reviewed", "ok"
        if item.get("status") == "rejected":
            return "Rejected", "bad"
        return "Proposed", "mut"


# ---------------------------------------------------------------------------
# Drawn figures: bars, scales and the ramp. Positions only; every number comes from the stored result.
# ---------------------------------------------------------------------------

def _waterfall(start: float, steps: list[float], end: float) -> tuple[list[tuple[float, float, str]], float]:
    """Where each bar of a bridge sits on a shared scale, as (left %, width %, tone), and where zero sits."""
    points, running = [start], start
    for value in steps:
        running += value
        points.append(running)
    low, high = min(0.0, end, *points), max(0.0, end, *points)
    span = (high - low) or 1.0

    def at(value: float) -> float:
        return (value - low) / span * 100

    bars = [(min(at(0), at(start)), abs(at(start) - at(0)), "loss" if start < 0 else "total")]
    running = start
    for value in steps:
        bars.append((min(at(running), at(running + value)), abs(at(running + value) - at(running)), "up" if value >= 0 else "down"))
        running += value
    bars.append((min(at(0), at(end)), abs(at(end) - at(0)), "loss" if end < 0 else "total"))
    return bars, at(0)


def _bar(bar: tuple[float, float, str] | None, zero: float) -> str:
    drawn = "" if bar is None else f"<i class='{bar[2]}' style='left:{bar[0]:.2f}%;width:{max(bar[1], 0.5):.2f}%'></i>"
    return f"<div class='barbox'>{drawn}<b style='left:{zero:.2f}%'></b></div>"


def _bridge(plan: _Plan, *, detailed: bool) -> str:
    """Baseline EBITDA, the steps in their fixed order, and the plan's figure. The steps add up exactly."""
    steps = [{**step, "value": 0.0 if abs(_num(step.get("value"))) < 0.005 else _num(step.get("value"))}
             for step in (plan.result.get("bridge") or {}).get("proposed") or []]
    reviewed = {step.get("key"): _num(step.get("value")) for step in (plan.result.get("bridge") or {}).get("reviewed") or []}
    start, end = _num(plan.baseline.get("ebitda")), _num(plan.proposed.get("ebitda"))
    bars, zero = _waterfall(start, [step["value"] for step in steps], end)

    def line(label: str, bar, value: str, other: str, cls: str = "") -> str:
        cells = [(_esc(label), "name"), (_bar(bar, zero), "bar"), (value, "num")]
        return _row(cells + ([(other, "num muted")] if detailed else []), cls)

    rows = [line("Baseline EBITDA", bars[0], f"<span class='{_signed_class(start).strip()}'>{_usd(start)}</span>", _usd(start), "edge")]
    for index, step in enumerate(steps):
        rows.append(line(str(step.get("label") or ""), None if step["value"] == 0 else bars[index + 1],
                         f"<span class='{'pos' if step['value'] > 0 else 'neg' if step['value'] < 0 else 'muted'}'>{_signed_usd(step['value'])}</span>",
                         _signed_usd(reviewed.get(step.get("key")))))
    rows.append(line("Plan EBITDA", bars[-1], f"<span class='{_signed_class(end).strip()}'>{_usd(end)}</span>", _usd(plan.reviewed.get("ebitda")), "edge"))
    head = [("From today to the plan", ""), ("", ""), ("EBITDA a year", "num")] + ([("Reviewed changes only", "num")] if detailed else [])
    return _table(head, rows, "bridge")


def _coverage(plan: _Plan) -> str:
    """Where coverage stands against 1.00x and the lender's threshold; nothing when there is no debt to cover."""
    today, after = plan.today.get("dscr"), plan.after.get("dscr")
    debt, target = plan.after.get("debt_service"), _num((plan.result.get("target") or {}).get("ratio"), 1.25)
    if not plan.available or not _finite(today) or not _finite(after) or not debt:
        return ""
    low, high = min(-0.25, today - 0.1, after - 0.1), max(1.75, today + 0.25, after + 0.25, target + 0.25)

    def at(value: float) -> float:
        return min(100.0, max(0.0, (value - low) / (high - low) * 100))

    state = ("Meets goal", "ok") if after >= target else ("Covers debt", "warn") if after >= 1 else ("Below 1.00x", "bad") if after > 0 else ("Negative", "bad")
    stored = (plan.result.get("target") or {}).get("proposed")
    needed = stored if _finite(stored) else target * debt - _num(plan.after.get("ebitda"))
    need = f"Needs {_usd(needed)} more EBITDA a year for {_times(target)}" if needed > 0.5 else f"Clears {_times(target)}"
    left, width = min(at(today), at(after)), abs(at(after) - at(today))
    return ("<div class='card'><div class='cardhead split'><span class='label'>DSCR · lender threshold</span><span>" + _tag(*state) + "</span></div>"
            "<div class='scale'><div class='track'></div>"
            f"<div class='fill{'' if after >= today else ' worse'}' style='left:{left:.2f}%;width:{width:.2f}%'></div>"
            f"<div class='tick one' style='left:{at(1):.2f}%'></div><div class='lab low' style='left:{at(1):.2f}%'>1.00x</div>"
            f"<div class='tick' style='left:{at(target):.2f}%'></div><div class='lab high' style='left:{at(target):.2f}%'>{_times(target)}</div>"
            f"<div class='mark before' style='left:{at(today):.2f}%'></div><div class='mark after' style='left:{at(after):.2f}%'></div></div>"
            f"<div class='legend'><span><i class='dot before'></i>Today {_times(today)}</span><span><i class='dot after'></i>With the plan {_times(after)}</span></div>"
            f"<p class='strong'>{_esc(need)}</p>"
            f"<p class='fine'>EBITDA over annual debt service of {_usd(debt)}{', on the saved loan terms' if plan.basis == 'with_terms' else ''}.</p></div>")


def _goal(plan: _Plan) -> str:
    """Progress toward the annual NOI goal staff set; nothing until one is set."""
    goal = (plan.result.get("goal") or {}).get("amount")
    if not plan.available or not _finite(goal) or not goal:
        return ""
    now, proposed, reviewed = _num(plan.baseline.get("noi")), _num(plan.proposed.get("noi")), _num(plan.reviewed.get("noi"))
    values = (now, proposed, reviewed, goal)
    low, high = min(0.0, *values), max(1.0, *values)

    def at(value: float) -> float:
        return 4 + (value - low) / (high - low) * 92

    stored = (plan.result.get("goal") or {}).get("proposed_gap")
    gap = stored if _finite(stored) else goal - proposed
    left, width = min(at(now), at(proposed)), abs(at(proposed) - at(now))
    rows = "".join(f"<tr><td><i class='dot {dot}'></i>{label}</td><td class='num{_signed_class(value)}'>{_usd(value)}</td></tr>"
                   for dot, label, value in (("before", "Current NOI", now), ("after", "With the plan", proposed), ("mid", "Reviewed changes only", reviewed), ("goal", "Goal", goal)))
    return ("<div class='card'><div class='cardhead split'><span class='label'>Annual NOI goal</span><span>"
            + _tag(f"{'Still to find' if gap > 0 else 'Above goal by'} {_usd(abs(gap))}", "warn" if gap > 0 else "ok") + "</span></div>"
            f"<p class='big'><b>{max(0, int(Decimal(str(proposed / goal * 100)).quantize(Decimal(1), rounding=ROUND_HALF_UP)))}%</b> of the {_usd(goal)} goal with the plan</p>"
            "<div class='scale short'><div class='track'></div>"
            f"<div class='fill' style='left:{left:.2f}%;width:{width:.2f}%'></div>"
            f"<div class='tick one' style='left:{at(0):.2f}%'></div><div class='tick' style='left:{at(goal):.2f}%'></div>"
            f"<div class='mark before' style='left:{at(now):.2f}%'></div><div class='mark mid' style='left:{at(reviewed):.2f}%'></div>"
            f"<div class='mark after' style='left:{at(proposed):.2f}%'></div></div>"
            f"<table class='mini'><tbody>{rows}</tbody></table></div>")


def _ramp(plan: _Plan) -> str:
    """Monthly EBITDA over the first twelve months as each change takes effect, against the monthly payment."""
    months = (plan.result.get("months") or {}).get("proposed") or []
    if not plan.available or not months:
        return ""
    up_height, down_height, foot = 112.0, 28.0, 13.0
    pay = plan.plan_debt / 12 if _finite(plan.plan_debt) and plan.plan_debt else None
    scale = max(pay or 0.0, *(abs(_num(month.get("ebitda"))) for month in months), 1.0) * 1.08
    columns = []
    for index, month in enumerate(months):
        value = _num(month.get("ebitda"))
        if value >= 0:
            height = value / scale * up_height
            drawn = (f"<div class='bar' style='bottom:{down_height + foot:.1f}px;height:{height:.1f}px'></div>"
                     f"<div class='val' style='bottom:{down_height + foot + height + 2:.1f}px'>{_usd(value)}</div>")
        else:
            height = min(0.25, -value / scale) * up_height
            drawn = (f"<div class='bar neg' style='top:{up_height:.1f}px;height:{height:.1f}px'></div>"
                     f"<div class='val neg' style='top:{up_height + height + 1:.1f}px'>{_usd(value)}</div>")
        columns.append(f"<div class='col'>{drawn}<div class='mon'>{_esc(_month(plan.plan_start, index, short=True))}</div></div>")
    payment = "" if pay is None else f"<div class='pay' style='top:{up_height - pay / scale * up_height:.1f}px'></div>"
    legend = ("<div class='legend'><span><i class='dot after'></i>Monthly EBITDA</span>"
              + (f"<span><i class='dash'></i>Monthly debt payment {_usd(pay)}</span>" if pay is not None else "") + "</div>")
    first = (plan.result.get("first_year") or {}).get("proposed") or {}
    return (f"<div class='ramp' style='height:{up_height + down_height + foot:.0f}px'>{''.join(columns)}"
            f"<div class='axis' style='top:{up_height:.1f}px'></div>{payment}</div>{legend}"
            f"<p class='strong'>First year {_usd(first.get('ebitda'))} · full-year run rate {_usd(plan.proposed.get('ebitda'))}</p>"
            "<p class='fine'>Monthly EBITDA as each change takes effect, after one-time setup costs in the month they fall."
            + (" Bars above the dashed line cover the monthly debt payment." if pay is not None else "") + "</p>")


# ---------------------------------------------------------------------------
# The six parts
# ---------------------------------------------------------------------------

def _part(number: int, lead: str, content: str) -> str:
    title = PARTS[number - 1]
    return (f"<section class='part'><header><div class='eyebrow'>Part {number} of {len(PARTS)}</div><h2>{_esc(title)}</h2>"
            f"<p class='lead'>{_esc(lead)}</p></header>{content}</section>")


def _no_figures(plan: _Plan) -> str:
    reason = plan.baseline.get("reason") or "This version was saved before the file had a filed business return or a profit and loss statement in use."
    return (f"<div class='notice warn'><b>No figures were stored with this version.</b> {_esc(reason)} "
            "The plan as written is printed below; its earnings and coverage could not be worked out.</div>")


def _tiles(plan: _Plan) -> str:
    tiles = []
    for key, label in SIX:
        before, after, reviewed = plan.today.get(key), plan.after.get(key), plan.after_reviewed.get(key)
        known = _finite(before) and _finite(after)
        delta = after - before if known else None
        chip = _tag(_six_change(key, delta), _tone(delta, lower_is_better=key == "debt_service")) if known else _tag(DASH)
        loss = known and key != "debt_service" and after < 0
        tiles.append(f"<div class='tile'><div class='top split'><span class='label'>{label}</span><span>{chip}</span></div>"
                     f"<div class='main'><span class='before'>{_six(key, before)}</span><span class='arrow'>{ARROW}</span>"
                     f"<span class='after{' neg' if loss else ''}'>{_six(key, after)}</span></div>"
                     f"<div class='sub'>Reviewed changes only <b>{_six(key, reviewed)}</b></div></div>")
    return f"<div class='tiles'>{''.join(tiles)}</div>"


def _cover(plan: _Plan) -> str:
    status = "Client submission · awaiting staff review" if plan.client_submission else "Saved by staff"
    issue = f"<span class='issue'>Version {_esc(plan.version)}</span>" if plan.version else ""
    stamp = " · ".join(part for part in (f"Saved {plan.saved}" if plan.saved else "", status) if part)
    counted = sum(1 for item in plan.items if item.get("included"))
    facts = "".join(f"<div class='fact'><span>{label}</span><b>{_esc(value)}</b></div>" for label, value in (
        ("Primary source", plan.source.get("label") or "No earnings source in use"), ("Reporting period", plan.period if plan.available else DASH),
        ("Plan starts", _month(plan.plan_start, 0) if plan.plan_start else "Not set"), ("Currency and basis", plan.currency_basis)))
    if plan.available:
        basis = ("After figures are measured against the saved loan terms." if plan.basis == "with_terms"
                 else "No loan terms are saved, so after figures are measured against today's debt payments.")
        figures = (_tiles(plan)
                   + f"<p class='fine'>With the plan counts every change that is switched on and not rejected ({_count(counted, 'change')}), at its full-year run rate. {basis} "
                     f"Reviewed changes only counts what staff has reviewed.{_esc(plan.loan_costs_note)}</p>"
                   + f"<div class='two'>{_coverage(plan)}{_goal(plan)}</div>"
                   + f"<div class='card'><div class='cardhead'><span class='label'>How the plan adds up</span></div>{_bridge(plan, detailed=False)}</div>")
    else:
        figures = _no_figures(plan)
    contents = " · ".join(f"{index} {title}" for index, title in enumerate(PARTS, 1))
    return ("<section class='part cover'><div class='brandrow split'><span class='brand'>Qualified Commercial</span>"
            f"<span class='stamp'>{issue}{' · ' if issue and stamp else ''}{_esc(stamp)}</span></div>"
            f"<h1>Operating Expenses and Earnings Plan</h1><div class='business'>{_esc(plan.business)}</div>"
            f"<div class='facts'>{facts}</div>{figures}"
            "<div class='notice'>What the business costs to run today, the changes it proposes, and what they do to earnings and debt coverage. "
            "The source figures stay as filed. Every change is a conditional proposal until staff has reviewed it; nothing here is a funding commitment."
            f"<div class='contents'>{_esc(contents)}</div></div></section>")


def _source_part(plan: _Plan) -> str:
    baseline, body = plan.baseline, plan.body
    confirmations = (body.get("source") or {}).get("confirmations") or {}
    corrections = (body.get("source") or {}).get("corrections") or {}
    if not plan.available:
        return _part(2, "The figures the plan starts from, as the source prints them.", _no_figures(plan))
    revenue, cogs, opex = baseline.get("revenue"), baseline.get("cogs"), baseline.get("operating_expenses")
    noi, ebitda = _num(baseline.get("noi")), _num(baseline.get("ebitda"))

    def step(label: str, value: str, cls: str = "", negative: bool = False) -> str:
        return _row([(label, "name"), (f"<span class='{'neg' if negative else ''}'>{value}</span>", "num")], cls)

    ladder = _table([("How the baseline is read", ""), ("Annual", "num")], [
        step("Revenue", _usd(revenue)), step(f"{MINUS} Cost of goods sold", _usd(cogs)), step(f"{MINUS} Operating expenses", _usd(opex)),
        step("= NOI", _usd(noi), "total", noi < 0), step("+ Other income, depreciation and amortization", _usd(ebitda - noi)),
        step("= EBITDA", _usd(ebitda), "total", ebitda < 0)], "ladder")
    today = plan.today
    coverage = _table([("Coverage today", ""), ("", "num")], [
        step("Annual debt service", _usd(today.get("debt_service"))),
        step("DSCR · EBITDA ÷ debt service", _times(today.get("dscr")), negative=_num(today.get("dscr")) < 0),
        step(f"Free cash flow · EBITDA {MINUS} debt service", _usd(today.get("free_cash_flow")), negative=_num(today.get("free_cash_flow")) < 0),
        step("Operating margin · NOI ÷ revenue", _percent(today.get("operating_margin")), negative=_num(today.get("operating_margin")) < 0)], "ladder")
    terms = ""
    if _finite(plan.terms_debt) and _finite(plan.current_debt):
        current_monthly = baseline.get("debt_service_current_monthly") if _finite(baseline.get("debt_service_current_monthly")) else plan.current_debt / 12
        terms_monthly = baseline.get("debt_service_with_terms_monthly") if _finite(baseline.get("debt_service_with_terms_monthly")) else plan.terms_debt / 12
        with_terms = ((plan.result.get("metrics") or {}).get("today") or {}).get("with_terms") or {}
        alone = f"{_times(today.get('dscr'))} {ARROW} {_times(with_terms.get('dscr'))}" if _finite(today.get("dscr")) and _finite(with_terms.get("dscr")) else DASH
        costs = [("Operating costs the loan adds", f"{_usd(plan.loan_operating)} / yr", "")] if plan.loan_operating > 0 else []
        terms = ("<div class='card terms'><div class='cardhead split'><span class='label'>Loan terms on file</span>"
                 f"<span class='muted'>{_esc(baseline.get('terms_label') or 'Saved loan terms')}</span></div><div class='facts inner'>"
                 + "".join(f"<div class='fact'><span>{label}</span><b class='{cls}'>{value}</b></div>" for label, value, cls in (
                     ("Current payments", f"{_usd(current_monthly)} / mo", ""), ("New payment", f"{_usd(terms_monthly)} / mo", ""),
                     ("Cash released", f"{_usd(baseline.get('released_monthly'))} / mo", "pos"), ("DSCR from terms alone", alone, ""), *costs))
                 + f"</div><p class='fine'>Lower payments release cash but do not change EBITDA or NOI. Earnings still have to rise for coverage to follow.{_esc(plan.loan_costs_note)}</p></div>")

    has_statement = bool(baseline.get("statement")) and any(_finite(group.get("statement_amount")) for group in plan.groups)
    statement = baseline.get("statement") or {}
    statement_head = f"{str(statement.get('label') or 'P&L').split(' · ')[0]}{' annualized' if statement.get('annualized') else ''}"
    rows, confirmed = [], 0
    for group in plan.groups:
        state = confirmations.get(group.get("key"))
        confirmed += 1 if state else 0
        mark = _tag("Correction noted", "warn") if state == "corrected" else _tag("Confirmed", "ok") if state else _tag("Not confirmed")
        other = group.get("statement_amount")
        cells = [(_esc(group.get("label") or group.get("key")), "name"), (_usd(group.get("amount")), "num")]
        if has_statement:
            cells += [(_usd(other) if _finite(other) else DASH, "num muted"), (_signed_usd(other - _num(group.get("amount"))) if _finite(other) else DASH, "num muted")]
        rows.append(_row(cells + [(mark, "state")]))
    listed = sum(_num(group.get("amount")) for group in plan.groups)
    rows.append(_row([("Costs on the source", "name"), (_usd(listed), "num")] + ([("", ""), ("", "")] if has_statement else []) + [("", "")], "total"))
    head = [("Group", ""), (plan.source_short, "num")] + ([(statement_head, "num"), ("Variance", "num")] if has_statement else []) + [("Business confirmation", "")]
    gap = _num(cogs) + _num(opex) - listed
    adds_up = ("These groups add up to cost of goods sold plus operating expenses." if abs(gap) < 1
               else f"Cost of goods sold plus operating expenses is {_usd(_num(cogs) + _num(opex))}; {_usd(abs(gap))} of it is outside these groups.")
    answers = body.get("category_answers") or {}
    questions = [_row([(_esc(row.get("label")), "name"), (_tag(ANSWERS.get(answers.get(row.get("key")), "Not answered"), "ok" if answers.get(row.get("key")) in ("included", "not_applicable") else "warn" if answers.get(row.get("key")) else "mut"), "state")])
                 for row in plan.result.get("questions") or []]
    facts = "".join(f"<div class='fact'><span>{label}</span><b>{_esc(value)}</b></div>" for label, value in (
        ("Primary source", plan.source.get("label") or DASH), ("Reporting period", plan.period), ("Currency and basis", plan.currency_basis),
        ("Source document", plan.source.get("file_name") or DASH)))
    primary = ("No filed business return is in use on this file, so the profit and loss statement stands in for it."
               if plan.source.get("kind") == "profit_and_loss" else "The filed return is primary. A profit and loss statement is shown beside it for comparison only and never replaces a line the return prints.")
    return _part(2, f"The figures the plan starts from, as the source prints them. {primary}", (
        f"<div class='facts'>{facts}</div><div class='two'><div>{ladder}</div><div>{coverage}</div></div>{terms}"
        f"<h3>Annual costs on the source <span class='count'>{confirmed} of {len(plan.groups)} confirmed by the business</span></h3>{_table(head, rows)}"
        f"<p class='fine'>{_esc(adds_up)}"
        + (f" {_count(len(corrections), 'correction')} noted by the business {'is' if len(corrections) == 1 else 'are'} listed under the tax preparer handoff in part 6; a correction never changes a source figure." if corrections else "") + "</p>"
        + (f"<h3>Costs the source prints no line for</h3>{_table([('Category', ''), ('Answer', '')], questions, 'half')}" if questions else "")))


def _model_part(plan: _Plan) -> str:
    body = plan.body
    revenue_now, revenue_then = plan.model.get("revenue") or {}, plan.proposed.get("stream_revenue") or {}
    volume_now = plan.model.get("volume") or {}
    factors_then = plan.proposed.get("factors") or {}
    rows = []
    for stream in body.get("streams") or []:
        identifier = stream.get("id")
        made, changed = [], []
        for index, factor in enumerate(stream.get("factors") or []):
            now = factor.get("value")
            then = (factors_then.get(identifier) or [])[index] if index < len(factors_then.get(identifier) or []) else now
            made.append(f"{_esc(factor.get('label') or 'Metric')} <b>{_esc(_factor_value(factor, now))}</b>")
            if _finite(now) and _finite(then) and abs(then - now) > 1e-9:
                changed.append(f"{_esc(factor.get('label') or 'Metric')} <b>{_esc(_factor_value(factor, then))}</b>")
        volume = volume_now.get(identifier)
        word = plan.unit_word(stream)
        per = f" · {_usd(_num(revenue_now.get(identifier)) / volume, 2)} revenue per {_esc(word)}" if _finite(volume) and volume else ""
        rows.append(_row([
            (f"<b>{_esc(stream.get('name') or 'Revenue stream')}</b><br><small>{_esc(plan.stream_type(stream))}"
             + (f" · {_plain(volume, 0)} a year{per}" if _finite(volume) else "") + "</small>", "name"),
            ("<br>".join(made) or DASH, ""), ("<br>".join(changed) or "<span class='muted'>No change</span>", ""),
            (_usd(revenue_now.get(identifier)), "num"), (_usd(revenue_then.get(identifier)) if plan.available else DASH, "num")]))
    if rows:
        rows.append(_row([("Revenue model", "name"), ("", ""), ("", ""), (_usd(plan.model.get("total")), "num"), (_usd(plan.proposed.get("revenue")) if plan.available else DASH, "num")], "total"))
    name = plan.baseline.get("source_name") or "source"
    if not plan.available:
        match = _tag("No source revenue to match", "mut")
    elif plan.model.get("reconciles"):
        match = _tag(f"Reconciles to {name} revenue", "ok")
    else:
        match = _tag(f"Differs from {name} revenue by {_signed_usd(plan.model.get('difference'))}", "warn")
    streams = (_table([("Revenue stream", ""), ("How it is made today", ""), ("With the plan", ""), ("Today a year", "num"), ("Plan a year", "num")], rows, "streams")
               or "<p class='muted'>No revenue stream is described in this plan.</p>")
    reconcile = (f"<p class='fine'>Model total {_usd(plan.model.get('total'))} against {_esc(name)} revenue {_usd(plan.baseline.get('revenue'))}. "
                 "A stream's revenue is its metrics multiplied together over twelve months.</p>") if plan.available else ""

    behaviors, rates, then_lines = plan.result.get("behaviors") or {}, plan.result.get("rates") or {}, plan.proposed.get("lines") or {}

    def follows(line: dict) -> str:
        effective, rate = behaviors.get(line.get("id"), line.get("behavior") or "fixed"), _num(rates.get(line.get("id")))
        stream = plan.streams.get(line.get("link"))
        if effective == "unit":
            return f"${_grouped(rate, 2)} per {plan.unit_word(stream)} of {(stream or {}).get('name') or 'its stream'}"
        if effective == "pct":
            return f"{_grouped(rate * 100, 1)}% of {'all revenue' if line.get('link') == ALL else ((stream or {}).get('name') or 'its stream') + ' revenue'}"
        return "Fixed amount" if (line.get("behavior") or "fixed") == "fixed" else "Driver is zero · held fixed"

    def cost_rows(in_cogs: bool) -> tuple[list[str], float, float]:
        out, now_total, then_total = [], 0.0, 0.0
        for line in body.get("lines") or []:
            if (line.get("group") == COGS_GROUP) != in_cogs:
                continue
            base = _num(line.get("base"))
            then = _num(then_lines.get(line.get("id")), base) if plan.available else base
            now_total, then_total = now_total + base, then_total + then
            group = plan.group_labels.get(line.get("group")) or line.get("group") or ""
            label = line.get("label") or group
            moved = then - base
            out.append(_row([
                (_esc(label) + (f"<br><small>Part of {_esc(group)}</small>" if line.get("added") and group and group != label else ""), "name"), (_esc(follows(line)), "muted"),
                (_usd(base), "num"), (_usd(then) if plan.available else DASH, "num"),
                (f"<span class='{'muted' if abs(moved) < 1 else 'neg' if moved > 0 else 'pos'}'>{_signed_usd(moved) if plan.available else DASH}</span>", "num")]))
        return out, now_total, then_total

    def subtotal(label: str, now: float, then: float) -> str:
        return _row([(label, "name"), ("", ""), (_usd(now), "num"), (_usd(then) if plan.available else DASH, "num"), (_signed_usd(then - now) if plan.available else DASH, "num")], "total")

    direct, direct_now, direct_then = cost_rows(True)
    running, running_now, running_then = cost_rows(False)
    spend = _num(plan.proposed.get("activity_spend")) if plan.available else 0.0
    lines = []
    if direct:
        lines += [_row([("Cost of goods sold", "name")] + [("", "")] * 4, "band")] + direct + [subtotal("Cost of goods sold", direct_now, direct_then)]
    if running or spend:
        lines += [_row([("Operating expenses", "name")] + [("", "")] * 4, "band")] + running
        if spend:
            lines.append(_row([("Growth spending and new activities<br><small>Added by the plan; listed in part 4</small>", "name"), ("New with the plan", "muted"),
                               (_usd(0), "num"), (_usd(spend), "num"), (f"<span class='neg'>{_signed_usd(spend)}</span>", "num")]))
        lines.append(subtotal("Operating expenses", running_now, running_then + spend))
    costs = (_table([("Cost", ""), ("How it behaves", ""), ("Today a year", "num"), ("Plan a year", "num"), ("Change", "num")], lines, "costs")
             or "<p class='muted'>The plan lists no cost lines.</p>")
    return _part(3, "How the business makes its revenue, and how each cost behaves when revenue moves: fixed, per unit sold, or a share of revenue.", (
        f"<h3>Revenue model {match}</h3>{streams}{reconcile}<h3>Current costs and how they follow revenue</h3>{costs}"
        "<p class='fine'>A cost that follows a stream moves with that stream's volume or revenue, so a change to a revenue metric carries its linked costs with it.</p>"))


def _what(plan: _Plan, row: dict, kind: str) -> str:
    """What a change does, in words: its direction, its size and where it lands."""
    if kind == "growth":
        stream = plan.streams.get(row.get("stream_id")) or {}
        factor = next((item for item in stream.get("factors") or [] if item.get("id") == row.get("factor_id")), {})
        return (f"Spend {_usd(row.get('monthly_spend'))} a month to add {_plain(row.get('added_per_month'))} {str(factor.get('label') or 'units').lower()} a month "
                f"in {stream.get('name') or 'a revenue stream'}")
    if kind == "activity":
        monthly = row.get("monthly_amount")
        return f"New cost of {_usd(monthly)} a month ({_usd(_num(monthly) * 12)} a year)"
    target = row.get("target") or {}
    verb = "Increase" if row.get("direction") == "up" else "Decrease"
    value, percent = row.get("value"), row.get("mode") == "percent"
    then_lines = plan.proposed.get("lines") or {}
    if target.get("kind") == "factor":
        stream = plan.streams.get(target.get("stream_id")) or {}
        factors = stream.get("factors") or []
        index = next((position for position, item in enumerate(factors) if item.get("id") == target.get("factor_id")), None)
        factor = factors[index] if index is not None else {}
        unit = str(factor.get("unit") or "")
        size = f"{_plain(value)}%" if percent else f"${_grouped(_num(value), 2)}" if unit == "$" else f"{_plain(value)} pts" if unit == "%" else f"{_plain(value)} {unit.split(' ')[0]}".strip()
        after = (plan.proposed.get("factors") or {}).get(target.get("stream_id")) or []
        then = after[index] if index is not None and index < len(after) else None
        moved = f" · {_factor_value(factor, factor.get('value'))} {ARROW} {_factor_value(factor, then)}" if _finite(then) else ""
        return f"{verb} by {size}{moved}"
    if target.get("kind") == "line":
        line = plan.lines.get(target.get("line_id")) or {}
        behavior = (plan.result.get("behaviors") or {}).get(line.get("id"), line.get("behavior") or "fixed")
        size = f"{_plain(value)}%" if percent else f"{_usd(value)} a year" if behavior == "fixed" else f"${_grouped(_num(value), 2)} a unit" if behavior == "unit" else f"{_plain(value)} pts"
        then = then_lines.get(line.get("id"))
        if not _finite(then):
            moved = ""
        elif behavior == "fixed":
            moved = f" · {_usd(line.get('base'))} {ARROW} {_usd(then)} a year"
        else:
            # Its plan figure also carries the volume it follows, so it is not a from-and-to of this change alone.
            moved = f" · {_usd(then)} a year in the plan, with the volume it follows"
        return f"{verb} by {size}{moved}"
    if target.get("kind") == "group":
        members = [line for line in plan.body.get("lines") or [] if line.get("group") == target.get("group")]
        now = sum(_num(line.get("base")) for line in members)
        then = sum(_num(then_lines.get(line.get("id")), _num(line.get("base"))) for line in members)
        size = f"{_plain(value)}%" if percent else f"{_usd(value)} a year"
        return f"{verb} by {size} across {_count(len(members), 'item')} · {_usd(now)} {ARROW} {_usd(then)} a year"
    size = f"{_plain(value)}%" if percent else f"{_usd(value)} a year"
    return f"{verb} prices by {size} across all revenue streams; volume unchanged"


def _changes_part(plan: _Plan) -> str:
    body = plan.body
    kinds = {str(row.get("id")): kind for block, kind in (("changes", "change"), ("growth", "growth"), ("activities", "activity")) for row in body.get(block) or []}
    by_id = {item.get("id"): item for item in plan.items}
    rows = []
    for section in plan.result.get("sections") or []:
        listed = []
        for identifier in section.get("items") or []:
            row, item, effect = plan.rows.get(str(identifier)) or {}, by_id.get(identifier) or {}, plan.effects.get(identifier) or {}
            label, tone = plan.status(item)
            off = not item.get("included", True)
            moves = effect.get("moves") or []
            # What else a change moves is its logic: shown where it moves more than its own line.
            logic = (" · ".join(f"{move.get('label')} {_signed_usd(move.get('amount'))}" for move in moves[:4]) + (f" · {len(moves) - 4} more" if len(moves) > 4 else "")
                     if len(moves) > 1 else "")
            setup = f" · setup {_usd(row.get('setup_cost'))} {'a month' if row.get('setup_cadence') == 'monthly' else 'one time'}" if _num(row.get("setup_cost")) > 0 else ""
            why = _esc(str(row.get("note") or "").strip()) or "<span class='muted'>No explanation given.</span>"
            extra = "".join(f"<br><small>{name}: {_esc(str(row.get(key)).strip())}</small>" for name, key in (("Evidence", "evidence_note"), ("Responsible", "owner")) if str(row.get(key) or "").strip())
            dscr = _signed_times(_num(effect.get("ebitda")) / plan.plan_debt) if plan.plan_debt and _finite(effect.get("ebitda")) else DASH
            listed.append(_row([
                (f"<b>{_esc(plan.labels.get(identifier) or item.get('label') or 'Change')}</b><br><span class='what'>{_esc(_what(plan, row, kinds.get(str(identifier), 'change')) + setup)}</span>"
                 f"<div class='why'>{why}{extra}</div>" + (f"<small>Moves {_esc(logic)}</small>" if logic else ""), "name"),
                (_esc(_month(plan.plan_start, int(_num(item.get("start_month") or row.get("start_month"), 1)) - 1)), ""),
                (f"<span class='{'muted' if off else 'pos' if _num(effect.get('ebitda')) > 0 else 'neg' if _num(effect.get('ebitda')) < 0 else 'muted'}'>{_signed_usd(effect.get('ebitda'))}</span>", "num"),
                (_signed_usd(effect.get("first_year")), "num"), (dscr, "num"), (_tag(label, tone), "state")], "off" if off else ""))
        if listed:
            total = _num(section.get("ebitda"))
            rows.append(f"<tr class='band'><td colspan='2'>{_esc(section.get('label'))}</td><td colspan='4' class='num'>"
                        f"<span class='{'muted' if abs(total) < 1 else 'pos' if total > 0 else 'neg'}'>EBITDA {_signed_usd(total)} a year</span></td></tr>")
            rows += listed
    changes = _table([("Change and why it will work", ""), ("Starts", ""), ("EBITDA a year", "num"), ("First year", "num"), ("DSCR", "num"), ("Status", "")], rows, "changes")
    released = plan.result.get("released") or {}
    uses = [((f"{_esc(row.get('label') or 'Use not named')} <small>· {_esc(PURPOSES.get(row.get('purpose'), 'Other'))}</small>"
              + (f"<br><small>{_esc(row.get('notes'))}</small>" if str(row.get("notes") or "").strip() else ""), "name"),
             (f"{_usd(row.get('monthly_amount'))} / mo", "num")) for row in body.get("allocations") or []]
    allocations = [_row(list(uses[index]) + list(uses[index + 1] if index + 1 < len(uses) else (("", ""), ("", "")))) for index in range(0, len(uses), 2)]
    cash = ""
    if allocations or _num(released.get("available_monthly")) > 0:
        over = bool(released.get("over_allocated"))
        left = _num(released.get("available_monthly")) - _num(released.get("allocated_monthly"))
        cash = (f"<div class='{'keep' if len(allocations) <= 8 else ''}'><h3>Released cash and reinvestment {_tag(f'Over-assigned by {_usd(-left)} / mo', 'bad') if over else _tag(f'{_usd(left)} / mo stays as cash')}</h3>"
                "<p class='fine'>" + (f"The saved loan terms release {_usd(released.get('available_monthly'))} a month; {_usd(released.get('allocated_monthly'))} a month is assigned below. "
                                      if plan.basis == "with_terms" else
                                      f"No loan terms are saved on this file, so no cash is released to assign; {_usd(released.get('allocated_monthly'))} a month is listed below. ")
                + "Assignments show where released cash goes. They do not change NOI.</p>"
                + _table([("Use", ""), ("Monthly", "num"), ("Use", ""), ("Monthly", "num")], allocations, "uses") + "</div>")
    carried = [_row([(f"{_esc(row.get('label'))}<br><small>{_esc(row.get('detail'))}</small>", "name"),
                     (_signed_usd(row.get("annual_amount")) if _finite(row.get("annual_amount")) else "Not stated", "num")]) for row in body.get("carried_over") or [] if not row.get("resolved")]
    waiting = (("<h3>Carried over from the earlier plan</h3><p class='fine'>Kept for re-entry. These are not counted in any figure in this document.</p>"
                + _table([("Item", ""), ("Annual effect stated", "num")], carried)) if carried else "")
    note = ("<p class='fine'>A change's own figure is what the plan would lose without it. Changes interact (a higher price also raises card fees), "
            "so those figures do not add up to the plan's total; the bridge in part 5 does. DSCR is the change's EBITDA over the plan's annual debt service.</p>") if changes else ""
    return _part(4, "Every proposed change: what moves, by how much, from when, and the reason it is expected to work.",
                 (changes or "<p class='muted'>No changes proposed.</p>") + note + cash + waiting)


def _before_after_part(plan: _Plan) -> str:
    if not plan.available:
        return _part(5, "Today against the plan at its full-year run rate.", _no_figures(plan))
    baseline, proposed, reviewed = plan.baseline, plan.proposed, plan.reviewed
    first = plan.result.get("first_year") or {}

    def money(label: str, now, then, checked, *, cost: bool = False, cls: str = "") -> str:
        delta = _num(then) - _num(now)
        tone = "muted" if abs(delta) < 0.5 else ("neg" if delta > 0 else "pos") if cost else ("pos" if delta > 0 else "neg")
        return _row([(label, "name"), (f"<span class='{_signed_class(now).strip()}'>{_usd(now)}</span>", "num"),
                     (f"<span class='{_signed_class(then).strip()}'>{_usd(then)}</span>", "num"), (f"<span class='{tone}'>{_signed_usd(delta)}</span>", "num"),
                     (_usd(checked), "num muted")], cls)

    def six(key: str, label: str) -> str:
        now, then, checked = plan.today.get(key), plan.after.get(key), plan.after_reviewed.get(key)
        delta = then - now if _finite(now) and _finite(then) else None
        tone = {"ok": "pos", "bad": "neg", "mut": "muted"}[_tone(delta, lower_is_better=key == "debt_service")]
        return _row([(label, "name"), (f"<span class='{'neg' if _num(now) < 0 else ''}'>{_six(key, now)}</span>", "num"),
                     (f"<span class='{'neg' if _num(then) < 0 else ''}'>{_six(key, then)}</span>", "num"),
                     (f"<span class='{tone}'>{_six_change(key, delta)}</span>", "num"), (_six(key, checked), "num muted")])

    rows = [money("Revenue", baseline.get("revenue"), proposed.get("revenue"), reviewed.get("revenue")),
            money(f"{MINUS} Cost of goods sold", baseline.get("cogs"), proposed.get("cogs"), reviewed.get("cogs"), cost=True),
            money(f"{MINUS} Operating expenses", baseline.get("operating_expenses"), proposed.get("opex"), reviewed.get("opex"), cost=True),
            money("= NOI", baseline.get("noi"), proposed.get("noi"), reviewed.get("noi"), cls="total"),
            money("= EBITDA", baseline.get("ebitda"), proposed.get("ebitda"), reviewed.get("ebitda"), cls="total"),
            six("debt_service", "Annual debt service"), six("dscr", "DSCR"), six("free_cash_flow", "Free cash flow"), six("operating_margin", "Operating margin"),
            money("First-year EBITDA", baseline.get("ebitda"), (first.get("proposed") or {}).get("ebitda"), (first.get("reviewed") or {}).get("ebitda")),
            money("First-year NOI", baseline.get("noi"), (first.get("proposed") or {}).get("noi"), (first.get("reviewed") or {}).get("noi"))]
    table = _table([("Annual figure", ""), ("Today", "num"), ("With the plan", "num"), ("Change", "num"), ("Reviewed changes only", "num")], rows, "compare")
    basis = ("Debt service, DSCR and free cash flow after the plan use the saved loan terms." + plan.loan_costs_note if plan.basis == "with_terms"
             else "No loan terms are saved, so debt service, DSCR and free cash flow use today's debt payments.")
    return _part(5, "Today against the plan at its full-year run rate, how the changes add up to it, and how the first twelve months build toward it.", (
        f"{table}<p class='fine'>{_esc(basis)} First-year figures count each change from its start month and include one-time setup costs.</p>"
        f"<h3>EBITDA bridge</h3>{_bridge(plan, detailed=True)}"
        "<p class='fine'>The bridge applies the steps in this order and adds up exactly to the plan's EBITDA.</p>"
        f"<div class='keep'><h3>First twelve months</h3>{_ramp(plan)}</div>"))


def _review_part(plan: _Plan) -> str:
    body = plan.body
    section_of = {identifier: section.get("label") for section in plan.result.get("sections") or [] for identifier in section.get("items") or []}
    counts = {"Reviewed": 0, "Proposed": 0, "Rejected": 0, "Switched off": 0}
    rows = []
    by_id = {item.get("id"): item for item in plan.items}
    order = [identifier for section in plan.result.get("sections") or [] for identifier in section.get("items") or [] if identifier in by_id]
    for item in [by_id[identifier] for identifier in order] + [item for item in plan.items if item.get("id") not in order]:
        label, tone = plan.status(item)
        counts[label] += 1
        rows.append(_row([(_esc(plan.labels.get(item.get("id")) or item.get("label") or "Change"), "name"), (_esc(section_of.get(item.get("id")) or ""), "muted"),
                          (_signed_usd((plan.effects.get(item.get("id")) or {}).get("ebitda")), "num"), (_tag(label, tone), "state")]))
    summary = " · ".join(f"{amount} {label.lower()}" for label, amount in counts.items() if amount) or "No changes"
    reviewed_line = ""
    if plan.available and plan.items:
        reviewed_line = (f"<p>Counting reviewed changes only, EBITDA is <b>{_usd(plan.reviewed.get('ebitda'))}</b> a year"
                         + (f" and DSCR is <b>{_times(plan.after_reviewed.get('dscr'))}</b>" if _finite(plan.after_reviewed.get("dscr")) else "")
                         + f", against <b>{_usd(plan.proposed.get('ebitda'))}</b>"
                         + (f" and <b>{_times(plan.after.get('dscr'))}</b>" if _finite(plan.after.get("dscr")) else "") + " with every proposed change.</p>")
    status = ("<p class='fine'>This version was submitted by the business and is awaiting staff review, so no change in it has been reviewed.</p>" if plan.client_submission
              else "<p class='fine'>Staff review each change before it counts toward the reviewed figures. A reviewed change is accepted for projection only; it is not a funding commitment.</p>")
    review = (f"<h3>Review status <span class='count'>{_esc(summary)}</span></h3>{reviewed_line}"
              + (_table([("Change", ""), ("Part of the plan", ""), ("EBITDA a year", "num"), ("Status", "")], rows) or "<p class='muted'>No changes proposed.</p>") + status)

    corrections = (body.get("source") or {}).get("corrections") or {}
    groups = {group.get("key"): group for group in plan.groups}
    handoff = [_row([(f"<b>{_esc((groups.get(key) or {}).get('label') or key)}</b><br><small>{_esc(row.get('reason'))}</small>", "name"),
                     (_usd((groups.get(key) or {}).get("amount")), "num"), (_usd(row.get("amount")), "num")]) for key, row in corrections.items()]
    preparer = ("<h3>Tax preparer handoff</h3><p class='fine'>Historical source corrections stay separate from future plans. Share this document with the tax preparer for review. "
                "It does not file or amend a return, and a correction never changes a source figure or raises verified income.</p>"
                + (_table([("Difference the business recorded", ""), ("On the source", "num"), ("Business says", "num")], handoff) or "<p class='muted'>No historical differences recorded.</p>"))

    confirmed = ("The business confirmed these disclosures and proposals." if body.get("attestation") else "The disclosures and proposals have not been confirmed by the business.")
    notes = str(body.get("notes") or "").strip()
    confirmation = (f"<h3>Confirmation and notes</h3><p>{_tag('Confirmed', 'ok') if body.get('attestation') else _tag('Not confirmed', 'warn')} {confirmed}</p>"
                    + (f"<div class='notes'>{_esc(notes)}</div>" if notes else "<p class='muted'>No plan or handoff notes.</p>"))

    stamp = str(plan.result.get("source_stamp") or (body.get("source") or {}).get("stamp") or "")
    method = ("<h3>How the figures are worked out</h3><div class='method'>"
              f"<p><b>NOI</b> = revenue {MINUS} cost of goods sold {MINUS} operating expenses. <b>EBITDA</b> = NOI + other income + depreciation and amortization, as the source prints them.</p>"
              f"<p><b>DSCR</b> = EBITDA ÷ annual debt service. <b>Free cash flow</b> = EBITDA {MINUS} annual debt service. <b>Operating margin</b> = NOI ÷ revenue.{_esc(plan.loan_costs_note)}</p>"
              "<p><b>With the plan</b> counts every change that is switched on and not rejected, at its full-year run rate. <b>First year</b> counts each change from its start month and one-time setup costs when they fall.</p>"
              "<p>The figures in this document were fixed when this version was saved. A later change to the file or to the plan is not reflected here; it appears in the next saved version.</p>"
              + (f"<p class='fine'>Source figures reference {_esc(stamp[:16])}</p>" if stamp else "") + "</div>")
    return _part(6, "Where each change stands with staff, what the business confirmed, and what to hand to the tax preparer.", review + confirmation + preparer + method)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

CSS = """
@page { size: Letter; margin: 56px 46px 50px;
  @top-left { content: "QUALIFIED COMMERCIAL"; font: bold 6.4px "DejaVu Sans", Arial, sans-serif; letter-spacing: 0.5px; color: #1b4b9e; vertical-align: bottom; padding-bottom: 16px; }
  @top-right { content: string(part); font: 7px "DejaVu Sans", Arial, sans-serif; color: #5a6675; vertical-align: bottom; padding-bottom: 16px; }
  @bottom-left { content: string(owner) " · Operating expenses and earnings plan" string(issue); font: 7px "DejaVu Sans", Arial, sans-serif; color: #5a6675; vertical-align: top; padding-top: 14px; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 7px "DejaVu Sans", Arial, sans-serif; color: #5a6675; vertical-align: top; padding-top: 14px; }
}
@page :first { margin-top: 42px; @top-left { content: none; } @top-right { content: none; } }
html { font: 9px/1.42 "DejaVu Sans", Arial, Helvetica, sans-serif; color: #0f1720; }
body { margin: 0; }
h1 { font-size: 23px; line-height: 1.15; margin: 14px 0 3px; letter-spacing: -0.3px; }
h2 { font-size: 16px; line-height: 1.2; margin: 3px 0 4px; letter-spacing: -0.2px; }
h3 { font-size: 10.5px; margin: 14px 0 6px; break-after: avoid; }
h3 .tag, h3 .count { margin-left: 6px; vertical-align: 1px; }
h3 .count { font-size: 7.6px; font-weight: normal; color: #5a6675; }
p { margin: 4px 0; }
b { font-weight: bold; }
small { font-size: 7.6px; color: #5a6675; }
.muted { color: #5a6675; } .neg { color: #b42318; } .pos { color: #0f7b4f; }
.fine { font-size: 7.6px; line-height: 1.45; color: #5a6675; }
.strong { font-weight: bold; margin: 5px 0 2px; }
.part { break-before: page; } .part.cover { break-before: auto; }
.keep { break-inside: avoid; }
.eyebrow { font-size: 7px; font-weight: bold; letter-spacing: 0.55px; text-transform: uppercase; color: #1b4b9e; }
.part h2 { string-set: part content(text); }
.lead { color: #5a6675; margin: 0 0 10px; max-width: 590px; }
.brand { font-size: 8px; font-weight: bold; letter-spacing: 0.6px; text-transform: uppercase; color: #1b4b9e; }
.stamp { font-size: 7.6px; color: #5a6675; }
.issue { color: #0f1720; font-weight: bold; string-set: issue " · " content(text); }
.business { font-size: 12.5px; font-weight: bold; color: #353e4a; string-set: owner content(text); }
.facts { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0 14px; border-top: 1px solid #dde1e6; border-bottom: 1px solid #dde1e6; padding: 8px 0; margin: 11px 0 13px; }
.facts.inner { border: 0; padding: 0; margin: 6px 0 4px; row-gap: 7px; }
.fact span { display: block; font-size: 6.8px; letter-spacing: 0.6px; text-transform: uppercase; color: #5a6675; }
.fact b { display: block; margin-top: 2px; font-size: 9.2px; }
.tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.tile { border: 1px solid #dde1e6; border-radius: 6px; padding: 9px 11px 8px; break-inside: avoid; }
.split { display: table; width: 100%; }
.split > * { display: table-cell; vertical-align: middle; }
.split > *:last-child { text-align: right; }
.cardhead { margin-bottom: 5px; }
.label { font-size: 7px; font-weight: bold; letter-spacing: 0.55px; text-transform: uppercase; color: #5a6675; }
.tile .main { margin: 7px 0 5px; white-space: nowrap; }
.tile .before { font-size: 9px; color: #5a6675; }
.tile .arrow { color: #8b97a6; margin: 0 4px; }
.tile .after { font-size: 17px; font-weight: bold; letter-spacing: -0.4px; }
.tile .sub { font-size: 7.2px; color: #5a6675; border-top: 1px solid #eef1f6; padding-top: 4px; }
.tile .sub b { color: #353e4a; }
.tag { display: inline-block; font-size: 6.9px; font-weight: bold; line-height: 1.3; padding: 1.5px 6px; border-radius: 99px; white-space: nowrap; }
.tag.ok { background: #e6f4ec; color: #0f7b4f; } .tag.warn { background: #fdf2e2; color: #a15c07; } .tag.bad { background: #fdeceb; color: #b42318; }
.tag.mut { background: #eef1f6; color: #4e5967; } .tag.acc { background: #eef3fb; color: #1b4b9e; }
.card { border: 1px solid #dde1e6; border-radius: 6px; padding: 10px 12px; break-inside: avoid; }
.card.terms { margin: 10px 0 0; }
.card .big { margin: 6px 0 0; color: #5a6675; }
.card .big b { font-size: 16px; color: #0f1720; letter-spacing: -0.3px; margin-right: 3px; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 10px 0; }
.two:empty { display: none; }
.notice { background: #eef3fb; border-left: 3px solid #1b4b9e; border-radius: 0 4px 4px 0; padding: 8px 11px; margin: 12px 0 0; font-size: 8.2px; break-inside: avoid; }
.notice.warn { background: #fdf2e2; border-left-color: #a15c07; margin: 0 0 12px; }
.contents { margin-top: 5px; font-size: 7.2px; color: #5a6675; }
.notes { white-space: pre-wrap; border: 1px solid #dde1e6; border-radius: 6px; padding: 8px 11px; }
.method { border-top: 1px solid #dde1e6; padding-top: 6px; font-size: 8.2px; color: #353e4a; }
table { width: 100%; border-collapse: collapse; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th { font-size: 6.8px; font-weight: bold; letter-spacing: 0.5px; text-transform: uppercase; color: #5a6675; text-align: left; vertical-align: bottom; padding: 5px 6px; border-bottom: 1px solid #cdd3da; }
td { padding: 5px 6px; border-bottom: 1px solid #e6eaef; vertical-align: top; overflow-wrap: anywhere; }
th.num, td.num { text-align: right; white-space: nowrap; }
td.state { white-space: nowrap; }
tr.total td { font-weight: bold; background: #f4f6f8; border-top: 1px solid #cdd3da; border-bottom: 0; }
tr.band td { font-size: 6.8px; font-weight: bold; letter-spacing: 0.6px; text-transform: uppercase; color: #1b4b9e; background: #eef3fb; border-bottom: 0; padding: 4px 6px; }
tbody.keep { break-inside: avoid; }
tr.band td.num { text-transform: none; letter-spacing: 0; font-size: 7.6px; }
tr.off td { color: #8b97a6; }
table.half { width: 62%; }
table.uses td { padding: 3.5px 6px; }
table.uses td.name { width: 36%; }
table.mini td { padding: 2px 0; border: 0; font-size: 8px; }
table.ladder td.name { width: 70%; }
table.compare td.name { width: 30%; }
table.streams td.name { width: 27%; } table.costs td.name { width: 30%; }
table.changes td { padding: 4px 6px; }
table.changes tr.band td { padding: 3px 6px; }
table.changes td.name { width: 51%; }
table.changes .what { color: #353e4a; }
table.changes .why { margin: 2px 0 1px; padding-left: 7px; border-left: 2px solid #b3cbee; }
table.bridge td { padding: 3.5px 6px; vertical-align: middle; }
table.bridge td.name { width: 27%; }
table.bridge td.bar { width: 43%; }
table.bridge tr.edge td { font-weight: bold; }
.barbox { position: relative; height: 9px; background: #f4f6f8; }
.barbox i { position: absolute; top: 0; bottom: 0; }
.barbox i.total { background: #1b4b9e; } .barbox i.loss, .barbox i.down { background: #b42318; } .barbox i.up { background: #0f7b4f; }
.barbox b { position: absolute; top: -2px; bottom: -2px; width: 0; border-left: 1px solid #8b97a6; }
.scale { position: relative; height: 44px; margin: 4px 0 0; }
.scale.short { height: 26px; margin-top: 2px; }
.scale .track, .scale .fill { position: absolute; top: 19px; height: 7px; }
.scale.short .track, .scale.short .fill { top: 9px; }
.scale .track { left: 0; right: 0; background: #e6eaef; border-radius: 4px; }
.scale .fill { background: #b3cbee; } .scale .fill.worse { background: #f6c9c5; }
.scale .mark { position: absolute; top: 14px; width: 3px; height: 17px; margin-left: -1.5px; border-radius: 1px; }
.scale.short .mark { top: 4px; }
.scale .mark.before { background: #8b97a6; } .scale .mark.mid { background: #7fa8e0; } .scale .mark.after { background: #1b4b9e; width: 4px; margin-left: -2px; }
.scale .tick { position: absolute; top: 12px; height: 21px; width: 0; border-left: 1px dashed #8a6a1f; }
.scale.short .tick { top: 2px; }
.scale .tick.one { border-left: 1px solid #b0b9c5; }
.scale .lab { position: absolute; width: 44px; margin-left: -22px; text-align: center; font-size: 6.6px; color: #5a6675; }
.scale .lab.high { top: 1px; color: #8a6a1f; } .scale .lab.low { top: 34px; }
.legend { font-size: 7.6px; color: #353e4a; }
.legend span { margin-right: 12px; }
.ramp + .legend { margin-top: 5px; }
.dot { display: inline-block; width: 6px; height: 6px; border-radius: 1px; margin-right: 4px; }
.dot.before { background: #8b97a6; } .dot.mid { background: #7fa8e0; } .dot.after { background: #1b4b9e; } .dot.goal { background: #8a6a1f; }
.ramp { position: relative; margin: 6px 0 2px; font-size: 0; }
.ramp .col { display: inline-block; position: relative; width: 8.333%; height: 100%; vertical-align: top; }
.ramp .bar { position: absolute; left: 20%; right: 20%; background: #1b4b9e; border-radius: 2px 2px 0 0; }
.ramp .bar.neg { background: #b42318; border-radius: 0 0 2px 2px; }
.ramp .val { position: absolute; left: 0; right: 0; text-align: center; font-size: 6.4px; color: #353e4a; }
.ramp .val.neg { color: #b42318; }
.ramp .mon { position: absolute; left: 0; right: 0; bottom: 0; text-align: center; font-size: 7px; color: #5a6675; }
.ramp .axis { position: absolute; left: 0; right: 0; height: 0; border-top: 1px solid #8b97a6; }
.ramp .pay { position: absolute; left: 0; right: 0; height: 0; border-top: 1px dashed #8a6a1f; }
.dash { display: inline-block; width: 14px; height: 0; border-top: 1px dashed #8a6a1f; vertical-align: middle; margin-right: 4px; }
"""


def build_html(*, body: dict, financial_totals: dict | None = None) -> str:
    plan = _Plan(body, financial_totals)
    title = f"Operating expenses and earnings plan · {plan.business}" + (f" · Version {plan.version}" if plan.version else "")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{_esc(title)}</title>"
            f"<meta name='author' content='Qualified Commercial'><style>{CSS}</style></head><body>"
            + _cover(plan) + _source_part(plan) + _model_part(plan) + _changes_part(plan) + _before_after_part(plan) + _review_part(plan) + "</body></html>")


def render_pdf(*, body: dict, financial_totals: dict | None = None) -> bytes:
    try:
        from weasyprint import HTML

        pdf = HTML(string=build_html(body=body, financial_totals=financial_totals)).write_pdf()
        if pdf:
            # The designed document carries its own running header and page numbers.
            return pdf
    except Exception:  # noqa: BLE001 - native PDF libraries may be unavailable locally
        log.info("Earnings plan PDF engine unavailable; drawing the plain document with PyMuPDF", exc_info=True)
    from app.services import earnings_plan_pdf_plain
    from app.services.operating_plan_pdf import _render_pymupdf, _with_footer

    pdf = _render_pymupdf(earnings_plan_pdf_plain.build_html(body=body, financial_totals=financial_totals))
    if not pdf:
        raise RuntimeError("Earnings plan PDF renderer returned no content")
    return _with_footer(pdf)
