"""Production Arrangement documents: the client presentation and the stage one
agreement schedules, rendered to branded PDF.

The presentation is what the agent hands the client before the agreement
stage: what got filled, and the deltas between today's verified production and
what the program commits to. It is explicitly not an agreement.

The agreement PDF carries Schedules A, B and E of the Production Commitment and
Capital Engagement Agreement with the frozen values and three signature blocks.
The schedule language below is operational wording derived from the design's
covenant rows (Addendum A.3 guideline, A.5/A.6 cure, A.9 sponsor-caused
exclusions); the agreement body proper is incorporated by reference to the
owner's template version, and any change to AGREEMENT_BODY should go through
counsel.

CSS MIRRORS qc_master_application.render_html so every branded PDF looks the
same; weasyprint is imported lazily by contract (native libraries).
"""

from __future__ import annotations

import hashlib
import html
from datetime import date, datetime
from typing import Any

from app.dealer_os.services.report_pdf import PDFUnavailableError
from app.services import production_arrangement as pa

PRESENTATION_FOOTER = "Production Arrangement presentation"
AGREEMENT_FOOTER = "Production Commitment and Capital Engagement Agreement"

DEALER_ANCHOR = "SIGNATURE - DEALER AUTHORIZED REPRESENTATIVE"
SPONSOR_ANCHOR = "SIGNATURE - SPONSOR"
QC_ANCHOR = "SIGNATURE - QUALIFIED COMMERCIAL LLC"
ELECTRONIC_PLACEHOLDER = "Electronic signature"
ELECTRONIC_DATE_PLACEHOLDER = "Signed electronically after review"
RECORDED_PLACEHOLDER = "Recorded signature"
RECORDED_DATE_PLACEHOLDER = "Recorded date"

BRAND_CSS = """
@page { size: Letter; margin: 0.55in 0.55in 0.58in; @bottom-left { content: "Qualified Commercial | %(footer)s"; color:#667085; font-size:8px; } @bottom-right { content: "Page " counter(page) " of " counter(pages); color:#667085; font-size:8px; } }
* { box-sizing:border-box; } body { font-family: Arial, sans-serif; color:#101828; font-size:9.2px; line-height:1.38; margin:0; }
h1 { font-size:21px; margin:0 0 3px; } h2 { font-size:13px; margin:16px 0 7px; padding-bottom:4px; border-bottom:2px solid #204ea1; color:#173b7a; }
h3 { font-size:10px; margin:10px 0 4px; } p { margin:3px 0; } .brand { display:flex; justify-content:space-between; gap:20px; border-bottom:4px solid #204ea1; padding-bottom:12px; margin-bottom:12px; }
table { width:100%; border-collapse:collapse; table-layout:fixed; margin:5px 0 10px; } th { background:#eaf0fb; color:#173b7a; text-align:left; } th,td { border:1px solid #cfd7e6; padding:5px 6px; vertical-align:top; overflow-wrap:anywhere; }
td.n, th.n { text-align:right; font-variant-numeric: tabular-nums; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:7px 14px; } .grid3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:7px 14px; } .field { border-bottom:1px solid #d6dce8; padding:2px 0 4px; } .label { display:block; color:#667085; font-size:7.5px; text-transform:uppercase; letter-spacing:.45px; font-weight:bold; }
.callout { background:#f7f9fc; border-left:4px solid #204ea1; padding:8px 10px; margin:7px 0; } .warning { border-color:#c99618; background:#fff9e8; } .ok { border-color:#0f7b4f; background:#eefaf3; } .danger { border-color:#b42318; background:#fdf1ef; } .muted { color:#667085; }
.big { font-size:18px; font-weight:bold; color:#173b7a; } .pos { color:#0f7b4f; font-weight:bold; } .neg { color:#b42318; font-weight:bold; }
.signature { page-break-inside:avoid; margin-top:18px; border:1px solid #aeb8ca; padding:14px; min-height:150px; } .signature-line { margin-top:40px; width:62%; border-top:1px solid #101828; padding-top:4px; }
.certificate-note { font-size:8px; color:#475467; } .nowrap { white-space:nowrap; } .pb { page-break-before:always; }
.bars { display:flex; align-items:flex-end; gap:2px; height:120px; border-bottom:1px solid #cfd7e6; padding:0 2px; } .bar { flex:1; display:flex; flex-direction:column-reverse; } .bar span { display:block; width:100%; } .b-repay { background:#1b4b9e; } .b-comm { background:#0d6e63; } .b-reserve { background:#8a6a1f; }
.legend span { display:inline-block; margin-right:12px; } .legend i { display:inline-block; width:10px; height:10px; margin-right:4px; vertical-align:middle; }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _money(value: Any, *, blank: str = "—") -> str:
    if value in (None, ""):
        return blank
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return blank


def _money2(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _num(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{int(f):,}" if f.is_integer() else f"{f:,.2f}"


def _signed_money(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    cls = "pos" if f >= 0 else "neg"
    return f'<span class="{cls}">{"+" if f >= 0 else "-"}{_money(abs(f))}</span>'


def _signed_num(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    cls = "pos" if f >= 0 else "neg"
    return f'<span class="{cls}">{"+" if f >= 0 else ""}{_num(f)}</span>'


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%B %d, %Y")
    if isinstance(value, date):
        return value.strftime("%B %d, %Y")
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return date.fromisoformat(text[:10]).strftime("%B %d, %Y")
    except ValueError:
        return text


def _field(label: str, value: Any) -> str:
    shown = value if (value not in (None, "") and not (isinstance(value, list) and not value)) else "—"
    if isinstance(shown, list):
        shown = ", ".join(str(v) for v in shown)
    body = shown if isinstance(shown, str) and shown.startswith("<span class=") else _e(shown)
    return f'<div class="field"><span class="label">{_e(label)}</span>{body}</div>'


def _blank_or(value: Any, render) -> str:
    if value in (None, "") or (isinstance(value, list) and not value):
        return '<span class="neg">[BLANK — NOT ENFORCEABLE]</span>'
    return render(value)


def _head(title: str, meta: dict[str, Any], *, footer: str, subtitle: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{BRAND_CSS.replace("%(footer)s", footer)}</style></head><body>
<div class="brand"><div><h1>{_e(title)}</h1><p>{_e(subtitle)}</p></div><div><b>{_e(meta.get("business_name") or "")}</b><br>{_e(meta.get("reference") or "")}<br>{_e(meta.get("generated_label") or "")}</div></div>
"""


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

