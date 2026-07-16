from __future__ import annotations

import base64
import io
import logging
import math
import re
from datetime import datetime
from html import escape
from typing import Any

log = logging.getLogger(__name__)

# Brand palette (white-background bank-underwriter document).
NAVY = "#0B1D3A"
TEAL = "#0f766e"
TEAL_LT = "#21d3c7"
RED = "#b91c1c"
INK = "#111827"
SLATE = "#334155"
MUTE = "#64748b"
LINE = "#d8dee9"
ZEBRA = "#f4f6fb"
HEADFILL = "#0B1D3A"


# ─────────────────────────────────────────────────────────────────────────────
# Value coercion helpers
# ─────────────────────────────────────────────────────────────────────────────
def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _text(value: Any, fallback: str = "Awaiting evidence") -> str:
    if value is None:
        return fallback
    # Flatten nested structures into readable prose so no PDF cell ever renders a
    # Python repr like {'dscr': 1.2} or ['a', 'b'].
    if isinstance(value, dict):
        pairs = []
        for k, v in value.items():
            flat = _text(v, fallback="")
            if flat:
                pairs.append(f"{str(k).replace('_', ' ').strip().title()}: {flat}")
        text = "; ".join(pairs)
        return text or fallback
    if isinstance(value, (list, tuple)):
        items = [_text(item, fallback="") for item in value]
        text = "; ".join(item for item in items if item)
        return text or fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip()
    return text or fallback


def _money(value: Any) -> str:
    if value is None or value == "":
        return "Awaiting evidence"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"${number:,.0f}"


def _num(value: Any) -> float | None:
    """Best-effort parse of a currency/number to float (strips $ , and spaces)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in ("", "-", ".", "-."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt_compact(number: float | None) -> str:
    if number is None:
        return "—"
    sign = "-" if number < 0 else ""
    a = abs(number)
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:.0f}K"
    return f"{sign}${a:,.0f}"


def _fmt_full(number: float | None) -> str:
    return "—" if number is None else f"${number:,.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# Sensitive-data redaction (mask account / ID numbers; keep last 4)
# ─────────────────────────────────────────────────────────────────────────────
_SSN_RE = re.compile(r"\b(\d{3})[-\s](\d{2})[-\s](\d{4})\b")
_EIN_RE = re.compile(r"\b(\d{2})-(\d{7})\b")
_LONGNUM_RE = re.compile(r"\b\d{7,}\b")


def _redact(text: str) -> str:
    """Mask account/routing/SSN/EIN-style numbers in free prose (show last 4).
    Names, businesses, balances and underwriting figures (which carry commas or
    $) are left intact — a lender needs those."""
    if not text:
        return text
    out = _SSN_RE.sub(lambda m: "•••-••-" + m.group(3), text)
    out = _EIN_RE.sub(lambda m: "••-•••" + m.group(2)[-4:], out)
    out = _LONGNUM_RE.sub(lambda m: "••••" + m.group(0)[-4:], out)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Structured-fact extraction (bank months + tax years) — pure, unit-testable.
# Called from the router against the durable per-file analysis cache.
# ─────────────────────────────────────────────────────────────────────────────
_MONTH_LABELS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _period_key(period: Any) -> tuple[str, str] | None:
    """('2026-01-01 to 2026-01-31') -> ('2026-01', 'Jan 2026'). Returns None if unparseable."""
    text = str(period or "").strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})", text)
    if not m:
        return None
    year, month = m.group(1), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return (f"{year}-{month:02d}", f"{_MONTH_LABELS[month]} {year}")


def _month_record(source: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one month of bank facts from a key_facts dict (or a months[] item)."""
    key = _period_key(source.get("statement_period"))
    if key is None:
        return None
    deposits = _num(source.get("total_deposits_and_credits"))
    withdrawals = _num(source.get("total_withdrawals_and_debits"))
    ending = _num(source.get("ending_balance"))
    nsf = _num(source.get("nsf_or_overdraft_count"))
    return {
        "sort": key[0],
        "label": key[1],
        "deposits": deposits,
        "withdrawals": abs(withdrawals) if withdrawals is not None else None,
        "ending_balance": ending,
        "nsf": int(nsf) if nsf is not None else None,
    }


