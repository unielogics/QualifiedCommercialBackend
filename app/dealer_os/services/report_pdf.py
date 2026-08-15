"""Lender-package PDF rendering — Phase 3 Wave 2.

build_html is a PURE function turning the _build_lender_package JSON bundle
(LenderPackageRead.model_dump(mode="json")) into one compact, self-contained
HTML document; render_pdf feeds it to weasyprint. weasyprint is imported
LAZILY: the runtime docker image carries it with its native libs (pango &
friends), but a bare dev venv may not — in that case render_pdf raises
PDFUnavailableError and the endpoint answers 501 instead of crashing imports.
"""

from __future__ import annotations

import html
from typing import Any


class PDFUnavailableError(RuntimeError):
    """weasyprint (or its native libraries) is not importable in this runtime."""


def _e(v: Any) -> str:
    return html.escape(str(v)) if v is not None else "—"


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.0f}" if v is not None else "—"
    except (TypeError, ValueError):
        return "—"


def _ratio(v: Any) -> str:
    try:
        return f"{float(v):.2f}x" if v is not None else "—"
    except (TypeError, ValueError):
        return "—"


def _num(v: Any, ndigits: int = 1) -> str:
    try:
        return f"{float(v):.{ndigits}f}" if v is not None else "—"
    except (TypeError, ValueError):
        return "—"


_CSS = """
body { font-family: Helvetica, Arial, sans-serif; font-size: 10px; color: #1a2233; margin: 28px; }
h1 { font-size: 19px; margin: 0 0 2px; }
h2 { font-size: 12px; border-bottom: 1px solid #c8d0dc; padding-bottom: 3px; margin: 18px 0 6px; }
.sub { color: #5a6678; margin: 0 0 10px; }
table { width: 100%; border-collapse: collapse; margin: 4px 0 8px; }
th, td { text-align: left; padding: 3px 6px; border-bottom: 1px solid #e4e8ef; vertical-align: top; }
th { background: #f2f5f9; font-size: 9px; text-transform: uppercase; letter-spacing: .04em; color: #46536a; }
td.num, th.num { text-align: right; }
.kpis { width: 100%; margin: 6px 0 2px; }
.kpis td { border: 1px solid #dbe1ea; padding: 6px 8px; width: 20%; }
.kpis .label { font-size: 8px; text-transform: uppercase; letter-spacing: .05em; color: #5a6678; }
.kpis .value { font-size: 14px; font-weight: bold; }
.muted { color: #7a8496; }
.small { font-size: 8.5px; }
"""


def _kpi_row(snapshot: dict | None) -> str:
    m = (snapshot or {}).get("metrics") or {}
    cells = [
        ("Health score", _num((snapshot or {}).get("score"), 1)),
        ("Tier", _e((snapshot or {}).get("tier"))),
        ("Bankable EBITDA", _money((m.get("ebitda") or {}).get("bankable"))),
        ("DSCR", _ratio((m.get("dscr") or {}).get("current"))),
        ("Avg daily balance", _money((m.get("adb") or {}).get("current"))),
    ]
    tds = "".join(
        f'<td><div class="label">{_e(label)}</div><div class="value">{value}</div></td>'
        for label, value in cells
    )
    return f'<table class="kpis"><tr>{tds}</tr></table>'


def _targets_table(targets: list[dict]) -> str:
    if not targets:
        return '<p class="muted">No targets proposed yet.</p>'
    rows = "".join(
        f"<tr><td>{_e(t.get('metric_key'))}</td>"
        f"<td class='num'>{_num(t.get('effective_value'), 2)}</td>"
        f"<td>{_e(t.get('status'))}</td></tr>"
        for t in targets
    )
    return (
        "<table><tr><th>Metric</th><th class='num'>Effective target</th><th>Status</th></tr>"
        f"{rows}</table>"
    )


def _periods_table(periods: list[dict]) -> str:
    if not periods:
        return '<p class="muted">No normalized financial periods on file.</p>'
    rows = "".join(
        f"<tr><td>{_e(p.get('period'))}</td>"
        f"<td class='num'>{_money(p.get('deposits'))}</td>"
        f"<td class='num'>{_money(p.get('withdrawals'))}</td>"
        f"<td class='num'>{_money(p.get('ending_balance'))}</td>"
        f"<td class='num'>{_money(p.get('avg_daily_balance'))}</td>"
        f"<td class='num'>{_e(p.get('nsf_count'))}</td>"
        f"<td>{'yes' if p.get('reconciled') else 'no'}</td></tr>"
        for p in periods
    )
    return (
        "<table><tr><th>Month</th><th class='num'>Deposits</th><th class='num'>Withdrawals</th>"
        "<th class='num'>Ending bal.</th><th class='num'>ADB</th><th class='num'>NSF</th>"
        f"<th>Reconciled</th></tr>{rows}</table>"
    )


