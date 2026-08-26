"""Qualified Commercial's lender-neutral master application and readiness map.

The generated document is intentionally not a downstream lender form.  It is
the stable QC record of what the applicant disclosed, what evidence was
reviewed, which direct path remains viable, and which conditions are still
open.  No SSN, raw bureau score, or downstream lender identity enters this
module.
"""

from __future__ import annotations

import hashlib
import html
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.application_profile import ApplicationTaxonomyEntry

from ..models import (
    DealerApplicationPreScreen,
    DealerApplicationProfile,
    DealerBusiness,
    DealerDebt,
    DealerDocument,
    DealerOwner,
)
from . import recurrence
from .lender_neutral_routing import (
    RULES_VERSION,
    TERM_DISPLAY_NAME,
    TERM_PROGRAM_KEY,
    WORKING_CAPITAL_DISPLAY_NAME,
    WORKING_CAPITAL_PROGRAM_KEY,
)

MASTER_TEMPLATE_KEY = "qc_business_financing_application"
MASTER_TITLE = "Qualified Commercial Business Financing Application and Certifications"
MASTER_VERSION = "2026-08-25-1"
STATEMENT_MONTH_TARGET = 3


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _money(value: Any) -> str:
    if value in (None, ""):
        return "Awaiting evidence"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "Awaiting evidence"


def _date(value: date | datetime | None) -> str:
    if value is None:
        return "Awaiting evidence"
    return value.strftime("%B %d, %Y")


def _address(*parts: Any) -> str:
    cleaned = [_text(part) for part in parts if _text(part)]
    return ", ".join(cleaned) if cleaned else "Awaiting evidence"


def _effective_doc_kind(doc: DealerDocument) -> str:
    return _text(doc.detected_kind or doc.kind).lower()


def _official_statement_months(documents: list[DealerDocument]) -> tuple[list[str], list[str]]:
    months: set[str] = set()
    for document in documents:
        if document.status != "extracted" or _effective_doc_kind(document) != "bank_statement":
            continue
        is_pdf = _text(document.content_type).lower() == "application/pdf"
        if not (is_pdf or document.plaid_item_id is not None):
            continue
        for row in (document.extracted or {}).get("months") or []:
            month = _text(row.get("month") if isinstance(row, dict) else "")
            if len(month) == 7 and month[4] == "-" and month.replace("-", "").isdigit():
                months.add(month)
    freshness = recurrence.compute_freshness(months, date.today(), window=STATEMENT_MONTH_TARGET)
    return sorted(months), list(freshness.get("missing_months") or [])


def _route_from(routing: dict[str, Any] | None, selected: str | None) -> tuple[str | None, str]:
    programs = (routing or {}).get("programs") or []
    by_key = {str(row.get("program_key") or ""): row for row in programs}
    selected_key = _text(selected).lower()
    requested_key = None
    if selected_key in {TERM_PROGRAM_KEY, TERM_DISPLAY_NAME.lower()} or "3-5" in selected_key:
        requested_key = TERM_PROGRAM_KEY
    elif selected_key in {WORKING_CAPITAL_PROGRAM_KEY, WORKING_CAPITAL_DISPLAY_NAME.lower()} or "10-year" in selected_key:
        requested_key = WORKING_CAPITAL_PROGRAM_KEY
    if requested_key and by_key.get(requested_key, {}).get("status") in {"recommended", "potential"}:
        row = by_key[requested_key]
        return requested_key, _text(row.get("name")) or (
            TERM_DISPLAY_NAME if requested_key == TERM_PROGRAM_KEY else WORKING_CAPITAL_DISPLAY_NAME
        )
    for row in programs:
        if row.get("status") in {"recommended", "potential"}:
            return _text(row.get("program_key")) or None, _text(row.get("name")) or "Funding path under review"
    return None, "Funding path under review"


def _status(
    requirement: str,
    state: str,
    evidence: str,
    *,
    route: str = "all",
    source: str | None = None,
) -> dict[str, str | None]:
    return {
        "requirement": requirement,
        "status": state,
        "evidence": evidence,
        "route": route,
        "source": source,
    }