def build_presentation_html(arrangement: dict[str, Any], computed: dict[str, Any], *, meta: dict[str, Any]) -> str:
    arr = {**pa.empty_arrangement(), **(arrangement or {})}
    e = computed["econ"]
    adv = computed["advance"]
    thr = computed["thresholds"]
    build = computed["buildout"]
    proj = computed["projection"]
    lot = computed["lot"]
    sponsor = computed.get("sponsor") or {}
    rows_on = [r for r in e["rows"] if r["on"]]
    term = adv["term"]

    out = [_head("Production Arrangement", meta, footer=PRESENTATION_FOOTER,
                 subtitle="Presentation for the dealer — what the program commits to, and what changes")]
    out.append(
        '<div class="callout warning"><b>Presentation only — not an agreement.</b> These figures are the basis for the '
        "Production Commitment and Capital Engagement Agreement. Nothing here is a commitment, approval or offer of "
        "credit until both agreements are executed and actual funding has occurred.</div>"
    )

    # 1. Parties
    out.append("<h2>1. Parties</h2><div class=\"grid3\">")
    out.append(_field("Dealer", arr.get("dealer_name")))
    out.append(_field("Dealer DBA", arr.get("dealer_dba")))
    out.append(_field("Dealer entity / state", " / ".join(p for p in (arr.get("dealer_entity"), arr.get("dealer_state")) if p)))
    out.append(_field("Sponsor", arr.get("sponsor_name")))
    out.append(_field("Sponsor platform", arr.get("sponsor_platform")))
    out.append(_field("Relationship manager", " · ".join(p for p in (arr.get("rm_name"), arr.get("rm_employer")) if p)))
    out.append("</div>")

    # 2. Today vs committed
    out.append("<h2>2. Today versus the commitment</h2>")
    out.append(
        f'<p>On <b>{_num(e["units"])}</b> retail units a month, today\'s verified production is '
        f'<b>{_num(e["cur_contracts"])}</b> contracts and <b>{_money(e["cur_gross"])}</b> gross. '
        f'The program commits to <b>{_num(e["contracts"])}</b> contracts and <b>{_money(e["gross"])}</b> gross — '
        f'{_signed_num(e["d_contracts"])} contracts and {_signed_money(e["d_gross"])} a month, '
        f'{_signed_money(e["d_gross_term"])} over the {term}-month term.</p>'
    )
    out.append("<table><thead><tr><th>Covered product</th><th class=\"n\">Today attach</th><th class=\"n\">Today premium</th>"
               "<th class=\"n\">Committed attach</th><th class=\"n\">Committed premium</th><th class=\"n\">Uplift / contract</th>"
               "<th class=\"n\">+ Contracts / mo</th><th class=\"n\">+ Gross / mo</th></tr></thead><tbody>")
    for r in rows_on:
        out.append(
            f'<tr><td>{_e(r["label"])}</td><td class="n">{_pct(r["cur_rate"], 0)}</td><td class="n">{_money(r["cur_premium"])}</td>'
            f'<td class="n">{_pct(r["rate"], 0)}</td><td class="n">{_money(r["premium"])}</td><td class="n">{_signed_money(r["uplift"])}</td>'
            f'<td class="n">{_signed_num(r["d_contracts"])}</td><td class="n">{_signed_money(r["d_gross"])}</td></tr>'
        )
    out.append(
        f'<tr><th>Total</th><th class="n"></th><th class="n">{_money(e["cur_gross"])} / mo</th><th class="n"></th>'
        f'<th class="n">{_money(e["gross"])} / mo</th><th class="n"></th><th class="n">{_signed_num(e["d_contracts"])}</th>'
        f'<th class="n">{_signed_money(e["d_gross"])}</th></tr></tbody></table>'
    )
    off = [r["label"] for r in e["rows"] if not r["on"]]
    if off:
        out.append(f'<p class="muted">Not covered: {_e(", ".join(off))}.</p>')

    # 3. Where the premium goes
    out.append("<h2>3. Where each VSC premium goes</h2><table><tbody>")
    for w in e.get("waterfall", []):
        out.append(f'<tr><th>{_e(w["label"])}</th><td class="n">{_money2(w["value"])}</td></tr>')
    out.append("</tbody></table>")
    out.append(
        f'<div class="grid3">{_field("Repayment withheld / month", _money(e["repay_m"]))}'
        f'{_field("Agency commissions / month", _money(e["comm_m"]))}{_field("Earned reserves / month", _money(e["reserve_m"]))}</div>'
    )

    # 4. Lot and baseline
    out.append("<h2>4. The lot and the verified baseline</h2><div class=\"grid3\">")
    out.append(_field("Vehicles in the lot", _num(arr.get("lot_units")) if arr.get("lot_units") else ""))
    out.append(_field("Average cost of car", _money(arr.get("avg_cost")) if arr.get("avg_cost") else ""))
    out.append(_field("Lot value", _money(lot["lot_value"]) if lot["lot_value"] else ""))
    out.append(_field("Average monthly retail units", _num(e["units"]) if e["units"] else ""))
    out.append(_field("Months of inventory on hand", f'{lot["months_of_inventory"]:.1f}' if lot.get("months_of_inventory") else ""))
    out.append(_field("Sell-through", _pct(lot["sell_through_pct"]) if lot.get("sell_through_pct") else ""))
    out.append(_field("Baseline period", f'{_date(arr.get("base_from"))} – {_date(arr.get("base_through"))}' if arr.get("base_from") or arr.get("base_through") else ""))
    out.append(_field("Cancellations / chargebacks", f'{_num(arr.get("cancels") or 0)} / {_num(arr.get("chargebacks") or 0)} a month'))
    out.append(_field("Evidence relied upon", arr.get("evidence")))
    out.append("</div>")
    if arr.get("seasonality"):
        out.append(f'<p class="muted"><b>Seasonality.</b> {_e(arr.get("seasonality"))}</p>')

    # 5. Advance and programme cost
    out.append("<h2>5. Advance and programme cost</h2><div class=\"grid3\">")
    out.append(_field("Requested facility type", arr.get("facility_type")))
    out.append(_field("Requested amount", _money(adv["requested"]) if adv["requested"] else ""))
    out.append(_field("Minimum activation amount", _money(arr.get("min_activation")) if arr.get("min_activation") else ""))
    out.append(_field("Term", f"{term} months"))
    out.append(_field("Dealer cost of funds", _pct(arr.get("dealer_cof")) if arr.get("dealer_cof") else ""))
    out.append(_field("Monthly facility debt service", _money(arr.get("debt_service")) if arr.get("debt_service") else ""))
    out.append(_field("Advance the repayment stream supports" if adv["sizing"] == "backsolve" else "Advance (fixed)", _money(adv["advance"])))
    out.append(_field("Implied return", _pct(adv["implied_rate"])))
    out.append(_field("All-in programme cost", _pct(adv["cost_rate"])))
    out.append("</div>")
    verdict = "ok" if adv["clears"] else "danger"
    out.append(
        f'<div class="callout {verdict}"><span class="big">{"+" if adv["spread"] >= 0 else ""}{adv["spread"]:.1f} points</span> — '
        + ("clears the 3 point underwriting floor." if adv["clears"] else "under the 3 point underwriting floor.")
        + "</div>"
    )
    out.append("<table><thead><tr><th>Programme cost line</th><th class=\"n\">Amount</th><th>When</th><th class=\"n\">Share</th></tr></thead><tbody>")
    for line in adv["cost_lines"]:
        out.append(f'<tr><td>{_e(line["label"])}</td><td class="n">{_money(line["amount"])}</td><td>{_e(line["when"])}</td><td class="n">{_pct(line["share_pct"]) if line.get("share_pct") is not None else "—"}</td></tr>')
    out.append(f'<tr><th>Total over the term</th><th class="n">{_money(adv["total_cost"])}</th><th></th><th class="n">100%</th></tr></tbody></table>')

    # 6. Policy buildout
    out.append("<h2>6. Policy buildout — does the product carry the payment?</h2>")
    for key in ("with", "without"):
        s = build["scenarios"][key]
        tone = "ok" if s["free"] else ("" if key == "with" else "warning")
        out.append(
            f'<div class="callout {tone}"><b>{_e(s["title"])}</b> — {_e(s["sub"])}<br>'
            f'Monthly payment {_money(s["payment"])} · funded by policies {_money(s["funded"])} ({_pct(s["funded_pct"])}) · '
            f'from operations {_money(s["from_operations"])} a month, {_money(s["total_from_operations"])} over the term · '
            f'product gross {_money(s["gross"])} a month.</div>'
        )
    if build["solve_rows"]:
        out.append(f'<p>To fund {_pct(build["fund_target_pct"], 0)} of the payment from policy production, the repayment withheld per contract would need to be:</p>')
        out.append("<table><thead><tr><th>Product</th><th class=\"n\">Contracts / mo</th><th class=\"n\">Today premium</th><th class=\"n\">Withhold / contract</th><th class=\"n\">Premium needed</th><th class=\"n\">Uplift</th></tr></thead><tbody>")
        for r in build["solve_rows"]:
            out.append(f'<tr><td>{_e(r["label"])}</td><td class="n">{_num(r["contracts"])}</td><td class="n">{_money(r["cur_premium"])}</td><td class="n">{_money(r["solve_repay"])}</td><td class="n">{_money(r["needed"])}</td><td class="n">{_signed_money(r["uplift"])}</td></tr>')
        out.append("</tbody></table>")

    # 7. Operative thresholds
    out.append("<h2>7. Operative thresholds (Addendum A.2, A.3 guideline)</h2>")
    out.append("<table><thead><tr><th>Covenant</th><th class=\"n\">Verified baseline</th><th class=\"n\">Operative requirement</th></tr></thead><tbody>")
    for r in thr["rows"]:
        if r.get("editable"):
            fmt = r["format"]
            def show(v: Any) -> str:
                if v is None:
                    return "N/A"
                return _money(v) if fmt == "money" else (_pct(v, 0) if fmt == "pct" else _num(v))
            op = show(r["operative"]) if not r["blank"] else '<span class="neg">[BLANK]</span>'
            out.append(f'<tr><td>{_e(r["label"])}</td><td class="n">{show(r["baseline"])}</td><td class="n">{op}{" *" if r.get("overridden") else ""}</td></tr>')
        else:
            out.append(f'<tr><td>{_e(r["label"])}</td><td class="n">N/A</td><td class="n">{_e(r["value"])}</td></tr>')
    out.append("</tbody></table>")
    out.append("<p class=\"muted\">* set by the desk above or below the guideline. Rolling three-month requirements are three times the monthly floor at 90% penetration.</p>")
    out.append("<table><thead><tr><th>Rolling three-month</th><th class=\"n\">Requirement</th></tr></thead><tbody>")
    for r in thr["rolling"]:
        v = _money(r["value"]) if r["format"] == "money" else (_pct(r["value"], 0) if r["format"] == "pct" else _num(r["value"]))
        out.append(f'<tr><td>{_e(r["label"])}</td><td class="n">{v}</td></tr>')
    out.append("</tbody></table>")

    # 8. Projection
    out.append("<h2>8. Repayment and earnout timeline</h2>")
    peak = proj["peak"] or 1
    bars = "".join(
        f'<div class="bar" title="Month {b["m"]}"><span class="b-repay" style="height:{max(1 if b["repay"] else 0, round(b["repay"] / peak * 110))}px"></span>'
        f'<span class="b-comm" style="height:{max(1 if b["comm"] else 0, round(b["comm"] / peak * 110))}px"></span>'
        f'<span class="b-reserve" style="height:{max(1 if b["reserve"] else 0, round(b["reserve"] / peak * 110))}px"></span></div>'
        for b in proj["bars"]
    )
    out.append(f'<div class="bars">{bars}</div>')
    out.append(
        '<p class="legend"><span><i class="b-repay"></i>Repayment</span><span><i class="b-comm"></i>Commissions</span><span><i class="b-reserve"></i>Earned reserves</span>'
        f' <span class="muted">{proj["span"]} months · originations stop at month {term}</span></p>'
    )
    retire = f"month {proj['retire_month']}" if proj.get("retire_month") else "not within the term"
    roll_off = f"{proj['roll_off_months']} months after the last contract"
    out.append(
        f'<div class="grid3">{_field("Total repayment over the term", _money(proj["totals"]["repay"]))}'
        f'{_field("Total commissions", _money(proj["totals"]["comm"]))}{_field("Total earned reserves", _money(proj["totals"]["reserve"]))}'
        f'{_field("Plateau (monthly)", _money(proj["plateau_monthly"]))}'
        f'{_field("Advance retired", retire)}{_field("Reserve roll-off", roll_off)}</div>'
    )

    # 9. Shortfall and cure
    cadence = {"month": "Billed monthly as it occurs", "quarter": "Netted quarterly", "balance": "Tracked as a running balance"}.get(arr.get("cadence") or "", "—")
    adj = arr.get("adj") or "none"
    adj_text = "None" if adj == "none" else (f'{_num(arr.get("adj_value") or 0)} basis points' if adj == "bps" else f'{_pct(arr.get("adj_value") or 0)} adjusted rate')
    out.append("<h2>9. Shortfall billing and cure</h2><div class=\"grid3\">")
    out.append(_field("Shortfall cadence", cadence))
    out.append(_field("Cure period", f'{_num(arr.get("cure_days"))} business days after notice' if arr.get("cure_days") else ""))
    out.append(_field("Corrective period", arr.get("corrective")))
    out.append(_field("Program rate adjustment", adj_text))
    out.append(_field("Remittance coverage", "125% of monthly debt service"))
    out.append(_field("Reporting deadline", "Fifth business day"))
    out.append("</div>")
    if arr.get("exclusions"):
        out.append(f'<p><b>Approved exclusions (A.5).</b> {_e(arr.get("exclusions"))}</p>')
    out.append('<p class="muted">Sponsor-caused shortfalls (A.9) — a shortage caused by the sponsor\'s own platform, remittance or administration failure is excluded from the dealer\'s shortfall.</p>')

    # 10. Sponsor economics + next steps
    out.append("<h2>10. Sponsor economics and next steps</h2><div class=\"grid3\">")
    out.append(_field("Sponsor markup", _pct(sponsor.get("markup_pct")) if sponsor.get("markup_pct") else ""))
    out.append(_field("Markup / month", _money(sponsor.get("markup_m"))))
    out.append(_field("Programme management / month", _money(sponsor.get("mgmt_m"))))
    out.append(_field("Sponsor total over the term", _money(sponsor.get("total_over_term"))))
    out.append(_field("Exclusivity window", f'{_num(arr.get("exclusivity"))} days from written approval' if arr.get("exclusivity") else ""))
    out.append(_field("Prepared by", " · ".join(p for p in (arr.get("rm_name"), arr.get("rm_email"), arr.get("rm_phone")) if p)))
    out.append("</div>")
    out.append(
        '<div class="callout"><b>What happens next.</b> Stage one — the Production Commitment and Capital Engagement '
        "Agreement (Schedules A, B, E) — is signed by the dealer, the sponsor and Qualified Commercial at approval. "
        "Stage two — the Program Activation and Production Agreement — is executed only after actual funding at or "
        "above the minimum activation amount.</div>"
    )
    out.append(f'<p class="certificate-note">Snapshot {_e(meta.get("snapshot_short") or "")} · {_e(meta.get("generated_label") or "")}</p>')
    out.append("</body></html>")
    return "".join(out)


