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


def _t(locale: str, en: str, es: str) -> str:
    return es if locale == "es" else en


def _kpi_row(snapshot: dict | None, locale: str = "en") -> str:
    m = (snapshot or {}).get("metrics") or {}
    cells = [
        (_t(locale, "Health score", "Puntuación de salud"), _num((snapshot or {}).get("score"), 1)),
        (_t(locale, "Tier", "Nivel"), _e((snapshot or {}).get("tier"))),
        (_t(locale, "Bankable EBITDA", "EBITDA para financiamiento"), _money((m.get("ebitda") or {}).get("bankable"))),
        ("DSCR", _ratio((m.get("dscr") or {}).get("current"))),
        (_t(locale, "Avg daily balance", "Saldo diario promedio"), _money((m.get("adb") or {}).get("current"))),
    ]
    tds = "".join(
        f'<td><div class="label">{_e(label)}</div><div class="value">{value}</div></td>'
        for label, value in cells
    )
    return f'<table class="kpis"><tr>{tds}</tr></table>'


def _targets_table(targets: list[dict], locale: str = "en") -> str:
    if not targets:
        return f'<p class="muted">{_t(locale, "No targets proposed yet.", "Todavía no hay objetivos propuestos.")}</p>'
    rows = "".join(
        f"<tr><td>{_e(t.get('metric_key'))}</td>"
        f"<td class='num'>{_num(t.get('effective_value'), 2)}</td>"
        f"<td>{_e(t.get('status'))}</td></tr>"
        for t in targets
    )
    return (
        f"<table><tr><th>{_t(locale, 'Metric', 'Métrica')}</th><th class='num'>{_t(locale, 'Effective target', 'Objetivo efectivo')}</th><th>{_t(locale, 'Status', 'Estado')}</th></tr>"
        f"{rows}</table>"
    )


def _periods_table(periods: list[dict], locale: str = "en") -> str:
    if not periods:
        return f'<p class="muted">{_t(locale, "No normalized financial periods on file.", "No hay períodos financieros normalizados en el expediente.")}</p>'
    rows = "".join(
        f"<tr><td>{_e(p.get('period'))}</td>"
        f"<td class='num'>{_money(p.get('deposits'))}</td>"
        f"<td class='num'>{_money(p.get('withdrawals'))}</td>"
        f"<td class='num'>{_money(p.get('ending_balance'))}</td>"
        f"<td class='num'>{_money(p.get('avg_daily_balance'))}</td>"
        f"<td class='num'>{_e(p.get('nsf_count'))}</td>"
        f"<td>{_t(locale, 'yes', 'sí') if p.get('reconciled') else 'no'}</td></tr>"
        for p in periods
    )
    return (
        f"<table><tr><th>{_t(locale, 'Month', 'Mes')}</th><th class='num'>{_t(locale, 'Deposits', 'Depósitos')}</th><th class='num'>{_t(locale, 'Withdrawals', 'Retiros')}</th>"
        f"<th class='num'>{_t(locale, 'Ending bal.', 'Saldo final')}</th><th class='num'>ADB</th><th class='num'>NSF</th>"
        f"<th>{_t(locale, 'Reconciled', 'Conciliado')}</th></tr>{rows}</table>"
    )


def _addbacks_table(addbacks: list[dict], locale: str = "en") -> str:
    if not addbacks:
        return f'<p class="muted">{_t(locale, "No add-backs identified.", "No hay ajustes identificados.")}</p>'
    rows = "".join(
        f"<tr><td>{_e(a.get('title'))}</td><td>{_e(a.get('status'))}</td>"
        f"<td class='num'>{_money(a.get('annual_amount'))}</td></tr>"
        for a in addbacks
    )
    return (
        f"<table><tr><th>{_t(locale, 'Add-back', 'Ajuste')}</th><th>{_t(locale, 'Status', 'Estado')}</th><th class='num'>{_t(locale, 'Annual', 'Anual')}</th></tr>"
        f"{rows}</table>"
    )


