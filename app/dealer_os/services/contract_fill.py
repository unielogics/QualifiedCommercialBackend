"""Prepopulating the two flat agreements from what the case has collected.

Neither PDF declares a form field, so filling is a coordinate overlay: find
each blank, place its value. Two anchor strategies, chosen by what each
document gives us:

- The consulting agreement's blanks are underscore runs. Extracted as words
  and ordered top-to-bottom, they are a stable index: blank 0 is the effective
  date, blank 2 the client name, and so on. Ordering is the contract here —
  a re-exported PDF that adds or removes a blank shifts every index after it,
  which is why fills are pinned to template revision 1 and refuse to run on a
  revision they have not been re-verified against.
- The loan application is labeled boxes; the value goes just under its label.
  Labels are searched by text, so small layout shifts survive.

What gets filled is deliberately conservative:
- **3% commission** is the desk's stated rate. It is written into the
  percentage blank and its option is checked. The tail-period months, minimum
  fee and tiered rows are left empty and reported as unfilled rather than
  invented — those are legal terms nobody stated.
- **SSN is never prefilled.** The application's SSN box stays empty even when
  a pull captured identity elsewhere; it is the applicant's to write.
- The rep working the case goes on the CONSULTANT name line — the rep is who
  signs at the bottom. The client's name is placed on the CLIENT line; both
  signature and date stay empty for the signing act itself.

Every generated fill records the exact values placed and the SHA-256 of the
output, so the document a client later signs is provably the document that was
generated — the same association-of-record discipline as the sign flow.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

from ..models import (
    ContractDocument,
    ContractTemplate,
    DealerApplicationProfile,
    DealerBusiness,
    DealerOwner,
)
from . import qc_master_application, storage

logger = logging.getLogger(__name__)

__all__ = ["FillResult", "build_values", "fill_pdf", "generate", "COMMISSION_PCT"]

# The desk's stated rate for the consulting agreement. One place, named, so
# changing the deal changes one line.
COMMISSION_PCT = "3"

# Qualified Commercial's principal place of business, as printed in the Terms.
QC_ADDRESS = "14 53rd St #408N, Brooklyn, NY 11232"
QC_LAW_STATE = "New Jersey"

# Overlay maps are verified against a specific upload of each PDF. A replaced
# template re-orders blanks silently, so a fill against an unverified revision
# is refused rather than risked.
VERIFIED_REVISIONS: dict[str, int] = {
    "consulting_agreement": 1,
    "loan_app": 1,
    "qc_program_application": 1,
}

_FONT = "helv"
_SIZE = 8.0


@dataclass
class FillResult:
    pdf: bytes
    placed: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    sha256: str = ""


def _fmt_date(now: datetime) -> str:
    return now.strftime("%B %-d")


def _fmt_money(n: float | None) -> str:
    if n is None:
        return ""
    return f"${n:,.0f}"


_INDUSTRY_LABELS = {
    "restaurant_food_service": "Restaurant / food service",
    "auto_service": "Auto sales & service",
    "grocery_commodities": "Grocery / commodities",
    "trucking_logistics": "Trucking / logistics",
    "manufacturing": "Manufacturing",
    "retail_ecommerce": "Retail / e-commerce",
    "construction_trades": "Construction / trades",
    "professional_practice": "Professional practice",
    "other": "Other",
}

# funding_purpose -> the program checkbox on the consulting agreement.
_PROGRAM_FOR_PURPOSE = {
    "working_capital": "Working Capital / Line of Credit",
    "equipment": "Equipment Financing",
    "real_estate": "Commercial Real Estate",
    "refinance": "MCA Refinance",
    "floorplan": "Auto Dealer / Floorplan",
}


async def build_values(
    db: AsyncSession, dealer: DealerBusiness
) -> tuple[dict[str, str], list[str]]:
    """Everything the case knows, shaped for the two documents. Missing values
    are named, not defaulted: a blank on a legal document must be a decision."""
    owner = (
        await db.execute(
            select(DealerOwner)
            .where(DealerOwner.dealer_id == dealer.id)
            .order_by(DealerOwner.is_primary.desc(), DealerOwner.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    rep = None
    if dealer.owner_user_id:
        rep = await db.get(User, dealer.owner_user_id)
    profile = (
        await db.execute(
            select(DealerApplicationProfile).where(
                DealerApplicationProfile.dealer_id == dealer.id
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    full_addr = ", ".join(
        x for x in (dealer.address, dealer.city, f"{dealer.state or ''} {dealer.zip or ''}".strip()) if x
    )

    v: dict[str, str] = {
        "effective_date_md": _fmt_date(now),
        "effective_date_yy": now.strftime("%y"),
        "today": now.strftime("%B %-d, %Y"),
        "qc_address": QC_ADDRESS,
        "law_state": QC_LAW_STATE,
        "client_legal_name": dealer.legal_name or dealer.name or "",
        "client_entity_type": (dealer.entity_type or "").lower(),
        "client_address": full_addr,
        "commission_pct": COMMISSION_PCT,
        "rep_name": (rep.name if rep else "") or "",
        "owner_first": (owner.first_name if owner else "") or "",
        "owner_last": (owner.last_name if owner else "") or "",
        "owner_full": "",  # composed from first + last below
        "owner_email": (owner.email if owner else "") or "",
        "owner_phone": (owner.phone if owner else "") or "",
        # The downstream form has an SSN box, but QC never receives or retains
        # raw SSNs. Identity is collected directly by the credit provider.
        "owner_ssn_notice": "Collected securely through credit authorization",
        "owner_pct": (
            f"{owner.ownership_pct:g}" if owner and owner.ownership_pct is not None else ""
        ),
        "owner_street": (owner.street if owner else "") or "",
        "owner_address_2": "N/A",
        "owner_city": (owner.city if owner else "") or "",
        "owner_state": (owner.state if owner else "") or "",
        "owner_zip": (owner.zip if owner else "") or "",
        "guaranty": (
            (profile.guaranty_type or "").replace("_", " ").title()
            if profile and profile.guaranty_type
            else "Personal" if owner and owner.is_guarantor else ""
        ),
        "biz_legal": dealer.legal_name or dealer.name or "",
        "biz_dba": dealer.name if dealer.legal_name and dealer.legal_name != dealer.name else "",
        "biz_industry": _INDUSTRY_LABELS.get(dealer.industry or "", dealer.industry or ""),
        "biz_entity": dealer.entity_type or "",
        "biz_office_space": (profile.office_space if profile else None) or "N/A",
        "biz_location_type": (profile.location_type if profile else None) or "N/A",
        "biz_formation_state": (profile.state_of_formation if profile else None) or dealer.state or "",
        "biz_start": dealer.started_on.strftime("%m/%Y") if dealer.started_on else "",
        "biz_website": (profile.website if profile else None) or "N/A",
        "business_stage": (profile.business_stage if profile else None) or "existing",
        "biz_address": dealer.address or "",
        "biz_city": dealer.city or "",
        "biz_state": dealer.state or "",
        "biz_zip": dealer.zip or "",
        "mail_address": (profile.mailing_address if profile else None) or dealer.address or "",
        "mail_city": (profile.mailing_city if profile else None) or dealer.city or "",
        "mail_state": (profile.mailing_state if profile else None) or dealer.state or "",
        "mail_zip": (profile.mailing_zip if profile else None) or dealer.zip or "",
        "mail_same_as_physical": "yes" if not profile or not any((
            profile.mailing_address,
            profile.mailing_city,
            profile.mailing_state,
            profile.mailing_zip,
        )) else "no",
        "annual_sales": _fmt_money(float(profile.annual_sales)) if profile and profile.annual_sales is not None else "",
        "amount_requested": _fmt_money(
            float(dealer.funding_goal) if dealer.funding_goal is not None else None
        ),
        "use_of_funds": dealer.use_of_proceeds_note or "",
        # A blank debt disclosure must never be converted into a factual $0.
        # The rep explicitly enters zero when the business has no such balance.
        "mca_balance": _fmt_money(float(profile.existing_mca_balance)) if profile and profile.existing_mca_balance is not None else "",
        "sba_balance": _fmt_money(float(profile.existing_sba_balance)) if profile and profile.existing_sba_balance is not None else "",
        "business_dscr": (
            f"{(float(profile.annual_cash_flow_available_for_debt) / (float(profile.monthly_debt_payments) * 12)):.2f}x"
            if profile
            and profile.annual_cash_flow_available_for_debt is not None
            and profile.monthly_debt_payments
            and float(profile.monthly_debt_payments) > 0
            else "N/A"
        ),
        "owner_count": "",  # populated below from the complete ownership schedule
        "ucc_filings": str(profile.active_ucc_filings) if profile and profile.active_ucc_filings is not None else "N/A",
        "affiliates": "Yes" if profile and profile.affiliate_businesses is True else "No" if profile and profile.affiliate_businesses is False else "N/A",
        "welcome_email": "Yes" if not profile or profile.send_welcome_email is not False else "No",
        "signer_title": (profile.signer_title if profile else None) or "",
        "selected_program": (profile.selected_program if profile else None) or "",
        "program_checkbox": _PROGRAM_FOR_PURPOSE.get(dealer.funding_purpose or "", ""),
    }
    if not v["owner_full"]:
        v["owner_full"] = " ".join(x for x in (v["owner_first"], v["owner_last"]) if x)
    owner_count = int(
        (await db.execute(select(func.count()).select_from(DealerOwner).where(
            DealerOwner.dealer_id == dealer.id
        ))).scalar_one()
    )
    v["owner_count"] = str(owner_count)

    missing = sorted(
        {
            "principal name": not v["owner_full"],
            "principal email": not v["owner_email"],
            "principal phone": not v["owner_phone"],
            "principal home address": not v["owner_street"],
            "business legal name": not v["biz_legal"],
            "entity type": not v["client_entity_type"],
            "business address": not v["biz_address"],
            "business start date": not v["biz_start"],
            "amount requested": not v["amount_requested"],
            "use of funds (write it on step 1)": not v["use_of_funds"],
            "rep on the case": not v["rep_name"],
        }.items()
    )
    return v, [k for k, absent in missing if absent]


def _blanks(page) -> list:
    """Underscore-run words in reading order — the consulting agreement's index.

    A blank is three or more underscores whose only other characters are
    punctuation: word extraction keeps a trailing comma or period attached
    ('____________,'), and a strict all-underscores test silently drops those
    blanks and shifts every index after them. That exact bug misplaced the
    governing-law state on the first verification pass."""
    words = page.get_text("words")
    punct = set('.,;:()"\'')

    def is_blank(t: str) -> bool:
        return t.count("_") >= 3 and (set(t) - {"_"}) <= punct

    runs = [w for w in words if is_blank(w[4])]
    runs.sort(key=lambda w: (round(w[1]), w[0]))
    return runs


def _put(page, x: float, y: float, text: str, size: float = _SIZE) -> None:
    if not text:
        return
    page.insert_text((x, y), text, fontname=_FONT, fontsize=size, color=(0.106, 0.294, 0.62))


def _under_label(
    page,
    label: str,
    value: str,
    *,
    dy: float = 13.0,
    occurrence: int = 0,
    size: float = _SIZE,
) -> bool:
    """Place a value just beneath a searched label. Returns False when the
    label is not found, so a re-export that renames a box surfaces as an
    unplaced value instead of text floating in space."""
    rects = page.search_for(label)
    if len(rects) <= occurrence:
        return False
    r = rects[occurrence]
    _put(page, r.x0, r.y1 + dy, value, size=size)
    return True


def _fill_consulting(page, v: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    placed: dict[str, str] = {}
    problems: list[str] = []
    blanks = _blanks(page)

    # Index map, verified against revision 1's geometry (18 blanks):
    # 0 date · 1 QC address · 2 client name · 3 entity type · 4 client address
    # 5 Other-program · 6 tail months · 7 commission % · 8 flat fee
    # 9-11 tiered · 12 minimum fee · 13 payable-from · 14 termination notice
    # 15 law state · 16-17 county courts
    def on_blank(i: int, key: str, pad: float = 1.0) -> None:
        if i >= len(blanks):
            problems.append(f"blank {i} ({key}) not found")
            return
        w = blanks[i]
        if v.get(key):
            _put(page, w[0] + pad, w[3] - 1.5, v[key])
            placed[key] = v[key]

    on_blank(0, "effective_date_md")
    on_blank(1, "qc_address")
    on_blank(2, "client_legal_name")
    on_blank(3, "client_entity_type")
    on_blank(4, "client_address")
    on_blank(7, "commission_pct")
    on_blank(15, "law_state")

    # The year: the printed text is '____, 20 ("Effective Date")' — the century
    # is on the page, only the two digits go after it.
    yr = page.search_for(', 20 ("Effective')
    if yr:
        _put(page, yr[0].x0 + 16.5, yr[0].y1 - 1.5, v["effective_date_yy"])
        placed["effective_date_yy"] = v["effective_date_yy"]

    # Checkboxes: X sits just left of the option's label text.
    def check(label: str, name: str) -> None:
        rects = page.search_for(label)
        if rects:
            _put(page, rects[0].x0 - 11, rects[0].y1 - 1.0, "X", size=9.0)
            placed[name] = "X"

    if v.get("program_checkbox"):
        check(v["program_checkbox"], f"program: {v['program_checkbox']}")
    check("% of the total funded amount", "compensation: percentage of funded amount")
    check("binding arbitration", "disputes: binding arbitration")

    # Signature blocks: the value line sits above its small-caps label.
    if v.get("rep_name"):
        _put(page, 36.0, 708.5, f"{v['rep_name']} · Qualified Commercial", size=9.0)
        placed["consultant name (the rep on the case)"] = v["rep_name"]
    if v.get("owner_full"):
        _put(page, 320.4, 708.5, v["owner_full"], size=9.0)
        placed["client name"] = v["owner_full"]

    return placed, problems


# Loan application: label -> (value key, occurrence). The value goes under the
# label inside its box.
_LOANAPP_LABELS: list[tuple[str, str, int]] = [
    ("FIRST NAME", "owner_first", 0),
    ("LAST NAME", "owner_last", 0),
    ("EMAIL", "owner_email", 0),
    ("PHONE", "owner_phone", 0),
    ("SSN", "owner_ssn_notice", 0),
    ("OWNERSHIP %", "owner_pct", 0),
    ("GUARANTY TYPE", "guaranty", 0),
    ("STREET ADDRESS", "owner_street", 0),  # first occurrence = borrower's
    ("ADDRESS 2", "owner_address_2", 0),
    # The source PDF contains lowercase uses of "state" in its instructions
    # plus STATE OF INCORPORATION. These source-specific occurrences target
    # the actual borrower and business-address boxes.
    ("STATE", "owner_state", 2),
    ("ZIP", "owner_zip", 0),
    ("LEGAL / CORPORATE NAME", "biz_legal", 0),
    ("COMPANY NAME (DBA)", "biz_dba", 0),
    ("INDUSTRY", "biz_industry", 0),
    ("BUSINESS TYPE", "biz_entity", 0),
    ("OFFICE SPACE", "biz_office_space", 0),
    ("BUSINESS LOCATION TYPE", "biz_location_type", 0),
    ("STATE OF INCORPORATION", "biz_formation_state", 0),
    ("BUSINESS START DATE", "biz_start", 0),
    ("WEBSITE", "biz_website", 0),
    ("PHYSICAL STREET ADDRESS", "biz_address", 0),
    ("GROSS ANNUAL SALES", "annual_sales", 0),
    ("FUNDING AMOUNT REQUESTED", "amount_requested", 0),
    ("EXISTING MCA BALANCE", "mca_balance", 0),
    ("EXISTING SBA BALANCE", "sba_balance", 0),
]


def _fill_loanapp(page, v: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    placed: dict[str, str] = {}
    problems: list[str] = []

    for label, key, occ in _LOANAPP_LABELS:
        if not key or not v.get(key):
            continue
        if _under_label(
            page,
            label,
            v[key],
            occurrence=occ,
            dy=11.0 if key == "owner_ssn_notice" else 13.0,
            size=4.5 if key == "owner_ssn_notice" else _SIZE,
        ):
            placed[label.title()] = v[key]
        else:
            problems.append(f"label not found: {label}")

    # The two CITY boxes: borrower's (row with ADDRESS 2) then business
    # physical. Distinguish by occurrence.
    if v.get("owner_city") and _under_label(page, "CITY", v["owner_city"], occurrence=0):
        placed["Borrower City"] = v["owner_city"]
    if v.get("biz_city") and _under_label(page, "CITY", v["biz_city"], occurrence=1):
        placed["Business City"] = v["biz_city"]
    # Business state/zip on the physical row (STATE occ 1, ZIP occ 1).
    if v.get("biz_state") and _under_label(page, "STATE", v["biz_state"], occurrence=4):
        placed["Business State"] = v["biz_state"]
    if v.get("biz_zip") and _under_label(page, "ZIP", v["biz_zip"], occurrence=1):
        placed["Business Zip"] = v["biz_zip"]

    # Mailing row. A separate mailing address is preserved when provided;
    # otherwise the same-as-physical box is selected below.
    for label, key, occurrence in (
        ("MAILING STREET ADDRESS", "mail_address", 0),
        ("CITY", "mail_city", 2),
        ("STATE", "mail_state", 5),
        ("ZIP", "mail_zip", 2),
    ):
        if v.get(key) and _under_label(page, label, v[key], occurrence=occurrence):
            placed[f"Mailing {label.title()}"] = v[key]

    # Type of business: an operating file is an existing business.
    if v.get("business_stage"):
        stage_label = {
            "startup": "Startup",
            "existing": "Existing business",
            "acquisition": "Acquisition",
        }.get(v["business_stage"].lower(), "Existing business")
        rects = page.search_for(stage_label)
        if rects:
            _put(page, rects[0].x0 - 11, rects[0].y1 - 1.0, "X", size=9.0)
            placed[f"Type: {stage_label}"] = "X"
    # One address collected means mailing == physical.
    rects = page.search_for("Mailing address same as physical")
    if rects and v.get("biz_address") and v.get("mail_same_as_physical") == "yes":
        _put(page, rects[0].x0 - 11, rects[0].y1 - 1.0, "X", size=9.0)
        placed["Mailing same as physical"] = "X"

    # Program selection is deterministic from the package, never guessed
    # from free-text use of funds.
    selected = (v.get("selected_program") or "").lower()
    program_label = (
        "EZ Term" if selected == "term_loan_3_5_year"
        else "MicroCap" if selected == "term_loan_10_year"
        else ""
    )
    if program_label:
        rects = page.search_for(program_label)
        if rects:
            # The program names also appear in the dark header and document
            # checklist. Select the occurrence on the PROGRAM APPLIED FOR row.
            candidate = min(rects, key=lambda rect: abs(rect.y0 - 97.0))
            _put(page, candidate.x0 - 11, candidate.y1 - 1.0, "X", size=9.0)
            placed[f"Program: {program_label}"] = "X"

    welcome_label = "Yes" if v.get("welcome_email") == "Yes" else "No"
    welcome_heading = page.search_for("WELCOME EMAIL")
    candidates = page.search_for(welcome_label)
    if welcome_heading and candidates:
        heading = welcome_heading[0]
        candidate = min(candidates, key=lambda r: abs(r.y0 - heading.y1))
        if abs(candidate.y0 - heading.y1) < 40:
            _put(page, candidate.x0 - 10, candidate.y1 - 1, "X", size=9.0)
            placed[f"Welcome email: {welcome_label}"] = "X"

    # Use of funds: the big box under its label, wrapped.
    if v.get("use_of_funds"):
        rects = page.search_for("USE OF FUNDS DESCRIPTION")
        if rects:
            import fitz

            r = rects[0]
            box = fitz.Rect(r.x0, r.y1 + 6, 576, r.y1 + 150)
            page.insert_textbox(
                box, v["use_of_funds"], fontname=_FONT, fontsize=8.5,
                color=(0.106, 0.294, 0.62),
            )
            placed["Use Of Funds Description"] = v["use_of_funds"][:120]

    # MicroCap-only fields are populated only for MicroCap. The EZ package
    # explicitly says N/A so conditional boxes are never ambiguous.
    is_microcap = selected == "term_loan_10_year"
    for label, key in (
        ("BUSINESS DSCR", "business_dscr"),
        ("NUMBER OF OWNERS", "owner_count"),
        ("ACTIVE UCC", "ucc_filings"),
    ):
        value = v.get(key) if is_microcap else "N/A"
        if value and _under_label(page, label, value):
            placed[label.title()] = value

    # Affiliate businesses is a Yes/No checkbox pair, not a text field.
    affiliate_heading = page.search_for("AFFILIATE")
    if affiliate_heading and not is_microcap:
        heading = affiliate_heading[-1]
        _put(page, heading.x0, heading.y1 + 10, "N/A", size=7.5)
        placed["Affiliate businesses"] = "N/A"
    else:
        affiliate_label = "Yes" if v.get("affiliates") == "Yes" else "No"
        affiliate_candidates = page.search_for(affiliate_label)
    if is_microcap and affiliate_heading and affiliate_candidates:
        heading = affiliate_heading[-1]
        candidate = min(
            affiliate_candidates,
            key=lambda rect: abs(rect.y0 - heading.y0),
        )
        if abs(candidate.y0 - heading.y0) < 30:
            _put(page, candidate.x0 - 10, candidate.y1 - 1, "X", size=9.0)
            placed[f"Affiliate businesses: {affiliate_label}"] = "X"

    return placed, problems


def fill_pdf(
    key: str,
    template_bytes: bytes,
    v: dict[str, str],
    *,
    overlay_map: dict | None = None,
) -> FillResult:
    """Apply one template's overlay. Pure aside from PyMuPDF."""
    import fitz

    doc = fitz.open(stream=template_bytes, filetype="pdf")
    page = doc[0]
    if key == "consulting_agreement":
        placed, problems = _fill_consulting(page, v)
    elif key in {"loan_app", "qc_program_application"}:
        placed, problems = _fill_loanapp(page, v)
    elif (overlay_map or {}).get("static_supporting_document"):
        # Supporting agreements may be included verbatim. Their immutable
        # template version carries the signature/date anchors used at
        # execution time, while the source PDF itself remains untouched.
        placed, problems = {}, []
    else:
        raise ValueError(f"no overlay map for template {key!r}")
    out = doc.tobytes(deflate=True)
    return FillResult(pdf=out, placed=placed, missing=problems, sha256=hashlib.sha256(out).hexdigest())


