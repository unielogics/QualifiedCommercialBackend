"""Template values for the two Production Package agreements.

Pure mapping from what the package holds -- the flat arrangement
(``production_arrangement.FIELD_RULES`` plus the stage-two keys), the
``compute()`` result, the sponsor snapshot, the parties block, the dealer
file context and the revision meta -- onto the ``data-field`` slots and
``data-check`` boxes of ``production_agreements`` templates.

Conventions:

* money prints ``$1,234,567`` (two decimals only when there are cents),
  percentages ``62%`` (one decimal when needed), dates ``September 03, 2026``;
* anything unknown prints blank -- the owner's choice for stage-one slots the
  package does not collect -- except Schedule C / 3, which prints its ``$0`` /
  ``None`` defaults so a blank there cannot be read as an undisclosed fee;
* signature-date slots are never filled here: the stamper writes them on the
  anchors (``STAMPED_SLOTS``);
* the stage-two keys another engineer is adding to the arrangement are all
  optional -- absent keys print blank and tick nothing.

Both builders return ``(values, checks)``: the slot values and the set of
check keys to tick, ready for ``production_agreements.fill_template``.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.services import production_arrangement as pa

QC_LEGAL_NAME = "Qualified Commercial LLC"
OWNER_ROWS = 5
NOTES_SPLIT_AT = 90
DEFAULT_AUDIT_DISCREPANCY_PCT = 5
DEFAULT_AMOUNT = "$0"
DEFAULT_PURPOSE = "None"
ONSITE_SOURCE = "Verified onsite review"

COMMITMENT_STAMPED_SLOTS: frozenset[str] = frozenset({"sig_qc_date", "sig_dealer_date", "sig_sponsor_date", "s2_ack_date"})
ACTIVATION_STAMPED_SLOTS: frozenset[str] = frozenset({
    "s2_ack_date", "s5_qc_date", "s5_dealer_date", "s5_sponsor_date", "s5_fp_date",
    "ms_qc_date", "ms_dealer_date", "ms_sponsor_date",
})
STAMPED_SLOTS: dict[str, frozenset[str]] = {
    "commitment_v1": COMMITMENT_STAMPED_SLOTS,
    "activation_v1": ACTIVATION_STAMPED_SLOTS,
}

SUPPORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("application_packaging", "Application and packaging support"),
    ("reporting_technology", "Reporting technology"),
    ("ongoing_monitoring", "Ongoing monitoring"),
    ("first_risk_reserve", "First-risk or reserve support"),
    ("capital_health", "Capital Health Services"),
    ("controlled_account", "Controlled-account support"),
    ("product_admin_platform", "Product-administration platform"),
    ("preferential_economics", "Preferential program economics"),
    ("other", "Other"),
)
RM_COMP_OPTIONS: tuple[tuple[str, str], ...] = (
    ("salary", "Salary"),
    ("fixed_recurring", "Fixed recurring account-management compensation"),
    ("hourly", "Hourly compensation"),
    ("disclosed_product", "Disclosed Covered Product sales or servicing compensation"),
    ("fixed_implementation", "Fixed implementation compensation for documented services"),
    ("other", "Other lawful compensation"),
)
FINANCING_COST_OPTIONS: tuple[tuple[str, str], ...] = (("no", "No"), ("yes", "Yes"))
SBA_OPTIONS: tuple[tuple[str, str], ...] = (
    ("not_sba", "Not an SBA transaction"),
    ("sba", "SBA transaction; required SBA compensation documentation attached"),
)
USE_OF_FUNDS_KEYS: tuple[str, ...] = (
    "inventory", "debt_payoff", "working_capital", "equipment", "real_estate", "program_implementation", "other",
)
ROLLING_SLOTS: dict[str, str] = {
    "Retail units": "a4_units",
    "VSC contracts": "a4_vsc",
    "VSC gross production": "a4_vsc_gross",
    "Total Covered Product gross": "a4_cp_gross",
    "Aggregate Eligible Net Remittance": "a4_remittance",
    "VSC penetration": "a4_penetration",
}
# funding attestation keys the caller may pass to override the arrangement at send time
FUNDING_OVERRIDE_KEYS: tuple[str, ...] = (
    "funded_amount", "funding_date", "activation_date", "commencement", "maturity",
    "funding_party", "funding_party_name", "funding_docs_executed_date",
)


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def text(value: Any) -> str:
    """Trimmed text; None and blanks are ''; lists join with ', '."""
    if value is None or isinstance(value, bool):
        return "" if value is None else ("Yes" if value else "No")
    if isinstance(value, (list, tuple)):
        return ", ".join(t for t in (text(v) for v in value) if t)
    return str(value).strip()


def first(*values: Any) -> str:
    for value in values:
        t = text(value)
        if t:
            return t
    return ""


def number(value: Any) -> float | None:
    """A float, or None for blanks and junk. Accepts '$1,200,000' and '62%'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        raw = re.sub(r"[,$%\s]", "", str(value))
        if not raw:
            return None
        try:
            out = float(raw)
        except ValueError:
            return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def money(value: Any) -> str:
    n = number(value)
    if n is None:
        return ""
    sign = "-" if n < 0 else ""
    n = abs(n)
    body = f"{n:,.0f}" if abs(n - round(n)) < 0.005 else f"{n:,.2f}"
    return f"{sign}${body}"