# ---------------------------------------------------------------------------
# agreement: Schedules A, B, E + signature blocks
# ---------------------------------------------------------------------------

AGREEMENT_BODY: tuple[tuple[str, str], ...] = (
    ("Incorporation", "These Schedules form part of, and are incorporated into, the Production Commitment and Capital "
                      "Engagement Agreement (the \"Agreement\") between Qualified Commercial LLC as program manager, "
                      "the Dealer and the Sponsor named in Schedule A. Capitalised terms have the meaning given in the Agreement."),
    ("Production commitment", "The Dealer commits to route every Covered Product listed in Schedule B through the Sponsor "
                              "platform named in Schedule A and to maintain the operative thresholds in Schedule B from the "
                              "Production Commencement Date."),
    ("Exclusivity", "From written approval and for the exclusivity window stated in Schedule A, the Dealer will not seek "
                    "or accept a competing capital advance secured by the same production."),
    ("Activation", "No advance below the minimum activation amount activates the Program Activation and Production "
                   "Agreement; a prequalification, term sheet, approval or partial advance does not."),
    ("Shortfall and cure", "A production shortfall is measured on the cadence in Schedule E, billed as an Eligible Net "
                           "Remittance shortage, and may be cured within the cure period after notice. Approved exclusions "
                           "and sponsor-caused shortfalls (A.9) are excluded from the Dealer's shortfall."),
    ("Reporting", "The Dealer and the Sponsor deliver the monthly production report by the fifth business day of the "
                  "following month."),
)