def _plan_table(plan: list[dict], locale: str = "en") -> str:
    if not plan:
        return f'<p class="muted">{_t(locale, "No action plan yet.", "Todavía no hay un plan de acción.")}</p>'
    rows = "".join(
        f"<tr><td>{_e(a.get('title'))}</td><td>{_e(a.get('category'))}</td>"
        f"<td>{_e(a.get('status'))}</td><td>{_e(a.get('due_on'))}</td>"
        f"<td>{_e(a.get('expected_effect'))}</td></tr>"
        for a in plan
    )
    return (
        f"<table><tr><th>{_t(locale, 'Action', 'Acción')}</th><th>{_t(locale, 'Category', 'Categoría')}</th><th>{_t(locale, 'Status', 'Estado')}</th><th>{_t(locale, 'Due', 'Fecha límite')}</th>"
        f"<th>{_t(locale, 'Expected effect', 'Efecto esperado')}</th></tr>{rows}</table>"
    )


def _paths_table(paths: dict | None, locale: str = "en") -> str:
    path_rows = (paths or {}).get("paths") or []
    if not path_rows:
        return f'<p class="muted">{_t(locale, "Funding-path readiness needs a metric snapshot.", "La preparación de financiamiento requiere una evaluación de métricas.")}</p>'
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
        f"<p>{_t(locale, 'Credit ladder position', 'Posición en la escala crediticia')}: <b>{_e(ladder.get('current_tier'))}</b></p>"
        if ladder.get("current_tier")
        else ""
    )
    return (
        f"<table><tr><th>{_t(locale, 'Path', 'Opción')}</th><th class='num'>{_t(locale, 'Readiness', 'Preparación')}</th><th>{_t(locale, 'Requirements', 'Requisitos')}</th></tr>"
        f"{rows}</table>{ladder_note}"
    )


def _forecast_block(forecast: dict | None, locale: str = "en") -> str:
    if not forecast:
        return f'<p class="muted">{_t(locale, "Forecast needs a metric snapshot.", "La proyección requiere una evaluación de métricas.")}</p>'
    fundable = forecast.get("fundable_month")
    uplift = forecast.get("uplift_pct")
    bits = [
        f"{_t(locale, 'Fundable month (plan scenario)', 'Mes de preparación (escenario del plan)')}: <b>{_e(fundable) if fundable else _t(locale, 'not within 12 months', 'fuera de los 12 meses')}</b>",
        f"{_t(locale, 'Plan uplift vs status quo (bankable EBITDA, month 12)', 'Mejora del plan frente al estado actual (EBITDA, mes 12)')}: <b>{_num(uplift, 1)}%</b>",
    ]
    return "<p>" + "<br/>".join(bits) + "</p>"


def _readiness_block(readiness: dict | None, locale: str = "en") -> str:
    if not readiness:
        return ""
    score = readiness.get("score") if (readiness.get("evidence_coverage_pct") or 0) >= 60 else None
    reviewed = readiness.get("review_status") in {"confirmed", "revised"}
    band_labels = {
        "ready_soon": _t(locale, "Ready soon", "Listo pronto"),
        "three_to_six_months": "3–6 " + _t(locale, "months", "meses"),
        "six_to_twelve_months": "6–12 " + _t(locale, "months", "meses"),
        "one_plus_year": _t(locale, "1+ year", "1+ año"),
        "insufficient_evidence": _t(locale, "Insufficient evidence", "Evidencia insuficiente"),
    }
    status_labels = {
        "concerning": _t(locale, "Concerning", "Preocupante"),
        "acceptable": _t(locale, "Acceptable", "Aceptable"),
        "healthy": _t(locale, "Healthy", "Saludable"),
        "very_strong": _t(locale, "Very strong", "Muy sólido"),
        "unavailable": _t(locale, "Unavailable", "No disponible"),
    }
    rows = []
    for metric in readiness.get("metrics") or []:
        if (metric.get("source") or {}).get("not_applicable"):
            continue
        value = metric.get("value") if metric.get("status") != "unavailable" else None
        unit = metric.get("unit")
        currency = (metric.get("source") or {}).get("currency")
        rendered = (
            _money(value) if unit == "currency" and currency == "USD"
            else f"{_num(value, 0)} {_e(currency)}" if unit == "currency" and currency
            else _num(value, 0) if unit == "currency"
            else _ratio(value) if unit in {"x", "ratio"}
            else _num(value)
        )
        if value is not None and unit == "%":
            rendered += "%"
        rows.append(f"<tr><td>{_e(metric.get('label'))}</td><td class='num'>{rendered}</td><td>{_e(status_labels.get(metric.get('status'), metric.get('status')))}</td></tr>")
    phases = "".join(f"<li>{_e(phase.get('label'))}</li>" for phase in readiness.get("phases") or [])
    blockers = "".join(f"<li>{_e(item.get('label'))}: {_e(item.get('detail'))}</li>" for item in (readiness.get("blockers") or [])[:5])
    review_label = _t(locale, "QC reviewed", "Revisado por QC") if reviewed else _t(locale, "Provisional — pending QC review", "Provisional — pendiente de revisión de QC")
    return (
        f"<h2>{_t(locale, 'Capital Readiness', 'Preparación de capital')}</h2>"
        f"<p><b>{_num(score, 0)}</b> · {_e(band_labels.get(readiness.get('band')))} · {review_label}</p>"
        f"<p class='small'>{_t(locale, 'Evidence coverage', 'Cobertura de evidencia')}: {_num(readiness.get('evidence_coverage_pct'), 0)}% · {_t(locale, 'Confidence', 'Confianza')}: {_num(readiness.get('confidence_pct'), 0)}% · {_t(locale, 'Version', 'Versión')} {_e(readiness.get('snapshot_version'))} · {_e(readiness.get('policy_key'))} v{_e(readiness.get('policy_version'))}</p>"
        f"<table><tr><th>{_t(locale, 'Metric', 'Métrica')}</th><th class='num'>{_t(locale, 'Value', 'Valor')}</th><th>{_t(locale, 'Tier', 'Nivel')}</th></tr>{''.join(rows)}</table>"
        f"<ul>{blockers}</ul><ol>{phases}</ol>"
        f"<p class='small muted'>{_t(locale, 'Readiness is advisory. Approval and lender terms require an authorized underwriting decision.', 'La preparación es orientativa. La aprobación y los términos requieren una decisión autorizada de suscripción.')}</p>"
    )