async def generate(
    db: AsyncSession, dealer: DealerBusiness, key: str
) -> tuple[ContractDocument, FillResult, list[str]]:
    """Produce (or refresh) the case's prepopulated copy of one agreement.

    Regenerating before signature is cheap and safe; the document row keeps the
    latest fill. Once a document is out for signature or executed it is frozen:
    the paper a signer saw must never be quietly replaced underneath them.
    """
    tpl = (
        await db.execute(select(ContractTemplate).where(ContractTemplate.key == key))
    ).scalar_one_or_none()
    if tpl is None or not tpl.active:
        raise ValueError("That agreement is not available.")
    if tpl.render_kind == "uploaded_pdf" and not tpl.s3_key:
        raise ValueError("That agreement has no uploaded paper yet.")
    verified = VERIFIED_REVISIONS.get(key)
    if tpl.render_kind == "uploaded_pdf" and (verified is None or tpl.revision != verified):
        raise ValueError(
            f"The overlay map for {key!r} is verified against revision {verified}, "
            f"but the uploaded paper is revision {tpl.revision}. Re-verify the map first."
        )

    existing = (
        await db.execute(
            select(ContractDocument).where(
                ContractDocument.dealer_id == dealer.id,
                ContractDocument.template_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status in ("out_for_signature", "executed"):
        raise ValueError(
            "This document is already out for signature; the paper a signer sees "
            "cannot be replaced underneath them. Void it first if the terms changed."
        )

    if tpl.render_kind == "generated_html":
        if key != qc_master_application.MASTER_TEMPLATE_KEY:
            raise ValueError(f"No generated renderer is registered for {key!r}.")
        context, readiness, pdf, sha256, missing_data = (
            await qc_master_application.build_application(db, dealer)
        )
        result = FillResult(
            pdf=pdf,
            sha256=sha256,
            placed={
                "case_ref": context["case_ref"],
                "business_name": context["business"]["legal_name"],
                "authorized_signer": context["primary_signer"]["name"],
                "signer_title": context["primary_signer"]["title"],
                "route": context["route_label"],
                "rules_version": context["rules_version"],
                "package_ready_for_signature": "yes" if readiness["package_ready"] else "no",
            },
        )
        ready = readiness["package_ready"]
    else:
        raw = storage.get_bytes(tpl.s3_key)
        if raw is None:
            raise RuntimeError("The template PDF could not be read from storage.")
        values, missing_data = await build_values(db, dealer)
        result = fill_pdf(key, raw, values)
        ready = not missing_data

    s3_key = f"contract-fills/{dealer.id}/{key}/r{tpl.revision}-{result.sha256[:16]}.pdf"
    if not storage.put_bytes(s3_key, result.pdf, "application/pdf"):
        raise RuntimeError("The filled PDF could not be stored.")

    if existing is None:
        existing = ContractDocument(dealer_id=dealer.id, template_key=key)
        db.add(existing)
    existing.template_revision = tpl.revision
    existing.field_values = result.placed
    existing.filled_s3_key = s3_key
    existing.filled_sha256 = result.sha256
    existing.status = "ready" if ready else "draft"
    await db.flush()
    return existing, result, missing_data