def build_agreement_html(
    arrangement: dict[str, Any], computed: dict[str, Any], *, meta: dict[str, Any],
    sponsor: dict[str, Any] | None, signatures: list[dict[str, Any]] | None = None,
) -> str:
    arr = {**pa.empty_arrangement(), **(arrangement or {})}
    e = computed["econ"]
    thr = computed["thresholds"]
    sponsor = sponsor or {}
    signatures = signatures or []
    sig_by_party = {s.get("party"): s for s in signatures}
    rows_on = [r for r in e["rows"] if r["on"]]
    out = [_head(pa.STAGE_ONE_TITLE, meta, footer=AGREEMENT_FOOTER, subtitle="Schedules A, B and E — stage one, signed at approval")]
    out.append(f'<p class="muted">Agreement reference {_e(meta.get("reference") or "")} · Document version {_e(pa.DOCUMENT_VERSION)} · Revision {_e(meta.get("revision_no") or "")}</p>')
    for heading, text in AGREEMENT_BODY:
        out.append(f"<h3>{_e(heading)}</h3><p>{_e(text)}</p>")

    # Schedule A
    out.append("<h2>Schedule A — Parties, facility and platform</h2><div class=\"grid\">")
    out.append(_field("Dealer full legal name", _blank_or(arr.get("dealer_name"), _e)))
    out.append(_field("Dealer entity type / state of formation", _blank_or(" / ".join(p for p in (arr.get("dealer_entity"), arr.get("dealer_state")) if p), _e)))
    out.append(_field("Dealer DBA", arr.get("dealer_dba")))
    out.append(_field("Dealer address", _blank_or(arr.get("dealer_address"), _e)))
    out.append(_field("Dealer authorized signer", _blank_or(" — ".join(p for p in (arr.get("dealer_signer_name"), arr.get("dealer_signer_title")) if p), _e)))
    out.append(_field("Sponsor full legal name", _blank_or(sponsor.get("name") or arr.get("sponsor_name"), _e)))
    out.append(_field("Sponsor entity type / state of formation", _blank_or(" / ".join(p for p in (sponsor.get("entity_type") or arr.get("sponsor_entity"), sponsor.get("state_of_formation") or arr.get("sponsor_state")) if p), _e)))
    out.append(_field("Sponsor principal address", sponsor.get("principal_address") or arr.get("sponsor_address")))
    out.append(_field("Sponsor platform", _blank_or(arr.get("sponsor_platform"), _e)))
    out.append(_field("Sponsor notice email", _blank_or(arr.get("sponsor_email") or sponsor.get("notice_email"), _e)))
    agreement_ref = (sponsor.get("agreement") or {})
    out.append(_field("Sponsor referral-protection agreement", f'{agreement_ref.get("contract_number", "")} · signed {_date(agreement_ref.get("signed_at"))}' if agreement_ref else ""))
    out.append(_field("Relationship manager", _blank_or(" · ".join(p for p in (arr.get("rm_name"), arr.get("rm_employer"), arr.get("rm_email"), arr.get("rm_phone")) if p), _e)))
    out.append(_field("Requested facility type", _blank_or(arr.get("facility_type"), _e)))
    out.append(_field("Requested amount", _blank_or(arr.get("requested"), _money)))
    out.append(_field("Minimum activation amount", _blank_or(arr.get("min_activation"), _money)))
    out.append(_field("Term", _blank_or(arr.get("term"), lambda v: f"{_num(v)} months")))
    out.append(_field("Dealer cost of funds", _blank_or(arr.get("dealer_cof"), _pct)))
    out.append(_field("Exclusivity window", _blank_or(arr.get("exclusivity"), lambda v: f"{_num(v)} days from written approval")))
    out.append("</div>")

    # Schedule B
    out.append("<h2>Schedule B — Covered Products, economics and operative thresholds</h2>")
    out.append("<table><thead><tr><th>Covered product</th><th class=\"n\">Attachment</th><th class=\"n\">Premium</th><th class=\"n\">Repayment withheld</th><th class=\"n\">Commission</th><th class=\"n\">Admin fee</th><th class=\"n\">Retention</th><th class=\"n\">Term</th></tr></thead><tbody>")
    for r in rows_on:
        out.append(
            f'<tr><td>{_e(r["label"])}</td><td class="n">{_pct(r["rate"], 0)}</td><td class="n">{_money(r["premium"])}</td>'
            f'<td class="n">{_money(r["repay"])}</td><td class="n">{_pct(r["comm_pct"], 0)}</td><td class="n">{_money(r["admin"])}</td>'
            f'<td class="n">{_pct(r["retention_pct"], 0)}</td><td class="n">{_num(r["term"])} mo</td></tr>'
        )
    if not rows_on:
        out.append('<tr><td colspan="8"><span class="neg">[NO COVERED PRODUCTS — NOT ENFORCEABLE]</span></td></tr>')
    out.append("</tbody></table>")
    out.append(f'<div class="grid"><div class="field"><span class="label">Baseline period (A.1)</span>{_blank_or(arr.get("base_from"), _date)} – {_blank_or(arr.get("base_through"), _date)}</div>'
               f'<div class="field"><span class="label">Evidence relied upon</span>{_blank_or(arr.get("evidence"), lambda v: _e(", ".join(v) if isinstance(v, list) else v))}</div></div>')
    out.append("<table><thead><tr><th>Operative threshold (A.2)</th><th class=\"n\">Verified baseline</th><th class=\"n\">Requirement</th></tr></thead><tbody>")
    for r in thr["rows"]:
        if r.get("editable"):
            fmt = r["format"]
            def show(v: Any) -> str:
                if v is None:
                    return "N/A"
                return _money(v) if fmt == "money" else (_pct(v, 0) if fmt == "pct" else _num(v))
            op = show(r["operative"]) if not r["blank"] else '<span class="neg">[BLANK — NOT ENFORCEABLE]</span>'
            out.append(f'<tr><td>{_e(r["label"])}</td><td class="n">{show(r["baseline"])}</td><td class="n">{op}</td></tr>')
        else:
            out.append(f'<tr><td>{_e(r["label"])}</td><td class="n">N/A</td><td class="n">{_e(r["value"])}</td></tr>')
    out.append("</tbody></table>")
    out.append("<table><thead><tr><th>Rolling three-month (A.3)</th><th class=\"n\">Requirement</th></tr></thead><tbody>")
    for r in thr["rolling"]:
        v = _money(r["value"]) if r["format"] == "money" else (_pct(r["value"], 0) if r["format"] == "pct" else _num(r["value"]))
        out.append(f'<tr><td>{_e(r["label"])}</td><td class="n">{v}</td></tr>')
    out.append("</tbody></table>")

    # Schedule E
    cadence = {"month": "Billed monthly as it occurs", "quarter": "Netted quarterly", "balance": "Tracked as a running balance"}.get(arr.get("cadence") or "", "")
    adj = arr.get("adj") or "none"
    adj_text = "None" if adj == "none" else (f'{_num(arr.get("adj_value") or 0)} basis points' if adj == "bps" else f'{_pct(arr.get("adj_value") or 0)} adjusted rate')
    out.append("<h2>Schedule E — Shortfall billing, cure and exclusions</h2><div class=\"grid\">")
    out.append(_field("Shortfall cadence", _blank_or(cadence, _e)))
    out.append(_field("Remittance shortage cure (A.6)", _blank_or(arr.get("cure_days"), lambda v: f"{_num(v)} business days after notice")))
    out.append(_field("Corrective period", _blank_or(arr.get("corrective"), _e)))
    out.append(_field("Program rate adjustment", adj_text))
    out.append(_field("Minimum remittance coverage", "125% of monthly Funding Facility debt service"))
    out.append(_field("Monthly reporting deadline", "Fifth business day"))
    out.append("</div>")
    out.append(f'<p><b>Approved exclusions (A.5).</b> {_e(arr.get("exclusions") or "None.")}</p>')
    out.append("<p><b>Sponsor-caused exclusions (A.9).</b> A shortfall caused by the Sponsor's platform, remittance or administration failure is not a Dealer shortfall and is not billed to the Dealer.</p>")

    # Signature blocks
    def block(anchor: str, party_label: str, who: str, name: str, title: str, electronic: bool, sig: dict[str, Any] | None) -> str:
        placeholder = ELECTRONIC_PLACEHOLDER if electronic else RECORDED_PLACEHOLDER
        date_placeholder = ELECTRONIC_DATE_PLACEHOLDER if electronic else RECORDED_DATE_PLACEHOLDER
        return (
            f'<div class="signature"><b>{_e(anchor)}</b><div class="muted">{_e(party_label)} — {_e(who)}</div>'
            f'<div class="signature-line">{placeholder}</div>'
            f'<div class="grid" style="margin-top:12px"><div><span class="label">Name</span>{_e(name or "")}</div>'
            f'<div><span class="label">Title</span>{_e(title or "")}</div>'
            f'<div><span class="label">Date</span>{date_placeholder}</div>'
            f'<div><span class="label">Document hash</span>See completion certificate</div></div></div>'
        )

    out.append('<h2 class="pb">Signatures</h2>')
    out.append(block(DEALER_ANCHOR, arr.get("dealer_name") or "Dealer", "Dealer", arr.get("dealer_signer_name") or "", arr.get("dealer_signer_title") or "", True, sig_by_party.get("dealer")))
    out.append(block(SPONSOR_ANCHOR, sponsor.get("name") or arr.get("sponsor_name") or "Sponsor", "Warranty provider, administrator, or sales organization", sponsor.get("signer_name") or "", sponsor.get("signer_title") or "", False, sig_by_party.get("sponsor")))
    out.append(block(QC_ANCHOR, "Qualified Commercial LLC", "Program manager", "", "", False, sig_by_party.get("qc")))
    out.append("</body></html>")
    return "".join(out)