def _addbacks_table(addbacks: list[dict]) -> str:
    if not addbacks:
        return '<p class="muted">No add-backs identified.</p>'
    rows = "".join(
        f"<tr><td>{_e(a.get('title'))}</td><td>{_e(a.get('status'))}</td>"
        f"<td class='num'>{_money(a.get('annual_amount'))}</td></tr>"
        for a in addbacks
    )
    return (
        "<table><tr><th>Add-back</th><th>Status</th><th class='num'>Annual</th></tr>"
        f"{rows}</table>"
    )


def _plan_table(plan: list[dict]) -> str:
    if not plan:
        return '<p class="muted">No action plan yet.</p>'
    rows = "".join(
        f"<tr><td>{_e(a.get('title'))}</td><td>{_e(a.get('category'))}</td>"
        f"<td>{_e(a.get('status'))}</td><td>{_e(a.get('due_on'))}</td>"
        f"<td>{_e(a.get('expected_effect'))}</td></tr>"
        for a in plan
    )
    return (
        "<table><tr><th>Action</th><th>Category</th><th>Status</th><th>Due</th>"
        f"<th>Expected effect</th></tr>{rows}</table>"
    )


def _paths_table(paths: dict | None) -> str:
    path_rows = (paths or {}).get("paths") or []
    if not path_rows:
        return '<p class="muted">Funding-path readiness needs a metric snapshot.</p>'
    rows = "".join(
        f"<tr><td>{_e(p.get('label'))}</td>"
        f"<td class='num'>{_num(p.get('readiness_pct'), 0)}%</td>"
        f"<td class='small'>"
        + "; ".join(
            f"{'✓' if r.get('met') else '✗'} {html.escape(str(r.get('label') or ''))}"
            for r in (p.get("requirements") or [])
        )
        + "</td></tr>"
        for p in path_rows
    )
    ladder = (paths or {}).get("ladder") or {}
    ladder_note = (
        f"<p>Credit ladder position: <b>{_e(ladder.get('current_tier'))}</b></p>"
        if ladder.get("current_tier")
        else ""
    )
    return (
        "<table><tr><th>Path</th><th class='num'>Readiness</th><th>Requirements</th></tr>"
        f"{rows}</table>{ladder_note}"
    )


def _forecast_block(forecast: dict | None) -> str:
    if not forecast:
        return '<p class="muted">Forecast needs a metric snapshot.</p>'
    fundable = forecast.get("fundable_month")
    uplift = forecast.get("uplift_pct")
    bits = [
        f"Fundable month (plan scenario): <b>{_e(fundable) if fundable else 'not within 12 months'}</b>",
        f"Plan uplift vs status quo (bankable EBITDA, month 12): <b>{_num(uplift, 1)}%</b>",
    ]
    return "<p>" + "<br/>".join(bits) + "</p>"


def build_html(bundle: dict) -> str:
    """Compact print-ready HTML for one lender package bundle. Pure."""
    dealer = bundle.get("dealer") or {}
    snapshot = bundle.get("snapshot")
    as_of = (snapshot or {}).get("as_of")
    location = ", ".join(x for x in (dealer.get("city"), dealer.get("state")) if x)
    sub_bits = [b for b in (dealer.get("legal_name"), location, dealer.get("email")) if b]
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Lender package — {_e(dealer.get('name'))}</title>
<style>{_CSS}</style></head><body>
<h1>{_e(dealer.get('name'))} — Lender package</h1>
<p class="sub">{_e(' · '.join(sub_bits)) if sub_bits else ''}
{f"&nbsp;&nbsp;<span class='muted'>Snapshot as of {_e(as_of)}</span>" if as_of else "<span class='muted'>No metric snapshot yet</span>"}</p>
{_kpi_row(snapshot)}
<h2>Funding paths</h2>{_paths_table(bundle.get('paths'))}
<h2>Targets</h2>{_targets_table(bundle.get('targets') or [])}
<h2>Monthly financials</h2>{_periods_table(bundle.get('periods') or [])}
<h2>EBITDA add-backs</h2>{_addbacks_table(bundle.get('addbacks') or [])}
<h2>Action plan</h2>{_plan_table(bundle.get('plan') or [])}
<h2>12-month forecast</h2>{_forecast_block(bundle.get('forecast'))}
<p class="small muted">Generated by Qualified Commercial Dealer Capital OS. Metrics are monitoring
readouts, not a credit decision; funding-path thresholds are provisional product heuristics.</p>
</body></html>"""


def render_pdf(html_doc: str) -> bytes:
    """HTML -> PDF bytes via weasyprint. Raises PDFUnavailableError when the
    library (or its native pango/cairo stack) is missing in this runtime."""
    try:
        import weasyprint  # noqa: PLC0415 — lazy by contract (native libs)
    except Exception as exc:  # ImportError or OSError from missing shared libs
        raise PDFUnavailableError(
            "PDF rendering is unavailable in this runtime (weasyprint/pango not installed)"
        ) from exc
    return weasyprint.HTML(string=html_doc).write_pdf()