def pct(value: Any) -> str:
    n = number(value)
    if n is None:
        return ""
    r = round(n, 1)
    return f"{int(r)}%" if r == int(r) else f"{r:.1f}%"


def count(value: Any) -> str:
    n = number(value)
    if n is None:
        return ""
    return f"{int(round(n)):,}" if abs(n - round(n)) < 1e-9 else f"{n:,.2f}"


def fmt_date(value: Any) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%B %d, %Y")
    t = text(value)
    if not t:
        return ""
    try:
        return date.fromisoformat(t[:10]).strftime("%B %d, %Y")
    except ValueError:
        return t


def metric(fmt, value: Any) -> str:
    """A computed measure: zero means the inputs were blank, so it prints blank too."""
    n = number(value)
    return "" if n is None or n == 0 else fmt(n)


def yes_no(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    t = text(value)
    if t.lower() in ("true", "yes", "y", "1", "sent"):
        return "Yes"
    if t.lower() in ("false", "no", "n", "0"):
        return "No"
    return t


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return text(value).lower() in ("true", "yes", "y", "1")


def name_and_title(name: Any, title: Any) -> str:
    return ", ".join(p for p in (text(name), text(title)) if p)


def split_notes(value: Any, at: int = NOTES_SPLIT_AT) -> tuple[str, str]:
    """Two underline-length lines: the first breaks on a word boundary near ``at``."""
    t = re.sub(r"\s+", " ", text(value))
    if len(t) <= at:
        return t, ""
    cut = t.rfind(" ", 0, at + 1)
    if cut <= 0:
        cut = at
    return t[:cut].strip(), t[cut:].strip()


def adjustment_text(arr: dict[str, Any]) -> str:
    """Addendum A.8 / certificate line 14, worded as ``preview_rows(stage=2)`` does."""
    adj = text(arr.get("adj")) or "none"
    value = pa._num(arr.get("adj_value"))
    if adj == "bps":
        return f"{pa.jsround(value)} basis points"
    if adj == "rate":
        return f"{pa.pct(value)} adjusted rate"
    return "None"


def _slugs(values: Any, options: tuple[tuple[str, str], ...]) -> set[str]:
    """Selected option slugs; accepts slugs or template labels, ignores the rest."""
    by_label = {label.lower(): slug for slug, label in options}
    known = {slug for slug, _ in options}
    out: set[str] = set()
    items = values if isinstance(values, (list, tuple, set, frozenset)) else ([values] if values else [])
    for item in items:
        t = text(item)
        if not t:
            continue
        if t in known:
            out.add(t)
        elif t.lower() in by_label:
            out.add(by_label[t.lower()])
    return out


def _option_slug(value: Any, options: tuple[tuple[str, str], ...]) -> str | None:
    t = text(value)
    if not t:
        return None
    for slug, label in options:
        if t == slug or t.lower() == label.lower() or (len(t) > 3 and label.lower().startswith(t.lower())):
            return slug
    return None


# ---------------------------------------------------------------------------
# shared blocks
# ---------------------------------------------------------------------------

def _sponsor_name(arr: dict[str, Any], sponsor: dict[str, Any]) -> str:
    return first(sponsor.get("name"), arr.get("sponsor_name"))


def _dealer_name(arr: dict[str, Any], parties: dict[str, Any]) -> str:
    return first(arr.get("dealer_name"), (parties.get("dealer") or {}).get("name"))


def _dealer_signer(arr: dict[str, Any], parties: dict[str, Any]) -> tuple[str, str]:
    dealer = parties.get("dealer") or {}
    return first(arr.get("dealer_signer_name"), dealer.get("signer_name")), first(arr.get("dealer_signer_title"), dealer.get("signer_title"))


def _header_parties(arr: dict[str, Any], sponsor: dict[str, Any], parties: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, str]:
    qc = file_ctx.get("qc") or {}
    return {
        "qc_address": first(qc.get("address"), qc.get("notice_address")),
        "dealer_legal_name": _dealer_name(arr, parties),
        "dealer_state": text(arr.get("dealer_state")),
        "dealer_entity_type": text(arr.get("dealer_entity")),
        "dealer_dba": text(arr.get("dealer_dba")),
        "dealer_address": text(arr.get("dealer_address")),
        "sponsor_legal_name": _sponsor_name(arr, sponsor),
        "sponsor_state": first(arr.get("sponsor_state"), sponsor.get("state_of_formation")),
        "sponsor_entity_type": first(arr.get("sponsor_entity"), sponsor.get("entity_type")),
        "sponsor_address": first(arr.get("sponsor_address"), sponsor.get("principal_address")),
        "sponsor_logo_text": _sponsor_name(arr, sponsor),
    }


def _identity(arr: dict[str, Any], parties: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, str]:
    """§9.1 -- the arrangement first, then what the dealer file already holds."""
    ident = file_ctx.get("identity") or {}
    return {
        "identity_legal_name": first(arr.get("dealer_name"), ident.get("legal_name"), (parties.get("dealer") or {}).get("name")),
        "identity_dba": first(arr.get("dealer_dba"), ident.get("dba")),
        "identity_entity_type": first(arr.get("dealer_entity"), ident.get("entity_type")),
        "identity_state": first(arr.get("dealer_state"), ident.get("state")),
        "identity_formation_date": fmt_date(first(arr.get("identity_formation_date"), ident.get("formation_date"))),
        "identity_ein": first(arr.get("identity_ein"), ident.get("ein")),
        "identity_naics": first(arr.get("identity_naics"), ident.get("naics")),
        "identity_address": first(arr.get("dealer_address"), ident.get("address")),
        "identity_license": first(arr.get("identity_license"), ident.get("license")),
        "identity_website": first(arr.get("identity_website"), ident.get("website")),
    }


def _owners(arr: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, str]:
    """§9.2 -- up to five rows plus the total; the arrangement's schedule wins over the file's."""
    rows = arr.get("owners")
    if not isinstance(rows, list) or not rows:
        rows = file_ctx.get("owners") or []
    rows = [r for r in rows if isinstance(r, dict)][:OWNER_ROWS]
    out: dict[str, str] = {}
    total = 0.0
    any_pct = False
    for i in range(1, OWNER_ROWS + 1):
        row = rows[i - 1] if i <= len(rows) else {}
        p = number(row.get("pct"))
        if p is not None:
            total += p
            any_pct = True
        out[f"owner_{i}_name"] = text(row.get("name"))
        out[f"owner_{i}_pct"] = pct(p)
        out[f"owner_{i}_title"] = text(row.get("title"))
        out[f"owner_{i}_email"] = text(row.get("email"))
        out[f"owner_{i}_phone"] = text(row.get("phone"))
        out[f"owner_{i}_auth"] = yes_no(row.get("auth"))
    out["owner_total_pct"] = pct(total) if any_pct else ""
    return out


def _notices(prefix: str, arr: dict[str, Any], sponsor: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, str]:
    qc = file_ctx.get("qc") or {}
    dealer = file_ctx.get("dealer_notice") or {}
    sponsor_notice = file_ctx.get("sponsor_notice") or {}
    return {
        f"{prefix}_notice_qc_email": text(qc.get("notice_email")),
        f"{prefix}_notice_dealer_email": first(arr.get("dealer_notice_email"), dealer.get("email")),
        f"{prefix}_notice_sponsor_email": first(arr.get("sponsor_email"), sponsor.get("notice_email"), sponsor_notice.get("email")),
        f"{prefix}_notice_qc_address": first(qc.get("notice_address"), qc.get("address")),
        f"{prefix}_notice_dealer_address": first(dealer.get("address"), arr.get("dealer_address")),
        f"{prefix}_notice_sponsor_address": first(arr.get("sponsor_address"), sponsor.get("principal_address"), sponsor_notice.get("address")),
    }


def _support_checks(arr: dict[str, Any]) -> set[str]:
    slugs = _slugs(arr.get("program_support"), SUPPORT_OPTIONS)
    if text(arr.get("program_support_other")):
        slugs.add("other")
    return {f"support.{s}" for s in slugs}


def _schedule_2(arr: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """Schedule B / 2 -- the relationship manager and their compensation category.
    ``s2_ack_date`` is stamped with the manager's signature, never filled."""
    slugs = _slugs(arr.get("rm_comp_categories"), RM_COMP_OPTIONS)
    if text(arr.get("rm_comp_other")):
        slugs.add("other")
    values = {
        "s2_rm_name": text(arr.get("rm_name")),
        "s2_rm_employer": text(arr.get("rm_employer")),
        "s2_rm_email": text(arr.get("rm_email")),
        "s2_rm_phone": text(arr.get("rm_phone")),
        "s2_comp_other": text(arr.get("rm_comp_other")),
        "s2_ack_name": text(arr.get("rm_name")),
    }
    return values, {f"rm_comp.{s}" for s in slugs}


def _schedule_3(arr: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """Schedule C / 3 -- compensation and conflict disclosure with the $0 / None defaults printed."""
    values = {
        "s3_fp_qc_amount": first(arr.get("comp_fp_qc_amount"), DEFAULT_AMOUNT),
        "s3_fp_qc_purpose": first(arr.get("comp_fp_qc_purpose"), DEFAULT_PURPOSE),
        "s3_fp_sponsor_amount": first(arr.get("comp_fp_sponsor_amount"), DEFAULT_AMOUNT),
        "s3_fp_sponsor_purpose": first(arr.get("comp_fp_sponsor_purpose"), DEFAULT_PURPOSE),
        "s3_dealer_qc_post_amount": first(arr.get("comp_dealer_qc_amount"), DEFAULT_AMOUNT),
        "s3_dealer_qc_post_purpose": first(arr.get("comp_dealer_qc_purpose"), DEFAULT_PURPOSE),
        "s3_dealer_sponsor_post_amount": first(arr.get("comp_dealer_sponsor_amount"), DEFAULT_AMOUNT),
        "s3_dealer_sponsor_post_purpose": first(arr.get("comp_dealer_sponsor_purpose"), DEFAULT_PURPOSE),
        "s3_economics_1": text(arr.get("program_economics_1")),
        "s3_economics_2": text(arr.get("program_economics_2")),
        "s3_economics_3": text(arr.get("program_economics_3")),
        "s3_financing_cost_explain": text(arr.get("financing_cost_explain")),
        "s3_conflict_1": text(arr.get("conflict_disclosure_1")),
        "s3_conflict_2": text(arr.get("conflict_disclosure_2")),
    }
    checks: set[str] = set()
    fc = _option_slug(arr.get("financing_cost_included"), FINANCING_COST_OPTIONS)
    if fc:
        checks.add(f"financing_cost.{fc}")
    sba = _option_slug(arr.get("sba_status"), SBA_OPTIONS)
    if sba:
        checks.add(f"sba.{sba}")
    return values, checks


def _schedule_4(arr: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in (1, 2, 3):
        out[f"s4_protected_{i}_name"] = text(arr.get(f"protected_{i}_name"))
        out[f"s4_protected_{i}_rel"] = text(arr.get(f"protected_{i}_rel"))
        out[f"s4_protected_{i}_date"] = fmt_date(arr.get(f"protected_{i}_date"))
        out[f"s4_protected_{i}_txn"] = text(arr.get(f"protected_{i}_txn"))
    for i in (1, 2, 3, 4):
        out[f"s4_existing_{i}_name"] = text(arr.get(f"existing_{i}_name"))
        out[f"s4_existing_{i}_rel"] = text(arr.get(f"existing_{i}_rel"))
        out[f"s4_existing_{i}_info"] = text(arr.get(f"existing_{i}_info"))
    return out


def _vsc_row(computed: dict[str, Any]) -> dict[str, Any]:
    econ = computed.get("econ") or {}
    return next((r for r in econ.get("rows") or [] if r.get("key") == pa.PRIMARY_PRODUCT), {})


def _threshold_rows(computed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = (computed.get("thresholds") or {}).get("rows") or []
    return {r["key"]: r for r in rows if r.get("editable")}


def _funding_party_name(arr: dict[str, Any], sponsor: dict[str, Any]) -> str:
    named = text(arr.get("funding_party_name"))
    if named:
        return named
    party = text(arr.get("funding_party"))
    if party == "Sponsor":
        return _sponsor_name(arr, sponsor)
    if party == "Lender":
        return ""
    return party


def _qc_signer(file_ctx: dict[str, Any]) -> tuple[str, str]:
    qc = file_ctx.get("qc") or {}
    return text(qc.get("signer_name")), text(qc.get("signer_title"))


def _sponsor_signer(sponsor: dict[str, Any]) -> str:
    return name_and_title(sponsor.get("signer_name"), sponsor.get("signer_title"))


# ---------------------------------------------------------------------------
# stage one: Production Commitment and Capital Engagement Agreement
# ---------------------------------------------------------------------------

def commitment_values(
    arrangement: dict[str, Any], computed: dict[str, Any], sponsor: dict[str, Any] | None,
    parties: dict[str, Any], file_ctx: dict[str, Any], meta: dict[str, Any],
) -> tuple[dict[str, str], set[str]]:
    arr = {**pa.empty_arrangement(), **(arrangement or {})}
    sponsor = sponsor or {}
    parties = parties or {}
    file_ctx = file_ctx or {}
    meta = meta or {}
    econ = computed.get("econ") or {}
    vsc = _vsc_row(computed)
    dealer_signer, dealer_title = _dealer_signer(arr, parties)
    qc_name, qc_title = _qc_signer(file_ctx)
    evidence = text(arr.get("evidence"))
    source = evidence or ONSITE_SOURCE
    notes_1, notes_2 = split_notes(arr.get("seasonality"))

    def measure(fmt, value: Any, *, zero_is_blank: bool = True) -> tuple[str, str]:
        shown = metric(fmt, value) if zero_is_blank else fmt(value)
        return shown, (source if shown else "")

    units_shown, units_source = measure(count, econ.get("units"))
    vsc_shown, vsc_source = measure(count, vsc.get("contracts"))
    pen_shown, pen_source = measure(pct, vsc.get("rate"))
    vsc_gross_shown, vsc_gross_source = measure(money, vsc.get("gross"))
    cp_gross_shown, cp_gross_source = measure(money, econ.get("gross"))
    cancel_shown, cancel_source = measure(count, arr.get("cancels"), zero_is_blank=False)
    chargeback_shown, chargeback_source = measure(count, arr.get("chargebacks"), zero_is_blank=False)

    values: dict[str, str] = {
        "agreement_no": text(meta.get("agreement_no")),
        "effective_date": fmt_date(meta.get("effective_date")),
        "written_approval_date": fmt_date(first(arr.get("written_approval_date"), meta.get("written_approval_date"))),
        "outside_funding_date": fmt_date(first(arr.get("outside_funding_date"), meta.get("outside_funding_date"))),
        **_header_parties(arr, sponsor, parties, file_ctx),
        "minimum_activation_amount": money(arr.get("min_activation")),
        "exclusivity_days": count(arr.get("exclusivity")),
        **_identity(arr, parties, file_ctx),
        **_owners(arr, file_ctx),
        # Schedule A
        "sa_dealer_legal_name": _dealer_name(arr, parties),
        "sa_facility_type": text(arr.get("facility_type")),
        "sa_requested_amount": money(arr.get("requested")),
        "sa_minimum_activation_amount": money(arr.get("min_activation")),
        "sa_exclusivity_days": count(arr.get("exclusivity")),
        "sa_sponsor_platform": first(arr.get("sponsor_platform"), sponsor.get("platform")),
        "sa_products_other": text(arr.get("products_other")),
        "sa_support_other": text(arr.get("program_support_other")),
        **_notices("sa", arr, sponsor, file_ctx),
        # Schedule E
        "se_baseline_from": fmt_date(arr.get("base_from")),
        "se_baseline_through": fmt_date(arr.get("base_through")),
        "se_evidence": evidence,
        "se_units_baseline": units_shown,
        "se_units_source": units_source,
        "se_vsc_baseline": vsc_shown,
        "se_vsc_source": vsc_source,
        "se_pen_baseline": pen_shown,
        "se_pen_source": pen_source,
        "se_vsc_gross_baseline": vsc_gross_shown,
        "se_vsc_gross_source": vsc_gross_source,
        "se_cp_gross_baseline": cp_gross_shown,
        "se_cp_gross_source": cp_gross_source,
        "se_cancel_baseline": cancel_shown,
        "se_cancel_source": cancel_source,
        "se_chargeback_baseline": chargeback_shown,
        "se_chargeback_source": chargeback_source,
        "se_notes_1": notes_1,
        "se_notes_2": notes_2,
        "se_verified_by": text(arr.get("rm_name")),
        "se_dealer_confirm": dealer_signer,
        "se_date": fmt_date(meta.get("generated_on")),
        # signature page
        "sig_qc_name": qc_name,
        "sig_qc_title": qc_title,
        "sig_dealer_legal_name": _dealer_name(arr, parties),
        "sig_dealer_name": name_and_title(dealer_signer, dealer_title),
        "sig_sponsor_legal_name": _sponsor_name(arr, sponsor),
        "sig_sponsor_name": _sponsor_signer(sponsor),
    }
    s2_values, s2_checks = _schedule_2(arr)
    s3_values, s3_checks = _schedule_3(arr)
    values.update(s2_values)
    values.update(s3_values)
    values.update(_schedule_4(arr))

    checks: set[str] = {f"products.{k}" for k in econ.get("on") or [] if k in pa.PRODUCT_KEYS}
    if text(arr.get("products_other")):
        checks.add("products.other")
    checks |= _support_checks(arr) | s2_checks | s3_checks
    return values, checks


# ---------------------------------------------------------------------------
# stage two: Program Activation and Production Agreement
# ---------------------------------------------------------------------------

def activation_values(
    arrangement: dict[str, Any], computed: dict[str, Any], sponsor: dict[str, Any] | None,
    parties: dict[str, Any], file_ctx: dict[str, Any], meta: dict[str, Any],
    original: dict[str, Any] | None = None, funding: dict[str, Any] | None = None,
) -> tuple[dict[str, str], set[str]]:
    """``original`` is the executed stage-one arrangement: the Activation has no
    identity or ownership section, so it only backfills the dealer notice email
    the final left blank (the comparison view is the caller's job). ``funding``
    is the funding attestation recorded at send (``FUNDING_OVERRIDE_KEYS``); its
    non-blank entries win over the arrangement."""
    arr = {**pa.empty_arrangement(), **(arrangement or {})}
    for key in ("dealer_notice_email",):
        if not text(arr.get(key)) and text((original or {}).get(key)):
            arr[key] = (original or {})[key]
    for key in FUNDING_OVERRIDE_KEYS:
        if (funding or {}).get(key) not in (None, ""):
            arr[key] = (funding or {})[key]
    sponsor = sponsor or {}
    parties = parties or {}
    file_ctx = file_ctx or {}
    meta = meta or {}
    thr = _threshold_rows(computed)
    thresholds = computed.get("thresholds") or {}
    dealer_signer, dealer_title = _dealer_signer(arr, parties)
    qc_name, qc_title = _qc_signer(file_ctx)
    funding_party = _funding_party_name(arr, sponsor)
    adjustment = adjustment_text(arr)

    def baseline(key: str, fmt) -> str:
        return metric(fmt, (thr.get(key) or {}).get("baseline"))

    def operative(key: str, fmt) -> str:
        return metric(fmt, (thr.get(key) or {}).get("operative"))

    use_amounts = arr.get("use_of_funds") if isinstance(arr.get("use_of_funds"), dict) else {}
    use_total = 0.0
    any_use = False
    use_values: dict[str, str] = {}
    for key in USE_OF_FUNDS_KEYS:
        n = number(use_amounts.get(key))
        if n is not None:
            use_total += n
            any_use = True
        shown = money(n)
        if key == "other" and shown and text(use_amounts.get("other_label")):
            shown = f"{shown} ({text(use_amounts.get('other_label'))})"
        use_values[f"s1_use_{key}"] = shown
    use_values["s1_use_total"] = money(use_total) if any_use else ""

    values: dict[str, str] = {
        "agreement_no": text(meta.get("agreement_no")),
        "activation_date": fmt_date(arr.get("activation_date")),
        "actual_funding_date": fmt_date(arr.get("funding_date")),
        "commitment_agreement_date": fmt_date(meta.get("commitment_agreement_date")),
        **_header_parties(arr, sponsor, parties, file_ctx),
        "minimum_activation_amount": money(arr.get("min_activation")),
        "audit_discrepancy_threshold": pct(arr.get("audit_discrepancy_threshold")) or pct(DEFAULT_AUDIT_DISCREPANCY_PCT),
        "review_threshold": money(arr.get("review_threshold")),
        # Addendum A
        "baseline_from": fmt_date(arr.get("base_from")),
        "baseline_through": fmt_date(arr.get("base_through")),
        "a2_avg_units_baseline": baseline("units", count),
        "a2_avg_units_op": baseline("units", count),
        "a2_min_units_op": operative("units", count),
        "a2_avg_vsc_baseline": baseline("vsc_count", count),
        "a2_avg_vsc_op": baseline("vsc_count", count),
        "a2_min_vsc_op": operative("vsc_count", count),
        "a2_pen_baseline": baseline("vsc_pen", pct),
        "a2_pen_op": baseline("vsc_pen", pct),
        "a2_min_pen_op": operative("vsc_pen", pct),
        "a2_roll_pen_op": operative("vsc_pen3", pct),
        "a2_avg_vsc_gross_baseline": baseline("vsc_gross", money),
        "a2_avg_vsc_gross_op": baseline("vsc_gross", money),
        "a2_min_vsc_gross_op": operative("vsc_gross", money),
        "a2_avg_cp_gross_baseline": baseline("total_gross", money),
        "a2_avg_cp_gross_op": baseline("total_gross", money),
        "a2_min_cp_gross_op": operative("total_gross", money),
        "a2_debt_service_op": operative("debt_service", money),
        "a2_min_remittance_op": metric(money, thresholds.get("remittance_req")),
        "a2_production_commencement": fmt_date(arr.get("commencement")),
        "a5_exclusion_1": text(arr.get("exclusion_1")),
        "a5_exclusion_2": text(arr.get("exclusion_2")),
        "a5_exclusion_3": text(arr.get("exclusion_3")),
        "a6_cure_days": count(arr.get("cure_days")),
        "a8_pricing_adjustment": adjustment,
        # Schedule 1
        "s1_dealer_legal_name": _dealer_name(arr, parties),
        "s1_funding_party": funding_party,
        "s1_facility_type": text(arr.get("facility_type")),
        "s1_actual_funding_amount": money(arr.get("funded_amount")),
        "s1_minimum_activation_amount": money(arr.get("min_activation")),
        "s1_funding_date": fmt_date(arr.get("funding_date")),
        "s1_activation_date": fmt_date(arr.get("activation_date")),
        "s1_maturity_date": fmt_date(arr.get("maturity")),
        "s1_monthly_debt_service": money(arr.get("debt_service")),
        "s1_production_commencement": fmt_date(arr.get("commencement")),
        "s1_controlled_account": text(arr.get("controlled_account")),
        "s1_ach_account": text(arr.get("ach_account")),
        **use_values,
        "s1_support_other": text(arr.get("program_support_other")),
        **_notices("s1", arr, sponsor, file_ctx),
        # Schedule 5 -- Funding Activation Certificate
        "s5_docs_executed_date": fmt_date(arr.get("funding_docs_executed_date")),
        "s5_funding_party": funding_party,
        "s5_funding_date": fmt_date(arr.get("funding_date")),
        "s5_amount_funded": money(arr.get("funded_amount")),
        "s5_production_commencement": fmt_date(arr.get("commencement")),
        "s5_activation_date": fmt_date(arr.get("activation_date")),
        "s5_pricing_adjustment": adjustment,
        "s5_protected_source": text(arr.get("protected_source")),
        "s5_qc_name": qc_name,
        "s5_qc_title": qc_title,
        "s5_dealer_legal_name": _dealer_name(arr, parties),
        "s5_dealer_name": name_and_title(dealer_signer, dealer_title),
        "s5_sponsor_legal_name": _sponsor_name(arr, sponsor),
        "s5_sponsor_name": _sponsor_signer(sponsor),
        # the optional joinder is wet ink: the legal name prints only when a joinder is expected
        "s5_fp_legal_name": funding_party if truthy(arr.get("fp_joinder")) else "",
        "s5_fp_name": "",
        # master signature page
        "ms_qc_name": qc_name,
        "ms_qc_title": qc_title,
        "ms_dealer_legal_name": _dealer_name(arr, parties),
        "ms_dealer_name": name_and_title(dealer_signer, dealer_title),
        "ms_sponsor_legal_name": _sponsor_name(arr, sponsor),
        "ms_sponsor_name": _sponsor_signer(sponsor),
    }
    for row in thresholds.get("rolling") or []:
        slot = ROLLING_SLOTS.get(str(row.get("label")))
        if slot is None:
            continue
        fmt = {"money": money, "pct": pct}.get(str(row.get("format")), count)
        values[slot] = metric(fmt, row.get("value"))
    for slot in ROLLING_SLOTS.values():
        values.setdefault(slot, "")

    s2_values, s2_checks = _schedule_2(arr)
    s3_values, s3_checks = _schedule_3(arr)
    values.update(s2_values)
    values.update(s3_values)
    values.update(_schedule_4(arr))

    checks = _support_checks(arr) | s2_checks | s3_checks
    return values, checks