def extract_bank_months(analyses: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """Dedupe bank-statement months across all analyzed files (by statement month),
    return the most recent `limit` months in chronological order."""
    by_month: dict[str, dict[str, Any]] = {}
    for item in analyses:
        if item.get("classification") != "bank_statement":
            continue
        facts = item.get("key_facts") or {}
        if not isinstance(facts, dict):
            continue
        candidates: list[dict[str, Any]] = []
        months = facts.get("months")
        if isinstance(months, list) and months:
            candidates.extend(m for m in months if isinstance(m, dict))
        else:
            candidates.append(facts)
        for source in candidates:
            rec = _month_record(source)
            if rec is None:
                continue
            prior = by_month.get(rec["sort"])
            # Prefer the record with the most populated numeric fields.
            def _score(r: dict[str, Any]) -> int:
                return sum(1 for k in ("deposits", "withdrawals", "ending_balance") if r.get(k) is not None)
            if prior is None or _score(rec) > _score(prior):
                by_month[rec["sort"]] = rec
    ordered = [by_month[k] for k in sorted(by_month)]
    return ordered[-limit:]


_TAX_KEYS = (
    "gross_receipts", "gross_income", "total_income", "net_income", "taxable_income",
    "ordinary_business_income", "total_revenue", "cost_of_goods_sold", "depreciation",
)

# Classifications that carry revenue-like line items but are NOT tax returns, so the
# _TAX_KEYS content heuristic must not misclassify them (e.g. a YTD P&L has
# gross_receipts but is not a filed return).
_NON_TAX_CLASSES = {
    "bank_statement", "current_p_and_l", "profit_and_loss", "p_and_l", "pnl",
    "floorplan_mca_inventory", "identity", "other",
}


def _tax_year(facts: dict[str, Any]) -> str | None:
    for k in ("tax_year", "year", "return_year", "fiscal_year"):
        v = facts.get(k)
        if v:
            m = re.search(r"(19|20)\d{2}", str(v))
            if m:
                return m.group(0)
    return None


def extract_tax_years(analyses: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    """Extract per-year tax-return figures from analyzed tax returns; return the
    most recent `limit` years. Empty if no tax returns were analyzed."""
    by_year: dict[str, dict[str, Any]] = {}
    for item in analyses:
        cls = str(item.get("classification") or "")
        facts = item.get("key_facts") or {}
        if not isinstance(facts, dict):
            continue
        # Only treat as a tax return by explicit classification, or by the content
        # heuristic when the file is NOT a known non-tax type (a P&L / bank statement
        # can carry gross_receipts/net_income without being a filed return).
        is_tax = cls == "tax_return" or (cls not in _NON_TAX_CLASSES and any(k in facts for k in _TAX_KEYS))
        if not is_tax:
            continue
        year = _tax_year(facts) or cls
        rec = {
            "year": year if re.fullmatch(r"(19|20)\d{2}", str(year or "")) else (_tax_year(facts) or "—"),
            "entity": _text(facts.get("entity_name") or facts.get("business_name") or facts.get("taxpayer"), fallback=""),
            "gross_receipts": _num(facts.get("gross_receipts") or facts.get("gross_income") or facts.get("total_revenue")),
            "net_income": _num(facts.get("net_income") or facts.get("ordinary_business_income") or facts.get("taxable_income")),
            "total_income": _num(facts.get("total_income")),
        }
        yk = str(rec["year"])
        if yk and yk != "—":
            prior = by_year.get(yk)
            if prior is None or sum(1 for k in ("gross_receipts", "net_income") if rec.get(k) is not None) >= sum(
                1 for k in ("gross_receipts", "net_income") if prior.get(k) is not None
            ):
                by_year[yk] = rec
    ordered = [by_year[k] for k in sorted(by_year)]
    return ordered[-limit:]


# ─────────────────────────────────────────────────────────────────────────────
# SVG line charts → PNG data-URI (works in both WeasyPrint and PyMuPDF Story)
# ─────────────────────────────────────────────────────────────────────────────
def _chart_panel(
    ox: float,
    oy: float,
    width: float,
    height: float,
    title: str,
    x_labels: list[str],
    series: list[dict[str, Any]],
) -> list[str]:
    """Draw one line-chart panel at offset (ox, oy) into a parent SVG. Returns svg fragments."""
    pad_l, pad_r, pad_t, pad_b = 54, 14, 34, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    x0 = ox + pad_l
    x1 = ox + width - pad_r
    y1 = oy + height - pad_b

    values = [v for s in series for v in s["values"] if v is not None]
    vmax = max(values) if values else 1.0
    vmax = vmax * 1.15 if vmax > 0 else 1.0
    n = len(x_labels)

    def px(i: int) -> float:
        if n <= 1:
            return x0 + plot_w / 2
        return x0 + plot_w * i / (n - 1)

    def py(v: float) -> float:
        return y1 - (v / vmax) * plot_h

    parts: list[str] = [
        f'<rect x="{ox}" y="{oy}" width="{width}" height="{height}" fill="#ffffff" stroke="{LINE}" stroke-width="1" rx="8"/>',
        f'<text x="{ox + 14}" y="{oy + 20}" font-family="Arial" font-size="12" font-weight="700" fill="{INK}">{escape(title)}</text>',
    ]
    for g in range(5):
        gv = vmax * g / 4
        gy = py(gv)
        parts.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" stroke="#eef1f6" stroke-width="1"/>')
        parts.append(
            f'<text x="{x0 - 6}" y="{gy + 3:.1f}" font-family="Arial" font-size="8" fill="{MUTE}" text-anchor="end">{escape(_fmt_compact(gv))}</text>'
        )
    for i, lab in enumerate(x_labels):
        parts.append(
            f'<text x="{px(i):.1f}" y="{y1 + 14:.1f}" font-family="Arial" font-size="8" fill="{MUTE}" text-anchor="middle">{escape(lab)}</text>'
        )
    for s in series:
        color = s["color"]
        pts = [f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(s["values"]) if v is not None]
        if len(pts) >= 2:
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2.4"/>')
        for i, v in enumerate(s["values"]):
            if v is None:
                continue
            parts.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" fill="{color}"/>')
    # legend (top-right of this panel)
    lx = x1
    for s in reversed(series):
        label = s["label"]
        lw = 8 + len(label) * 5.6
        lx -= lw + 10
        parts.append(f'<rect x="{lx:.1f}" y="{oy + 8}" width="9" height="9" rx="2" fill="{s["color"]}"/>')
        parts.append(
            f'<text x="{lx + 13:.1f}" y="{oy + 16}" font-family="Arial" font-size="9" fill="{SLATE}">{escape(label)}</text>'
        )
    return parts


def _dual_chart_svg(panels: list[dict[str, Any]], *, panel_w: int = 328, panel_h: int = 196, gap: int = 14) -> tuple[str, int]:
    """One wide SVG holding N side-by-side chart panels — avoids relying on the
    HTML renderer to size two separate <img> cells (PyMuPDF Story does not).
    Returns (svg, logical_width)."""
    width = panel_w * len(panels) + gap * (len(panels) - 1)
    height = panel_h
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for idx, p in enumerate(panels):
        ox = idx * (panel_w + gap)
        parts.extend(_chart_panel(ox, 0, panel_w, panel_h, p["title"], p["x_labels"], p["series"]))
    parts.append("</svg>")
    return "".join(parts), width


def _dual_axis_chart_svg(
    title: str,
    x_labels: list[str],
    left_series: list[dict[str, Any]],
    right_series: list[dict[str, Any]],
    *,
    width: int = 660,
    height: int = 250,
) -> tuple[str, int]:
    """One chart with TWO independent Y axes so series of different magnitudes
    (cash-flow in millions on the left, ending balance in hundreds of thousands on
    the right) share a single plot correctly. Gridlines are shared; each axis gets
    its own tick labels. Returns (svg, logical_width)."""
    pad_l, pad_r, pad_t, pad_b = 64, 68, 56, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    x0 = pad_l
    x1 = width - pad_r
    y0 = pad_t
    y1 = height - pad_b
    n = len(x_labels)
    STEPS = 4

    def _axis_range(series: list[dict[str, Any]]) -> tuple[float, float]:
        """Return (vmin, vmax) that always includes 0 as a baseline and pads the
        top; if any value is negative (e.g. an overdrawn ending balance) the axis
        extends below 0 so the point stays on-canvas instead of falling off."""
        vals = [v for s in series for v in s["values"] if v is not None]
        if not vals:
            return 0.0, 1.0
        hi = max(vals)
        lo = min(vals)
        vmax = hi * 1.15 if hi > 0 else 0.0
        vmin = lo * 1.15 if lo < 0 else 0.0
        if vmax <= vmin:  # all zero, or degenerate
            vmax = vmin + 1.0
        return vmin, vmax

    left_min, left_max = _axis_range(left_series)
    right_min, right_max = _axis_range(right_series)

    def px(i: int) -> float:
        if n <= 1:
            return x0 + plot_w / 2
        return x0 + plot_w * i / (n - 1)

    def py(v: float, vmin: float, vmax: float) -> float:
        span = (vmax - vmin) or 1.0
        return y1 - ((v - vmin) / span) * plot_h

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" stroke="{LINE}" stroke-width="1" rx="8"/>',
        f'<text x="16" y="24" font-family="Arial" font-size="13" font-weight="700" fill="{INK}">{escape(title)}</text>',
    ]
    # shared horizontal gridlines; left tick labels (teal) + right tick labels (navy).
    # Each axis interpolates its own [vmin, vmax] across the shared gridline fractions.
    for g in range(STEPS + 1):
        frac = g / STEPS
        gy = y1 - frac * plot_h
        parts.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" stroke="#eef1f6" stroke-width="1"/>')
        parts.append(
            f'<text x="{x0 - 8}" y="{gy + 3:.1f}" font-family="Arial" font-size="8" fill="{TEAL}" text-anchor="end">{escape(_fmt_compact(left_min + (left_max - left_min) * frac))}</text>'
        )
        parts.append(
            f'<text x="{x1 + 8}" y="{gy + 3:.1f}" font-family="Arial" font-size="8" fill="{NAVY}" text-anchor="start">{escape(_fmt_compact(right_min + (right_max - right_min) * frac))}</text>'
        )
    # axis spines (teal left = cash flow, navy right = ending balance)
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="{TEAL}" stroke-width="1.5"/>')
    parts.append(f'<line x1="{x1}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="{NAVY}" stroke-width="1.5"/>')
    # x labels
    for i, lab in enumerate(x_labels):
        parts.append(
            f'<text x="{px(i):.1f}" y="{y1 + 15:.1f}" font-family="Arial" font-size="8.5" fill="{MUTE}" text-anchor="middle">{escape(lab)}</text>'
        )

    def _draw(series: list[dict[str, Any]], vmin: float, vmax: float) -> None:
        for s in series:
            color = s["color"]
            # Segment the line at None gaps so a missing month leaves a break rather
            # than a false straight line implying continuous data.
            segment: list[str] = []
            for i, v in enumerate(s["values"]):
                if v is None:
                    if len(segment) >= 2:
                        parts.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="2.4"/>')
                    segment = []
                    continue
                pt = f"{px(i):.1f},{py(v, vmin, vmax):.1f}"
                segment.append(pt)
                parts.append(f'<circle cx="{px(i):.1f}" cy="{py(v, vmin, vmax):.1f}" r="3.2" fill="{color}"/>')
            if len(segment) >= 2:
                parts.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="2.4"/>')

    _draw(left_series, left_min, left_max)
    _draw(right_series, right_min, right_max)

    # legend row on the top line, right-aligned so it never collides with the title.
    # Color maps each series to its axis (teal/red = left cash-flow, navy = right balance).
    legend = [(s["label"], s["color"]) for s in left_series] + [(s["label"], s["color"]) for s in right_series]
    def _seg_w(label: str) -> float:
        return 22 + len(label) * 5.6 + 16
    total = sum(_seg_w(lbl) for lbl, _ in legend)
    title_right = 16 + len(title) * 7.2  # keep the legend clear of the title text
    lx = max(title_right + 10, width - 16 - total)
    ly = 16
    for label, color in legend:
        parts.append(f'<rect x="{lx:.1f}" y="{ly}" width="16" height="10" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{lx + 21:.1f}" y="{ly + 9}" font-family="Arial" font-size="9.5" fill="{SLATE}">{escape(label)}</text>')
        lx += _seg_w(label)
    parts.append("</svg>")
    return "".join(parts), width


def _svg_to_png_datauri(svg: str, scale: float = 2.0) -> str | None:
    """Rasterize an SVG string to a base64 PNG data-URI via PyMuPDF (no native deps)."""
    try:
        import fitz

        doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        png = pix.tobytes("png")
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    except Exception:
        log.exception("chart rasterization failed")
        return None


def _svg_to_png_fit(svg: str, svg_logical_w: float, target_pt: float) -> str | None:
    """Rasterize an SVG so its NATIVE placement width equals target_pt.

    PyMuPDF Story places a PNG at (pixels × 0.75) points when it ignores the HTML
    width attribute (which it does inconsistently in complex documents). By sizing
    the raster so native width == target_pt AND setting width=target_pt on the tag,
    the chart fits the frame whether or not Story honors the attribute."""
    scale = (target_pt / 0.75) / svg_logical_w
    return _svg_to_png_datauri(svg, scale=max(scale, 0.5))


def _logo_datauri(size: int = 132) -> str | None:
    """The QCMark logo (matches qcdesktop QCMark.tsx) as a PNG data-URI."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="{size}" height="{size}">'
        '<defs>'
        '<linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">'
        f'<stop offset="0%" stop-color="{NAVY}"/><stop offset="100%" stop-color="#050E1F"/>'
        '</linearGradient>'
        '<linearGradient id="teal" x1="0%" y1="0%" x2="100%" y2="100%">'
        f'<stop offset="0%" stop-color="{TEAL_LT}"/><stop offset="100%" stop-color="#18A89F"/>'
        '</linearGradient>'
        '</defs>'
        '<rect width="512" height="512" rx="115" fill="url(#bg)"/>'
        '<circle cx="200" cy="240" r="120" fill="none" stroke="#FFFFFF" stroke-width="52"/>'
        '<line x1="280" y1="320" x2="350" y2="400" stroke="#FFFFFF" stroke-width="52" stroke-linecap="square"/>'
        '<path d="M 460 140 A 130 130 0 1 0 460 370" fill="none" stroke="url(#teal)" stroke-width="52" stroke-linecap="square"/>'
        '</svg>'
    )
    return _svg_to_png_datauri(svg, scale=1.0)


# ─────────────────────────────────────────────────────────────────────────────
# HTML section builders
# ─────────────────────────────────────────────────────────────────────────────
def _field(label: str, value: Any) -> str:
    return (
        '<td class="field">'
        f'<span>{escape(label)}</span>'
        f'<strong>{escape(_text(value))}</strong>'
        "</td>"
    )


def _prose_card(title: str, text: str | None) -> str:
    if not text:
        return ""
    return f'<section class="card"><h2>{escape(title)}</h2><p>{escape(text)}</p></section>'


def _bullet_card(title: str, items: list[str], tone: str = "") -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{escape(item)}</li>" for item in items[:12])
    return f'<section class="card {tone}"><h2>{escape(title)}</h2><ul>{lis}</ul></section>'


def _bank_section(months: list[dict[str, Any]]) -> str:
    if not months:
        return (
            '<section class="card"><h2>Bank activity — last 6 months</h2>'
            '<p class="empty">Awaiting bank-statement evidence.</p></section>'
        )
    labels = [m["label"] for m in months]

    def _net(m: dict[str, Any]) -> float | None:
        if m["deposits"] is None or m["withdrawals"] is None:
            return None
        return m["deposits"] - m["withdrawals"]

    # ONE chart, two Y axes: deposits & withdrawals on the left (cash-flow scale),
    # ending balance on the right (its own, much smaller scale).
    chart_svg, chart_w = _dual_axis_chart_svg(
        "Monthly cash flow & ending balance",
        labels,
        left_series=[
            {"label": "Deposits", "color": TEAL, "values": [m["deposits"] for m in months]},
            {"label": "Withdrawals", "color": RED, "values": [m["withdrawals"] for m in months]},
        ],
        right_series=[
            {"label": "Ending balance", "color": NAVY, "values": [m["ending_balance"] for m in months]},
        ],
    )
    # Target ~656pt so the chart sits inside the card's inner width on a landscape page.
    target = 656
    chart_uri = _svg_to_png_fit(chart_svg, chart_w, target)
    charts = f'<div class="charts"><img src="{chart_uri}" width="{target}"/></div>' if chart_uri else ""

    # Transposed table: one ROW per metric, one COLUMN per month (spreadsheet layout).
    month_headers = "".join(f"<th class='num'>{escape(m['label'])}</th>" for m in months)

    def _metric_row(label: str, fmt) -> str:
        cells = "".join(f'<td class="num">{fmt(m)}</td>' for m in months)
        return f'<tr><td class="rowhead">{escape(label)}</td>{cells}</tr>'

    body = (
        _metric_row("Deposits", lambda m: _fmt_full(m["deposits"]))
        + _metric_row("Withdrawals", lambda m: _fmt_full(m["withdrawals"]))
        + _metric_row("Net", lambda m: _fmt_full(_net(m)))
        + _metric_row("Ending balance", lambda m: _fmt_full(m["ending_balance"]))
        + _metric_row("NSF / OD", lambda m: ("—" if m["nsf"] is None else str(m["nsf"])))
    )
    deposits_vals = [m["deposits"] for m in months if m["deposits"] is not None]
    avg_dep = sum(deposits_vals) / len(deposits_vals) if deposits_vals else None
    return (
        '<section class="card wide"><h2>Bank activity — last 6 months</h2>'
        f"{charts}"
        '<table class="grid-table"><thead><tr>'
        f"<th>Metric</th>{month_headers}"
        f"</tr></thead><tbody>{body}</tbody></table>"
        f'<p class="note">Average monthly deposits across the period: <strong>{_fmt_full(avg_dep)}</strong> '
        f"({len(months)} month{'s' if len(months) != 1 else ''} of statements analyzed). "
        "Account numbers redacted for confidentiality.</p>"
        "</section>"
    )


def _tax_section(years: list[dict[str, Any]]) -> str:
    if not years:
        return (
            '<section class="card"><h2>Tax returns — 2-year summary</h2>'
            '<p class="empty">Awaiting business tax-return evidence (last 2 years).</p></section>'
        )
    rows = "".join(
        "<tr>"
        f'<td>{escape(str(y["year"]))}</td>'
        f'<td>{escape(y["entity"] or "—")}</td>'
        f'<td class="num">{_fmt_full(y["gross_receipts"])}</td>'
        f'<td class="num">{_fmt_full(y["total_income"])}</td>'
        f'<td class="num">{_fmt_full(y["net_income"])}</td>'
        "</tr>"
        for y in years
    )
    # trend narrative
    narrative = ""
    if len(years) == 2:
        a, b = years[0], years[1]
        if a.get("gross_receipts") and b.get("gross_receipts"):
            delta = b["gross_receipts"] - a["gross_receipts"]
            pct = (delta / a["gross_receipts"] * 100) if a["gross_receipts"] else 0
            direction = "grew" if delta >= 0 else "declined"
            narrative = (
                f"Gross receipts {direction} from {_fmt_full(a['gross_receipts'])} in {a['year']} to "
                f"{_fmt_full(b['gross_receipts'])} in {b['year']} ({pct:+.0f}%)."
            )
    return (
        '<section class="card wide"><h2>Tax returns — 2-year summary</h2>'
        '<table class="grid-table"><thead><tr>'
        "<th>Tax year</th><th>Entity</th><th class='num'>Gross receipts</th>"
        "<th class='num'>Total income</th><th class='num'>Net income</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        + (f'<p class="note">{escape(narrative)}</p>' if narrative else "")
        + "</section>"
    )


def _metric_table(metric_rows: list[dict[str, Any]]) -> str:
    body = "".join(
        "<tr>"
        f'<td>{escape(_text(r.get("metric")))}</td>'
        f'<td>{escape(_text(r.get("value")))}</td>'
        f'<td>{escape(_text(r.get("source")))}</td>'
        "</tr>"
        for r in metric_rows[:20]
    )
    return (
        '<section class="card wide"><h2>Key figures &amp; application fields</h2>'
        '<table class="grid-table"><thead><tr><th>Field</th><th>Value</th><th>Source</th></tr></thead>'
        f"<tbody>{body}</tbody></table></section>"
    )


def _simple_table(title: str, rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        body = f'<tr><td colspan="{len(columns)}" class="empty">Awaiting evidence.</td></tr>'
    else:
        body = "".join(
            "<tr>" + "".join(f"<td>{escape(_text(row.get(key), fallback='—'))}</td>" for key, _ in columns) + "</tr>"
            for row in rows[:16]
        )
    headers = "".join(f"<th>{escape(label)}</th>" for _, label in columns)
    return (
        f'<section class="card wide"><h2>{escape(title)}</h2>'
        f'<table class="grid-table"><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></section>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Plain-text fallback (unchanged) — last resort when no renderer is available.
# ─────────────────────────────────────────────────────────────────────────────
def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(value: str, width: int = 110) -> list[str]:
    words = value.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            if current:
                lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def _minimal_pdf(lines: list[str]) -> bytes:
    chunks = [lines[index:index + 48] for index in range(0, len(lines), 48)] or [["Qualified Commercial Underwriting Packet"]]
    objects: list[bytes] = []

    def add_object(body: str) -> int:
        objects.append(body.encode("latin-1", "replace"))
        return len(objects)

    catalog_id = add_object("<< /Type /Catalog /Pages 2 0 R >>")
    pages_id = add_object("<< /Type /Pages /Kids [] /Count 0 >>")
    font_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    for chunk in chunks:
        text_ops = ["BT /F1 10 Tf 40 560 Td 14 TL"]
        for line in chunk:
            text_ops.append(f"({_pdf_escape(line)}) Tj T*")
        text_ops.append("ET")
        stream = "\n".join(text_ops)
        content_id = add_object(f"<< /Length {len(stream.encode('latin-1', 'replace'))} >>\nstream\n{stream}\nendstream")
        page_id = add_object(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 792 612] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        page_ids.append(page_id)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] /Count {len(page_ids)} >>".encode("latin-1")
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("latin-1"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_at = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("latin-1"))
    return bytes(output)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def render_underwriting_packet_pdf(
    *,
    intake: Any,
    files: list[Any],
    missing_docs: list[Any],
    result: dict[str, Any] | None,
    executive_summary: dict[str, Any] | None = None,
    financials: dict[str, Any] | None = None,
) -> bytes:
    result = result or {}
    executive_summary = executive_summary or {}
    financials = financials or {}
    bank_months = financials.get("bank_months") or []
    tax_years = financials.get("tax_years") or []

    variant = str(getattr(intake, "variant", "") or "")
    is_real_estate = variant.startswith("real_estate")
    key_metrics = _record(result.get("key_metrics"))
    bankability = _record(result.get("bankability_assessment"))
    title = "Underwriting Packet"
    subtitle = "Real estate / DSCR funding review" if is_real_estate else "Dealer capital funding review"
    borrower_name = getattr(intake, "full_name", None)
    business_name = getattr(intake, "business_name", None)
    requested_amount = _money(getattr(intake, "requested_loan_amount", None))
    purpose = getattr(intake, "loan_purpose", None)
    status = result.get("probability_status") or result.get("fundability_status") or bankability.get("status")
    program_fit = result.get("program_fit") or executive_summary.get("suggested_product_path") or executive_summary.get("recommended_approach")

    summary_text = _redact(_text(
        executive_summary.get("executive_summary")
        or result.get("executive_summary")
        or bankability.get("reason")
        or "The file has not produced a complete AI underwriting summary yet."
    ))
    recommended_angle = _redact(_text(
        executive_summary.get("vendor_submission_angle")
        or executive_summary.get("submission_angle")
        or result.get("one_next_step")
        or "Awaiting final submission angle."
    ))
    risks = [_redact(s) for s in (_strings(executive_summary.get("risks")) or _strings(result.get("risks")))]
    mitigants = [_redact(s) for s in (_strings(executive_summary.get("mitigants")) or _strings(result.get("mitigants")))]
    strengths = [_redact(s) for s in (_strings(executive_summary.get("strengths")) or _strings(result.get("strengths")))]

    missing_rows = _records(result.get("missing_or_incomplete_items"))
    if not missing_rows:
        missing_rows = [
            {"title": getattr(doc, "name", "Missing item"), "detail": getattr(doc, "description", ""), "priority": "open"}
            for doc in missing_docs
        ]
    evidence = _record(result.get("document_evidence_map"))
    coverage_rows = _records(evidence.get("baseline_coverage")) or _records(evidence.get("file_classifications"))
    reviewed_docs = [
        {
            "file": getattr(file, "zip_entry_path", None) or getattr(file, "file_name", "Uploaded file"),
            "type": getattr(file, "content_type", ""),
            "status": getattr(file, "status", ""),
        }
        for file in files
    ]
    metric_rows = [
        {"metric": "Probability status", "value": status or "Awaiting review", "source": "AI review"},
        {"metric": "Suggested path", "value": program_fit or "Awaiting evidence", "source": "AI review"},
        {"metric": "Requested amount", "value": requested_amount, "source": "Intake"},
    ]
    exec_metrics = executive_summary.get("key_metrics")
    if isinstance(exec_metrics, list) and exec_metrics:
        for m in exec_metrics:
            if isinstance(m, dict):
                label = _text(m.get("label"), fallback="")
                value = _text(m.get("value"), fallback="")
                note = _text(m.get("note"), fallback="")
                if label or value:
                    metric_rows.append({"metric": label or "Metric", "value": value, "source": note or "AI extraction"})
    else:
        for key, value in key_metrics.items():
            metric_rows.append({"metric": str(key).replace("_", " ").title(), "value": _text(value), "source": "AI extraction"})

    def _prose(value: Any) -> str | None:
        text = _text(value, fallback="")
        return _redact(text) if text and text.lower() != "awaiting evidence" else None

    borrower_profile = _prose(executive_summary.get("borrower_profile"))
    entity_vesting = _prose(executive_summary.get("entity_vesting_notes"))
    property_collateral = _prose(executive_summary.get("property_collateral"))
    requested_terms = _prose(executive_summary.get("requested_terms"))
    application_types = _strings(executive_summary.get("suggested_application_types"))

    narrative_pairs = [
        ("Borrower profile", borrower_profile),
        ("Entity &amp; vesting", entity_vesting),
        ("Property / collateral", property_collateral),
        ("Requested terms", requested_terms),
    ]
    narrative_present = [(label, text) for label, text in narrative_pairs if text]
    # Two-column grid of narrative cards (pairs per row).
    narrative_rows = []
    for i in range(0, len(narrative_present), 2):
        left = _prose_card(*narrative_present[i])
        right = _prose_card(*narrative_present[i + 1]) if i + 1 < len(narrative_present) else ""
        narrative_rows.append(f'<table class="cols"><tr><td>{left}</td><td>{right}</td></tr></table>')
    narrative_cards = "".join(narrative_rows)

    # Executive summary as one or more real <p> paragraphs (prose, not JSON).
    exec_paras = [p.strip() for p in re.split(r"\n{2,}|\r\n\r\n", summary_text) if p.strip()]
    if not exec_paras:
        exec_paras = [summary_text]
    exec_html = "".join(f"<p>{escape(p)}</p>" for p in exec_paras)

    logo = _logo_datauri()
    logo_img = f'<img src="{logo}" width="46" height="46"/>' if logo else ""
    generated = datetime.utcnow().strftime("%b %d, %Y %H:%M UTC")

    fallback_lines = [
        f"Qualified Commercial {title}", subtitle,
        f"Generated: {generated}",
        f"Borrower/guarantor: {_text(borrower_name)}",
        f"Business/entity: {_text(business_name)}",
        f"Requested amount: {requested_amount}",
        f"Loan purpose: {_text(purpose)}",
        f"Status: {_text(status)}",
        "", "Executive summary:",
    ]
    fallback_lines.extend(_wrap(summary_text))
    fallback_lines.extend(["", "Recommended submission angle:"])
    fallback_lines.extend(_wrap(recommended_angle))
    if bank_months:
        fallback_lines.extend(["", "Bank activity (last 6 months):"])
        for m in bank_months:
            fallback_lines.append(
                f"  {m['label']}: deposits {_fmt_full(m['deposits'])}, withdrawals {_fmt_full(m['withdrawals'])}, ending {_fmt_full(m['ending_balance'])}"
            )
    fallback_lines.extend(["", "Missing confirmations:"])
    for row in missing_rows[:12]:
        fallback_lines.extend(_wrap(f"- {row.get('title') or 'Missing item'}: {row.get('detail') or ''}"))
    fallback_lines.extend(["", "CONFIDENTIAL — This packet is not a 1003, lender-specific application, commitment to lend, or final credit decision."])

    html_doc = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{ size: Letter landscape; margin: 34px; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: Arial, Helvetica, sans-serif; background:#ffffff; color:{INK}; font-size:11px; margin:0; }}
    h1 {{ font-size:22px; margin:0; color:{NAVY}; letter-spacing:-.01em; }}
    h2 {{ font-size:12px; margin:0 0 8px; color:{NAVY}; text-transform:uppercase; letter-spacing:.06em; border-bottom:2px solid {TEAL}; padding-bottom:4px; }}
    p {{ color:{SLATE}; line-height:1.55; margin:0 0 8px; }}
    .header {{ width:100%; border-bottom:3px solid {NAVY}; padding-bottom:12px; margin-bottom:14px; }}
    .header td {{ vertical-align:middle; }}
    .brand {{ font-weight:800; color:{TEAL}; text-transform:uppercase; letter-spacing:.14em; font-size:10px; }}
    .sub {{ color:{MUTE}; font-size:10px; margin-top:2px; }}
    .pill {{ border:1px solid {TEAL_LT}; border-radius:999px; padding:6px 12px; color:{TEAL}; background:#ecfeff; font-weight:800; font-size:11px; white-space:nowrap; }}
    .snapshot {{ width:100%; border-collapse:separate; border-spacing:8px 0; margin:0 0 14px; }}
    .field {{ border:1px solid {LINE}; border-radius:10px; padding:9px 11px; background:#fbfcfe; width:16.6%; vertical-align:top; }}
    .field span {{ color:{MUTE}; display:block; font-size:9px; text-transform:uppercase; letter-spacing:.07em; font-weight:800; }}
    .field strong {{ display:block; margin-top:4px; color:{INK}; font-size:12px; }}
    .card {{ border:1px solid {LINE}; border-radius:12px; padding:14px 16px; background:#ffffff; margin-bottom:12px; }}
    .card.wide {{ page-break-inside:avoid; }}
    .cols {{ width:100%; }} .cols td {{ vertical-align:top; width:50%; }}
    .cols td:first-child {{ padding-right:8px; }} .cols td:last-child {{ padding-left:8px; }}
    ul {{ margin:0; padding-left:18px; }} li {{ margin:4px 0; line-height:1.45; color:{SLATE}; }}
    .grid-table {{ width:100%; border-collapse:collapse; margin-top:6px; font-size:10.5px; }}
    .grid-table th {{ background:{HEADFILL}; color:#ffffff; text-transform:uppercase; font-size:9px; letter-spacing:.05em; padding:7px 8px; text-align:left; }}
    .grid-table th.num, .grid-table td.num {{ text-align:right; }}
    .grid-table td {{ border-bottom:1px solid {LINE}; padding:6px 8px; color:{INK}; }}
    .grid-table td.rowhead {{ font-weight:700; color:{NAVY}; text-align:left; background:{ZEBRA}; }}
    .grid-table tbody tr:nth-child(even) td {{ background:{ZEBRA}; }}
    .charts {{ width:100%; margin-bottom:8px; text-align:center; }}
    .note {{ color:{MUTE}; font-size:10px; font-style:italic; margin-top:8px; }}
    .empty {{ color:{MUTE}; font-style:italic; }}
    .green {{ border-left:4px solid {TEAL}; }} .amber {{ border-left:4px solid #d97706; }}
    .disclaimer {{ background:#fff7ed; border-color:#fed7aa; color:#7c2d12; }}
    .footer {{ color:{MUTE}; font-size:9px; margin-top:12px; border-top:1px solid {LINE}; padding-top:8px; }}
  </style>
</head>
<body>
  <table class="header"><tr>
    <td width="60">{logo_img}</td>
    <td>
      <div class="brand">Qualified Commercial</div>
      <h1>{escape(title)}</h1>
      <div class="sub">{subtitle} &middot; generated {generated}</div>
    </td>
    <td align="right" width="180"><span class="pill">{escape(_text(status, "Preliminary review"))}</span></td>
  </tr></table>

  <table class="snapshot"><tr>
    {_field("Borrower / guarantor", borrower_name)}
    {_field("Business / entity", business_name)}
    {_field("Requested amount", requested_amount)}
    {_field("Loan purpose", purpose)}
    {_field("Suggested path", program_fit)}
    {_field("Status", status)}
  </tr></table>

  <section class="card">
    <h2>Executive summary</h2>
    {exec_html}
  </section>

  <table class="cols"><tr>
    <td>{_prose_card("Recommended lender / vendor approach", recommended_angle)}</td>
    <td>{_bullet_card("Applications suggested", application_types) or _prose_card("Applications suggested", "Awaiting evidence.")}</td>
  </tr></table>

  {narrative_cards}

  {_bank_section(bank_months)}
  {_tax_section(tax_years)}

  {_metric_table(metric_rows)}

  <table class="cols"><tr>
    <td>{_simple_table("Documents reviewed", reviewed_docs, [("file", "Document"), ("type", "Type"), ("status", "Status")])}</td>
    <td>{_simple_table("Evidence coverage", coverage_rows, [("category", "Category"), ("status", "Status"), ("gap", "Evidence / gap")])}</td>
  </tr></table>

  {_simple_table("Missing confirmations", missing_rows, [("title", "Item"), ("priority", "Priority"), ("detail", "Detail")])}

  <table class="cols"><tr>
    <td>{_bullet_card("Strengths", strengths, "green")}</td>
    <td>{_bullet_card("Risks", risks, "amber")}</td>
  </tr></table>
  {_bullet_card("Mitigants", mitigants, "green")}

  <section class="card disclaimer">
    <h2>Important disclaimer</h2>
    <p>This packet is a Qualified Commercial underwriting support package generated from submitted evidence, chat answers, and AI analysis. It is not an official 1003, not a lender-specific application, not a commitment to lend, and not final underwriting approval. Sensitive account and identity numbers have been redacted. Unsupported values are marked as awaiting evidence.</p>
  </section>

  <div class="footer">Qualified Commercial LLC &mdash; CONFIDENTIAL. Internal and vendor underwriting support only.</div>
</body>
</html>
"""

    # 1) WeasyPrint — best CSS fidelity, but needs native libs (Pango/Cairo/GTK).
    pdf: bytes | None = None
    try:
        from weasyprint import HTML

        pdf = HTML(string=html_doc).write_pdf()
    except Exception:
        log.warning("lender-packet: WeasyPrint unavailable, trying PyMuPDF Story renderer")
    # 2) PyMuPDF Story — pure wheel, no system deps (the deployed path).
    if pdf is None:
        try:
            pdf = _render_html_pymupdf(html_doc)
        except Exception:
            log.exception("lender-packet: PyMuPDF Story render failed; using plain-text fallback")
    # 3) Last resort — sectioned plain-text PDF.
    if pdf is None:
        pdf = _minimal_pdf(fallback_lines)

    # Stamp every page with the CONFIDENTIAL watermark + per-page footer.
    return _apply_watermark(pdf) or pdf


def _render_html_pymupdf(html_doc: str) -> bytes | None:
    """Render HTML to a paginated landscape PDF using PyMuPDF's Story API."""
    import fitz

    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    story = fitz.Story(html=html_doc)
    media = fitz.paper_rect("letter-l")  # landscape
    frame = media + (34, 34, -34, -34)
    more = 1
    guard = 0
    while more and guard < 300:
        guard += 1
        device = writer.begin_page(media)
        more, _ = story.place(frame)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buf.getvalue() or None


def _apply_watermark(pdf: bytes) -> bytes | None:
    """Overlay a diagonal CONFIDENTIAL watermark + per-page footer on every page."""
    try:
        import fitz

        doc = fitz.open(stream=pdf, filetype="pdf")
        total = doc.page_count
        angle = math.radians(30)
        rot = fitz.Matrix(math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle), 0, 0)
        for index, page in enumerate(doc, start=1):
            rect = page.rect
            pivot = fitz.Point(rect.width / 2, rect.height / 2)
            page.insert_text(
                fitz.Point(rect.width / 2 - 230, rect.height / 2 + 20),
                "CONFIDENTIAL",
                fontsize=72,
                color=(0.82, 0.84, 0.9),
                fill_opacity=0.16,
                morph=(pivot, rot),
                overlay=True,
            )
            page.insert_text(
                fitz.Point(rect.width - 210, rect.height - 16),
                f"CONFIDENTIAL  ·  Page {index} of {total}",
                fontsize=7.5,
                color=(0.45, 0.5, 0.6),
                overlay=True,
            )
        return doc.tobytes()
    except Exception:
        log.exception("lender-packet: watermark overlay failed")
        return None