async def build_context(db: AsyncSession, dealer: DealerBusiness) -> dict[str, Any]:
    profile = (
        await db.execute(
            select(DealerApplicationProfile).where(DealerApplicationProfile.dealer_id == dealer.id)
        )
    ).scalar_one_or_none()
    owners = list(
        (
            await db.execute(
                select(DealerOwner)
                .where(DealerOwner.dealer_id == dealer.id)
                .order_by(DealerOwner.is_primary.desc(), DealerOwner.created_at.asc())
            )
        ).scalars().all()
    )
    documents = list(
        (
            await db.execute(
                select(DealerDocument)
                .where(DealerDocument.dealer_id == dealer.id)
                .order_by(DealerDocument.created_at.asc())
            )
        ).scalars().all()
    )
    debts = list(
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.dealer_id == dealer.id, DealerDebt.status == "active")
                .order_by(DealerDebt.created_at.asc())
            )
        ).scalars().all()
    )
    pre_screen = (
        await db.execute(
            select(DealerApplicationPreScreen).where(
                DealerApplicationPreScreen.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()
    taxonomy_rows = []
    taxonomy_ids = [
        value
        for value in (dealer.industry_entry_id, dealer.subindustry_entry_id, dealer.activity_entry_id)
        if value is not None
    ]
    if taxonomy_ids:
        taxonomy_rows = list(
            (
                await db.execute(
                    select(ApplicationTaxonomyEntry).where(
                        ApplicationTaxonomyEntry.id.in_(taxonomy_ids)
                    )
                )
            ).scalars().all()
        )
    taxonomy_status = (
        "pending"
        if any(row.status == "pending" for row in taxonomy_rows)
        else "official"
        if len(taxonomy_rows) == 3
        else "unclassified"
    )
    routing = dict(pre_screen.routing_result or {}) if pre_screen else {}
    route_key, route_label = _route_from(routing, profile.selected_program if profile else None)
    statement_months, missing_statement_months = _official_statement_months(documents)
    primary = next((owner for owner in owners if owner.is_primary), owners[0] if owners else None)

    owner_rows: list[dict[str, Any]] = []
    owner_answers = dict(pre_screen.owner_answers or {}) if pre_screen else {}
    for owner in owners:
        answer = dict(owner_answers.get(str(owner.id)) or {})
        threshold = answer.get("credit_660_or_higher")
        if owner.credit_pulled_at:
            threshold = (owner.credit_score or 0) >= 660 if owner.credit_score is not None else None
        owner_rows.append(
            {
                "id": str(owner.id),
                "name": owner.full_name,
                "ownership_pct": float(owner.ownership_pct or 0),
                "email": owner.email or "Awaiting evidence",
                "phone": owner.phone or "Awaiting evidence",
                "primary": bool(owner.is_primary),
                "credit_required": owner.credit_required,
                "credit_status": (
                    "Completed - threshold met"
                    if owner.credit_pulled_at and threshold is True
                    else "Completed - below threshold"
                    if owner.credit_pulled_at and threshold is False
                    else "Completed - tier recorded"
                    if owner.credit_pulled_at
                    else "Pending"
                    if owner.credit_required
                    else "Not required"
                ),
                "credit_reference": str(owner.credit_pull_id) if owner.credit_pull_id else None,
                "residency": answer.get("residency_status") or "Awaiting disclosure",
                "bankruptcy": answer.get("bankruptcy_timing") or "Awaiting disclosure",
                "foreclosure": answer.get("foreclosure_within_3_years"),
                "felony": answer.get("felony_timing") or "Awaiting disclosure",
                "misdemeanor": answer.get("misdemeanor_within_5_years"),
                "active_legal_charges": answer.get("active_legal_charges"),
                "ofac_match": answer.get("ofac_match"),
            }
        )

    docs_reviewed = [
        {
            "name": document.filename,
            "classification": _effective_doc_kind(document).replace("_", " ").title(),
            "status": document.status,
            "source": "Plaid" if document.plaid_item_id else "Uploaded",
            "official_statement": bool(
                _effective_doc_kind(document) == "bank_statement"
                and (document.plaid_item_id or _text(document.content_type).lower() == "application/pdf")
            ),
        }
        for document in documents
    ]
    debt_rows = [
        {
            "lender": debt.lender,
            "category": debt.category,
            "balance": float(debt.balance) if debt.balance is not None else None,
            "monthly_payment": float(debt.monthly_payment) if debt.monthly_payment is not None else None,
            "maturity": _date(debt.maturity_on) if debt.maturity_on else "Awaiting evidence",
            "ucc": bool((debt.evidence or {}).get("ucc")),
        }
        for debt in debts
    ]
    selected_program = next(
        (row for row in routing.get("programs") or [] if row.get("program_key") == route_key),
        None,
    )
    metrics = dict(routing.get("calculated_metrics") or {})

    return {
        "generated_at": datetime.now(timezone.utc),
        "template_version": MASTER_VERSION,
        "rules_version": routing.get("rules_version") or (pre_screen.rules_version if pre_screen else RULES_VERSION),
        "case_ref": dealer.case_ref or str(dealer.id),
        "business": {
            "legal_name": dealer.legal_name or dealer.name,
            "dba_name": (profile.dba_name if profile else None) or dealer.name,
            "entity_type": dealer.entity_type or "Awaiting evidence",
            "website": profile.website if profile and profile.website else "Awaiting evidence",
            "state_of_formation": profile.state_of_formation if profile and profile.state_of_formation else "Awaiting evidence",
            "started_on": _date(dealer.started_on),
            "location_type": profile.location_type if profile and profile.location_type else "Awaiting evidence",
            "physical_address": _address(dealer.address, dealer.city, " ".join(filter(None, [dealer.state, dealer.zip]))),
            "mailing_address": _address(
                profile.mailing_address if profile else None,
                profile.mailing_city if profile else None,
                " ".join(filter(None, [profile.mailing_state if profile else None, profile.mailing_zip if profile else None])),
            ),
            "email": dealer.email or "Awaiting evidence",
            "phone": dealer.phone or "Awaiting evidence",
        },
        "taxonomy": {
            "industry": dealer.industry_label or dealer.industry or "Awaiting classification",
            "subcategory": dealer.subindustry_label or dealer.subindustry or "Awaiting classification",
            "naics_code": dealer.naics_code or "Awaiting classification",
            "naics_label": dealer.naics_label or "Awaiting classification",
            "status": taxonomy_status,
            "canonical": bool(dealer.naics_code and dealer.activity_entry_id and taxonomy_status == "official"),
        },
        "request": {
            "amount": float(dealer.funding_goal or dealer.client_requested_amount or 0),
            "original_amount": float(dealer.client_requested_amount or dealer.funding_goal or 0),
            "term_months": profile.term_requested_months if profile else None,
            "purpose": (dealer.funding_purpose or "Awaiting evidence").replace("_", " ").title(),
            "use_of_funds": (profile.use_of_proceeds_text if profile else None) or dealer.use_of_proceeds_note or "Awaiting evidence",
            "line_items": dealer.use_of_proceeds or [],
            "collateral": profile.collateral_description if profile and profile.collateral_description else "Awaiting evidence",
        },
        "financial": {
            "annual_sales": float(profile.annual_sales) if profile and profile.annual_sales is not None else None,
            "annual_cash_flow_available_for_debt": float(profile.annual_cash_flow_available_for_debt) if profile and profile.annual_cash_flow_available_for_debt is not None else None,
            "monthly_debt_payments": float(profile.monthly_debt_payments) if profile and profile.monthly_debt_payments is not None else None,
            "dscr": metrics.get("dscr"),
            "dscr_source": metrics.get("dscr_source") or "unavailable",
            "statement_months": statement_months,
            "missing_statement_months": missing_statement_months,
        },
        "owners": owner_rows,
        "primary_signer": {
            "name": primary.full_name if primary else "Awaiting evidence",
            "title": profile.signer_title if profile and profile.signer_title else "Awaiting evidence",
            "email": primary.email if primary and primary.email else "Awaiting evidence",
        },
        "debts": debt_rows,
        "documents": docs_reviewed,
        "routing": routing,
        "route_key": route_key,
        "route_label": route_label,
        "route_status": selected_program.get("status") if selected_program else "review",
        "route_reasons": (selected_program or {}).get("borrower_safe_reasons") or [],
        "route_unresolved": (selected_program or {}).get("unresolved") or [],
        "profile": profile,
        "pre_screen": pre_screen,
    }


def build_readiness(context: dict[str, Any]) -> dict[str, Any]:
    owners = context["owners"]
    documents = context["documents"]
    financial = context["financial"]
    business = context["business"]
    taxonomy = context["taxonomy"]
    request = context["request"]
    profile = context.get("profile")
    pre_screen = context.get("pre_screen")
    route_key = context.get("route_key")
    refinance = bool((pre_screen.file_answers or {}).get("refinance_debt")) if pre_screen else False

    ownership_total = round(sum(float(row["ownership_pct"]) for row in owners), 2)
    required_owners = [row for row in owners if row["credit_required"]]
    credit_complete = bool(required_owners) and all(row["credit_status"].startswith("Completed") for row in required_owners)
    primary = context.get("primary_signer") or {}
    app_complete = all(
        [
            _text(business.get("legal_name")),
            _text(business.get("entity_type")) != "Awaiting evidence",
            _text(business.get("state_of_formation")) != "Awaiting evidence",
            _text(business.get("started_on")) != "Awaiting evidence",
            _text(business.get("physical_address")) != "Awaiting evidence",
            _text(business.get("mailing_address")) != "Awaiting evidence",
            _text(primary.get("name")) != "Awaiting evidence",
            _text(primary.get("title")) != "Awaiting evidence",
            financial.get("annual_sales") is not None,
            float(request.get("amount") or 0) > 0,
            _text(request.get("use_of_funds")) != "Awaiting evidence",
            ownership_total == 100.0,
            bool(pre_screen and pre_screen.completed_at),
        ]
    )
    statement_complete = len(financial.get("statement_months") or []) >= STATEMENT_MONTH_TARGET and not financial.get("missing_statement_months")
    has_tax = any(row["classification"].lower() == "tax return" and row["status"] == "extracted" for row in documents)
    has_debt_schedule = any(row["classification"].lower() == "debt schedule" and row["status"] == "extracted" for row in documents)
    has_supplemental_bank = any(
        row["classification"].lower() == "bank statement" and not row["official_statement"]
        for row in documents
    )
    human_status = _text(getattr(profile, "human_review_status", "pending")) or "pending"

    items = [
        _status("Complete applicant and ownership data", "complete" if app_complete else "missing", f"Ownership allocated: {ownership_total:.2f}%", source="Application"),
        _status("Canonical six-digit NAICS classification", "complete" if taxonomy["canonical"] else "missing", f"{taxonomy['naics_code']} - {taxonomy['naics_label']}", source="Taxonomy"),
        _status("Independent credit verification for every 20%+ owner", "complete" if credit_complete else "missing", f"{sum(row['credit_status'].startswith('Completed') for row in required_owners)} of {len(required_owners)} completed", source="iSoftPull"),
        _status(
            "Three current bank-produced statement months",
            "complete" if statement_complete else "supplemental" if has_supplemental_bank else "missing",
            ", ".join(financial.get("statement_months") or []) or "No qualifying statement months",
            source="Plaid or uploaded PDF",
        ),
        _status("Detailed written use of funds", "complete" if request["use_of_funds"] != "Awaiting evidence" else "missing", request["use_of_funds"], source="Application"),
        _status(
            "Debt schedule for refinance",
            "not_applicable" if route_key != TERM_PROGRAM_KEY or not refinance else "complete" if has_debt_schedule else "missing",
            "Debt refinance disclosed" if refinance else "No debt-refinance use disclosed",
            route=TERM_PROGRAM_KEY,
            source="Documents",
        ),
        _status("Business tax return", "complete" if has_tax else "missing" if route_key == WORKING_CAPITAL_PROGRAM_KEY else "not_applicable", "Extracted tax return in file" if has_tax else "No extracted business tax return", route=WORKING_CAPITAL_PROGRAM_KEY, source="Documents"),
        _status(
            "Business eligibility questionnaire",
            "not_applicable" if route_key != WORKING_CAPITAL_PROGRAM_KEY else "complete" if pre_screen and pre_screen.completed_at else "missing",
            "Versioned owner and business disclosures",
            route=WORKING_CAPITAL_PROGRAM_KEY,
            source="Step 1.5",
        ),
        _status("Human-reviewed fundable path", "complete" if human_status == "fundable" else "missing", getattr(profile, "human_review_note", None) or human_status.replace("_", " ").title(), source="Qualified Commercial desk"),
    ]
    package_items = [
        row
        for row in items
        if row["status"] != "not_applicable"
        and row["requirement"] != "Human-reviewed fundable path"
    ]
    package_ready = bool(
        route_key
        and package_items
        and all(row["status"] == "complete" for row in package_items)
    )
    ready = bool(package_ready and human_status == "fundable")
    return {
        "ready": ready,
        "package_ready": package_ready,
        "route_key": route_key,
        "route_label": context.get("route_label"),
        "human_review_status": human_status,
        "human_review_note": getattr(profile, "human_review_note", None),
        "human_reviewed_at": getattr(profile, "human_reviewed_at", None),
        "human_reviewed_by_user_id": getattr(profile, "human_reviewed_by_user_id", None),
        "rules_version": context.get("rules_version"),
        "items": items,
        "counts": {
            state: sum(row["status"] == state for row in items)
            for state in ("complete", "missing", "supplemental", "not_applicable")
        },
    }


def _table(headers: list[str], rows: list[list[Any]], widths: list[str] | None = None) -> str:
    colgroup = ""
    if widths:
        colgroup = "<colgroup>" + "".join(f'<col style="width:{html.escape(width)}">' for width in widths) + "</colgroup>"
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_text(value))}</td>" for value in row) + "</tr>"
        for row in rows
    ) or f'<tr><td colspan="{len(headers)}" class="muted">None reported.</td></tr>'
    return f"<table>{colgroup}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(context: dict[str, Any], readiness: dict[str, Any]) -> str:
    business = context["business"]
    taxonomy = context["taxonomy"]
    request = context["request"]
    financial = context["financial"]
    signer = context["primary_signer"]
    owners = _table(
        ["Owner", "Ownership", "Contact", "Credit verification", "Eligibility disclosures"],
        [
            [
                row["name"],
                f"{row['ownership_pct']:.2f}%",
                f"{row['email']} | {row['phone']}",
                f"{row['credit_status']}" + (f" | Ref {row['credit_reference']}" if row["credit_reference"] else ""),
                f"Residency: {row['residency']}; bankruptcy: {row['bankruptcy']}; foreclosure <3y: {row['foreclosure']}; felony: {row['felony']}; misdemeanor <5y: {row['misdemeanor']}; legal charges: {row['active_legal_charges']}; sanctions disclosure: {row['ofac_match']}",
            ]
            for row in context["owners"]
        ],
        ["17%", "10%", "22%", "23%", "28%"],
    )
    debts = _table(
        ["Creditor", "Type", "Balance", "Monthly payment", "Maturity", "UCC"],
        [[row["lender"], row["category"], _money(row["balance"]), _money(row["monthly_payment"]), row["maturity"], "Yes" if row["ucc"] else "No/unknown"] for row in context["debts"]],
    )
    evidence = _table(
        ["Document", "AI classification", "Source", "Status"],
        [[row["name"], row["classification"], row["source"], row["status"].title()] for row in context["documents"]],
        ["39%", "24%", "16%", "21%"],
    )
    readiness_table = _table(
        ["Requirement", "Status", "Evidence/source"],
        [[row["requirement"], row["status"].replace("_", " ").title(), f"{row['evidence']} | {row['source'] or ''}"] for row in readiness["items"]],
        ["38%", "17%", "45%"],
    )
    rules = context.get("routing") or {}
    matched = []
    for program in rules.get("programs") or []:
        matched.extend(program.get("matched_rules") or [])
    matched_table = _table(
        ["Rule", "Matched fact", "Applicant-safe result"],
        [[row.get("rule_id"), row.get("matched_value"), row.get("explanation")] for row in matched],
    )
    line_items = _table(
        ["Use", "Amount"],
        [[row.get("description") or row.get("label") or "Use of funds", _money(row.get("amount"))] for row in request.get("line_items") or []],
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: Letter; margin: 0.55in 0.55in 0.58in; @bottom-left {{ content: "Qualified Commercial | Preliminary lender-ready application"; color:#667085; font-size:8px; }} @bottom-right {{ content: "Page " counter(page) " of " counter(pages); color:#667085; font-size:8px; }} }}
* {{ box-sizing:border-box; }} body {{ font-family: Arial, sans-serif; color:#101828; font-size:9.2px; line-height:1.38; margin:0; }}
h1 {{ font-size:21px; margin:0 0 3px; }} h2 {{ font-size:13px; margin:16px 0 7px; padding-bottom:4px; border-bottom:2px solid #204ea1; color:#173b7a; }}
h3 {{ font-size:10px; margin:10px 0 4px; }} p {{ margin:3px 0; }} .brand {{ display:flex; justify-content:space-between; gap:20px; border-bottom:4px solid #204ea1; padding-bottom:12px; margin-bottom:12px; }}
table {{ width:100%; border-collapse:collapse; table-layout:fixed; margin:5px 0 10px; }} th {{ background:#eaf0fb; color:#173b7a; text-align:left; }} th,td {{ border:1px solid #cfd7e6; padding:5px 6px; vertical-align:top; overflow-wrap:anywhere; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:7px 14px; }} .field {{ border-bottom:1px solid #d6dce8; padding:2px 0 4px; }} .label {{ display:block; color:#667085; font-size:7.5px; text-transform:uppercase; letter-spacing:.45px; font-weight:bold; }}
.callout {{ background:#f7f9fc; border-left:4px solid #204ea1; padding:8px 10px; margin:7px 0; }} .warning {{ border-color:#c99618; background:#fff9e8; }} .muted {{ color:#667085; }}
.signature {{ page-break-inside:avoid; margin-top:18px; border:1px solid #aeb8ca; padding:14px; min-height:150px; }} .signature-line {{ margin-top:40px; width:62%; border-top:1px solid #101828; padding-top:4px; }}
.certificate-note {{ font-size:8px; color:#475467; }} .nowrap {{ white-space:nowrap; }}
</style></head><body>
<div class="brand"><div><h1>{html.escape(MASTER_TITLE)}</h1><p>Qualified Commercial LLC</p></div><div><b>Case {html.escape(context['case_ref'])}</b><br>Version {MASTER_VERSION}<br>Generated {_date(context['generated_at'])}</div></div>
<div class="callout warning"><b>Preliminary submission record, not a commitment or approval.</b> This QC-branded application consolidates applicant disclosures and reviewed evidence for commercial-financing evaluation. Final eligibility, pricing, documentation, and approval remain subject to underwriting.</div>

<h2>1. Business and Applicant Identity</h2>
<div class="grid">
<div class="field"><span class="label">Legal business name</span>{html.escape(_text(business['legal_name']))}</div>
<div class="field"><span class="label">DBA</span>{html.escape(_text(business['dba_name']))}</div>
<div class="field"><span class="label">Entity / state of formation</span>{html.escape(_text(business['entity_type']))} / {html.escape(_text(business['state_of_formation']))}</div>
<div class="field"><span class="label">Website / start date</span>{html.escape(_text(business['website']))} / {html.escape(_text(business['started_on']))}</div>
<div class="field"><span class="label">Physical address</span>{html.escape(_text(business['physical_address']))}</div>
<div class="field"><span class="label">Mailing address</span>{html.escape(_text(business['mailing_address']))}</div>
<div class="field"><span class="label">Business contact</span>{html.escape(_text(business['email']))} | {html.escape(_text(business['phone']))}</div>
<div class="field"><span class="label">Location type</span>{html.escape(_text(business['location_type']))}</div>
</div>

<h2>2. Canonical Industry Classification</h2>
<div class="grid"><div class="field"><span class="label">Category</span>{html.escape(_text(taxonomy['industry']))}</div><div class="field"><span class="label">Subcategory</span>{html.escape(_text(taxonomy['subcategory']))}</div><div class="field"><span class="label">Six-digit NAICS</span>{html.escape(_text(taxonomy['naics_code']))}</div><div class="field"><span class="label">Business activity</span>{html.escape(_text(taxonomy['naics_label']))}</div></div>

<h2>3. Ownership and Independent Credit Verification</h2>{owners}
<p class="certificate-note">No Social Security number or raw credit score is included. Credit references identify only the completed provider workflow and threshold/tier result.</p>

<h2>4. Financing Request and Use of Funds</h2>
<div class="grid"><div class="field"><span class="label">Requested amount</span>{_money(request['amount'])}</div><div class="field"><span class="label">Requested term</span>{html.escape(_text(request['term_months']) or 'Awaiting evidence')} months</div><div class="field"><span class="label">Purpose</span>{html.escape(_text(request['purpose']))}</div><div class="field"><span class="label">Recommended path</span>{html.escape(_text(context['route_label']))}</div></div>
<div class="callout"><b>Detailed use of funds</b><br>{html.escape(_text(request['use_of_funds']))}</div>{line_items}

<h2>5. Financial, Banking, and Debt Summary</h2>
<div class="grid"><div class="field"><span class="label">Annual sales</span>{_money(financial['annual_sales'])}</div><div class="field"><span class="label">Annual cash flow available for debt</span>{_money(financial['annual_cash_flow_available_for_debt'])}</div><div class="field"><span class="label">Monthly debt payments</span>{_money(financial['monthly_debt_payments'])}</div><div class="field"><span class="label">Calculated DSCR</span>{html.escape(_text(financial['dscr']) or 'Awaiting evidence')} ({html.escape(_text(financial['dscr_source']))})</div><div class="field"><span class="label">Qualifying statement months</span>{html.escape(', '.join(financial['statement_months']) or 'Awaiting evidence')}</div><div class="field"><span class="label">Open statement months</span>{html.escape(', '.join(financial['missing_statement_months']) or 'None')}</div></div>
<h3>Debt, MCA/SBA, and UCC schedule</h3>{debts}

<h2>6. Documents Reviewed and Source Readiness</h2>{evidence}{readiness_table}

<h2>7. Routing Result, Conditions, and Matched Rules</h2>
<div class="grid"><div class="field"><span class="label">Funding path</span>{html.escape(_text(context['route_label']))}</div><div class="field"><span class="label">Current route status</span>{html.escape(_text(context['route_status']).replace('_',' ').title())}</div><div class="field"><span class="label">Rules version</span>{html.escape(_text(context['rules_version']))}</div><div class="field"><span class="label">Human review</span>{html.escape(_text(readiness['human_review_status']).replace('_',' ').title())}</div></div>
{matched_table}
<h3>Remaining conditions</h3><ul>{''.join(f'<li>{html.escape(_text(item))}</li>' for item in context['route_unresolved']) or '<li>None identified by the current preliminary route.</li>'}</ul>

<h2>8. Applicant Certifications and Authorization</h2>
<p>I certify that the business, ownership schedule, financing request, financial information, debt schedule, eligibility disclosures, and supporting documents in this application are true and complete to the best of my knowledge. I am authorized to submit this application on behalf of the business. I authorize Qualified Commercial to use this application and its supporting evidence to evaluate and present commercial-financing opportunities. I understand that this document is not an approval, commitment, SBA form, or downstream lender application, and that additional forms and evidence may be required.</p>
<p>I acknowledge that each owner at 20% or more completed or must complete a separate credit authorization. This application does not contain or authorize storage of a Social Security number, and it does not display a raw credit score.</p>

<div class="signature"><b>SIGNATURE OF AUTHORIZED REPRESENTATIVE</b><div class="signature-line">Electronic signature</div><div class="grid" style="margin-top:12px"><div><span class="label">Typed legal name</span>{html.escape(_text(signer['name']))}</div><div><span class="label">Title</span>{html.escape(_text(signer['title']))}</div><div><span class="label">Date</span>Signed electronically after review</div><div><span class="label">Document hash</span>See completion certificate</div></div></div>
</body></html>"""


def render_pdf(context: dict[str, Any], readiness: dict[str, Any]) -> tuple[bytes, str]:
    from weasyprint import HTML

    pdf = HTML(string=render_html(context, readiness)).write_pdf()
    if not pdf:
        raise RuntimeError("The QC master application could not be rendered.")
    return pdf, hashlib.sha256(pdf).hexdigest()


async def build_application(
    db: AsyncSession, dealer: DealerBusiness
) -> tuple[dict[str, Any], dict[str, Any], bytes, str, list[str]]:
    context = await build_context(db, dealer)
    readiness = build_readiness(context)
    pdf, sha256 = render_pdf(context, readiness)
    missing = [row["requirement"] for row in readiness["items"] if row["status"] in {"missing", "supplemental"}]
    return context, readiness, pdf, sha256, missing