# ---------------------------------------------------------------------------
# rendering + storage keys
# ---------------------------------------------------------------------------

def render_pdf(html_doc: str) -> tuple[bytes, str]:
    """HTML -> (pdf bytes, sha256). Raises PDFUnavailableError when weasyprint
    (or its native libraries) is not importable in this runtime."""
    try:
        from weasyprint import HTML  # noqa: PLC0415 — lazy by contract (native libs)
    except Exception as exc:  # noqa: BLE001
        raise PDFUnavailableError(str(exc)) from exc
    pdf = HTML(string=html_doc).write_pdf()
    if not pdf:
        raise RuntimeError("The production arrangement could not be rendered.")
    return pdf, hashlib.sha256(pdf).hexdigest()


def presentation_key(profile_id: Any, package_id: Any, sha256: str) -> str:
    return f"production-packages/{profile_id}/{package_id}/presentation-{sha256[:16]}.pdf"


def revision_key(profile_id: Any, package_id: Any, revision_no: int, phase: str, sha256: str, ext: str = "pdf") -> str:
    return f"production-packages/{profile_id}/{package_id}/r{revision_no}/stage1-{phase}-{sha256[:16]}.{ext}"


def scan_key(profile_id: Any, package_id: Any, revision_no: int, party: str, sha256: str, ext: str) -> str:
    return f"production-packages/{profile_id}/{package_id}/r{revision_no}/{party}-scan-{sha256[:16]}.{ext}"