def build_html(bundle: dict) -> str:
    """Compact print-ready HTML for one lender package bundle. Pure."""
    dealer = bundle.get("dealer") or {}
    snapshot = bundle.get("snapshot")
    readiness = bundle.get("capital_readiness")
    locale = "es" if (readiness or {}).get("communication_locale") == "es" else "en"
    as_of = (snapshot or {}).get("as_of")
    location = ", ".join(x for x in (dealer.get("city"), dealer.get("state")) if x)
    sub_bits = [b for b in (dealer.get("legal_name"), location, dealer.get("email")) if b]
    return f"""<!DOCTYPE html>
<html lang="{locale}"><head><meta charset="utf-8"><title>{_t(locale, 'Lender package', 'Paquete de financiamiento')} — {_e(dealer.get('name'))}</title>
<style>{_CSS}</style></head><body>
<h1>{_e(dealer.get('name'))} — {_t(locale, 'Lender package', 'Paquete de financiamiento')}</h1>
<p class="sub">{_e(' · '.join(sub_bits)) if sub_bits else ''}
{f"&nbsp;&nbsp;<span class='muted'>{_t(locale, 'Snapshot as of', 'Evaluación al')} {_e(as_of)}</span>" if as_of else f"<span class='muted'>{_t(locale, 'No metric snapshot yet', 'Todavía no hay evaluación de métricas')}</span>"}</p>
{_readiness_block(readiness, locale)}
{_kpi_row(snapshot, locale)}
<h2>{_t(locale, 'Funding paths', 'Opciones de financiamiento')}</h2>{_paths_table(bundle.get('paths'), locale)}
<h2>{_t(locale, 'Targets', 'Objetivos')}</h2>{_targets_table(bundle.get('targets') or [], locale)}
<h2>{_t(locale, 'Monthly financials', 'Finanzas mensuales')}</h2>{_periods_table(bundle.get('periods') or [], locale)}
<h2>{_t(locale, 'EBITDA add-backs', 'Ajustes de EBITDA')}</h2>{_addbacks_table(bundle.get('addbacks') or [], locale)}
<h2>{_t(locale, 'Action plan', 'Plan de acción')}</h2>{_plan_table(bundle.get('plan') or [], locale)}
<h2>{_t(locale, '12-month forecast', 'Proyección de 12 meses')}</h2>{_forecast_block(bundle.get('forecast'), locale)}
<p class="small muted">{_t(locale, 'Generated by Qualified Commercial Capital OS. Metrics are monitoring readouts, not a credit decision; funding-path thresholds are provisional product heuristics.', 'Generado por Qualified Commercial Capital OS. Las métricas son indicadores de seguimiento; la decisión crediticia requiere revisión autorizada y los umbrales de programas son preliminares.')}</p>
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
