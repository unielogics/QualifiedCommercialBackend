"""Cut a listing draft from a funding file.

The desk picks a loan or an application profile from the unified file list
(the same rows /operator-files serves), and this fills what the loan, the
profile and the current production term sheet already know: name, vertical,
amount, rate, term, maturity, collateral and location. Everything else
(lien, minimum, offered amount, narrative) the desk types, and nothing here
is shown to a member until the listing is published.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.loan import Loan
from app.models.production_package import ProductionTermSheet
from app.models.user import User
from app.routers import operator_files as unified

from .. import enums as E
from ..models import CpListing

_VERTICAL = {"real_estate": E.Vertical.REAL_ESTATE, "dealer": E.Vertical.AUTO, "main_street": E.Vertical.BUSINESS, "mca": E.Vertical.BUSINESS}

_PROGRAM_BY_LOAN_TYPE = {
    "dscr": "DSCR rental",
    "fix_and_flip": "Fix and flip",
    "ground_up": "Ground-up construction",
    "bridge": "Bridge",
    "portfolio": "Portfolio",
    "cash_out_refi": "Cash-out refinance",
}

_PROPERTY_COLLATERAL = {
    "sfr": "Residential 1-4",
    "single_family": "Residential 1-4",
    "multifamily": "Multifamily",
    "mixed_use": "Mixed-use",
    "office": "Office",
    "industrial": "Industrial",
    "retail": "Retail",
    "hospitality": "Hospitality",
    "land": "Land",
}

#: Unified statuses that mean a file is far enough along to be offered.
LISTABLE_STATUSES = {"term_sheet_provided", "approved", "closed_won"}


def _num(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(getattr(value, "value", value)).strip()
    return s or None


async def _existing_refs(db: AsyncSession) -> dict[str, str]:
    rows = (
        await db.execute(
            select(CpListing.loan_id, CpListing.profile_id, CpListing.ref).where(CpListing.status != E.ListingStatus.WITHDRAWN)
        )
    ).all()
    out: dict[str, str] = {}
    for loan_id, profile_id, ref in rows:
        if loan_id:
            out[f"loan:{loan_id}"] = ref
        if profile_id:
            out[f"profile:{profile_id}"] = ref
    return out


async def sources(db: AsyncSession, user: User, *, q: str | None, vertical: str, include_all: bool) -> list[dict[str, Any]]:
    """Files the desk may publish from, newest first."""
    rows = await unified._all_rows(user, db)
    rows = unified._apply_filters(rows, vertical=vertical, origin="all", q=q)
    refs = await _existing_refs(db)
    out = []
    for row in rows:
        status = row.pipeline_status or row.underwriting_status
        if not include_all and status not in LISTABLE_STATUSES and row.funded_amount is None and row.approved_amount is None:
            continue
        key_loan = f"loan:{row.loan_id}" if row.loan_id else None
        key_profile = f"profile:{row.profile_id}" if row.profile_id else None
        out.append(
            {
                "kind": row.source_kind,
                "id": row.id,
                "ref": row.ref,
                "name": row.title,
                "vertical": _VERTICAL.get(row.vertical, E.Vertical.BUSINESS),
                "vertical_label": row.vertical_label,
                "amount": _num(row.amount),
                "approved_amount": _num(row.approved_amount),
                "funded_amount": _num(row.funded_amount),
                "status": status,
                "loan_id": row.loan_id,
                "profile_id": row.profile_id,
                "updated_at": row.updated_at,
                "already_listed_ref": refs.get(key_loan or "") or refs.get(key_profile or ""),
            }
        )
    return out[:300]


async def _current_term_sheet(db: AsyncSession, profile_id: uuid.UUID) -> ProductionTermSheet | None:
    return (
        await db.execute(
            select(ProductionTermSheet)
            .where(ProductionTermSheet.profile_id == profile_id, ProductionTermSheet.status == "current")
            .limit(1)
        )
    ).scalar_one_or_none()


async def prefill(db: AsyncSession, user: User, *, loan_id: uuid.UUID | None, profile_id: uuid.UUID | None) -> dict[str, Any]:
    """A ListingIn-shaped dict plus the snapshot it was built from."""
    loan: Loan | None = await db.get(Loan, loan_id) if loan_id else None
    profile: ApplicationProfile | None = await db.get(ApplicationProfile, profile_id) if profile_id else None
    if loan is None and profile is not None and getattr(profile, "loan_id", None):
        loan = await db.get(Loan, profile.loan_id)
    if profile is None and loan is not None:
        profile = (
            await db.execute(select(ApplicationProfile).where(ApplicationProfile.loan_id == loan.id).limit(1))
        ).scalar_one_or_none()
    if loan is None and profile is None:
        raise LookupError("No loan or application profile matches that id.")

    sheet = await _current_term_sheet(db, profile.id) if profile is not None else None
    rows = await unified._all_rows(user, db)
    row = next(
        (r for r in rows if (loan is not None and r.loan_id == loan.id) or (profile is not None and r.profile_id == profile.id)),
        None,
    )

    snapshot: dict[str, Any] = {
        "loan_id": str(loan.id) if loan else None,
        "profile_id": str(profile.id) if profile else None,
        "term_sheet_id": str(sheet.id) if sheet else None,
        "unified_ref": row.ref if row else None,
        "unified_title": row.title if row else None,
    }
    draft: dict[str, Any] = {
        "source_kind": "loan" if loan is not None else "profile",
        "loan_id": loan.id if loan else None,
        "profile_id": profile.id if profile else None,
        "servicer_label": "Independent third-party servicer",
        "lien_position": "first",
        "ltv_kind": "ltv",
        "payment_frequency": "monthly",
    }

    if row is not None:
        draft["vertical"] = _VERTICAL.get(row.vertical, E.Vertical.BUSINESS)
        draft["name"] = row.title
        snapshot["unified"] = {
            "vertical": row.vertical,
            "amount": row.amount,
            "approved_amount": row.approved_amount,
            "funded_amount": row.funded_amount,
            "status": row.pipeline_status or row.underwriting_status,
            "program_tags": list(row.program_tags or []),
        }
        if row.program_tags:
            draft["program_label"] = row.program_tags[0]

    if loan is not None:
        loan_type = _str(getattr(loan, "type", None))
        snapshot["loan"] = {
            "deal_id": getattr(loan, "deal_id", None),
            "type": loan_type,
            "purpose": _str(getattr(loan, "purpose", None)),
            "stage": _str(getattr(loan, "stage", None)),
            "amount": _str(getattr(loan, "amount", None)),
            "base_rate": _str(getattr(loan, "base_rate", None)),
            "final_rate": _str(getattr(loan, "final_rate", None)),
            "term_months": getattr(loan, "term_months", None),
            "ltv": _str(getattr(loan, "ltv", None)),
            "ltc": _str(getattr(loan, "ltc", None)),
            "arv": _str(getattr(loan, "arv", None)),
            "address": getattr(loan, "address", None),
            "city": getattr(loan, "city", None),
            "state": getattr(loan, "state", None),
            "property_type": _str(getattr(loan, "property_type", None)),
            "entity_name": getattr(loan, "entity_name", None),
            "experience_tier": _str(getattr(loan, "experience_tier", None)),
            "exit_strategy": _str(getattr(loan, "exit_strategy", None)),
            "close_date": str(getattr(loan, "close_date", None) or ""),
        }
        draft.setdefault("vertical", E.Vertical.REAL_ESTATE)
        draft["program_label"] = _PROGRAM_BY_LOAN_TYPE.get(loan_type or "", draft.get("program_label") or "Bridge")
        draft["loan_amount"] = _num(getattr(loan, "amount", None))
        draft["offered_amount"] = _num(getattr(loan, "amount", None))
        rate = getattr(loan, "final_rate", None) or getattr(loan, "base_rate", None)
        draft["note_rate_pct"] = _num(rate)
        draft["term_months"] = getattr(loan, "term_months", None)
        ltv = getattr(loan, "ltv", None)
        ltc = getattr(loan, "ltc", None)
        if ltv is not None:
            draft["ltv_pct"], draft["ltv_kind"] = _num(ltv), "ltv"
        elif ltc is not None:
            draft["ltv_pct"], draft["ltv_kind"] = _num(ltc), "ltc"
        draft["city"] = getattr(loan, "city", None)
        state = getattr(loan, "state", None)
        draft["state"] = (state or "")[:2].upper() or None
        prop = (_str(getattr(loan, "property_type", None)) or "").lower()
        draft["collateral_type"] = _PROPERTY_COLLATERAL.get(prop, _str(getattr(loan, "property_type", None)))
        exp = _str(getattr(loan, "experience_tier", None))
        if exp:
            draft["borrower_track_record"] = f"Experience tier: {exp}"
        exit_strategy = _str(getattr(loan, "exit_strategy", None))
        if exit_strategy:
            draft["exit_plan"] = exit_strategy
        if draft.get("name") in (None, "") and getattr(loan, "address", None):
            draft["name"] = f"{draft['program_label']} loan, {loan.address}"
        if draft.get("loan_amount") and draft.get("ltv_pct"):
            try:
                draft["valuation_amount"] = (draft["loan_amount"] / draft["ltv_pct"] * Decimal(100)).quantize(Decimal("1"))
                draft["valuation_source"] = "Third-party appraisal"
            except Exception:
                pass

    if profile is not None:
        snapshot["profile"] = {
            "vertical": _str(getattr(profile, "vertical", None)),
            "industry": getattr(profile, "industry", None),
            "naics_label": getattr(profile, "naics_label", None),
            "funding_category": getattr(profile, "funding_category", None),
            "underwriting_status": _str(getattr(profile, "underwriting_status", None)),
            "underwriting_approved_amount": _str(getattr(profile, "underwriting_approved_amount", None)),
            "underwriting_approved_dscr": _str(getattr(profile, "underwriting_approved_dscr", None)),
        }
        draft.setdefault("vertical", _VERTICAL.get(_str(getattr(profile, "vertical", None)) or "", E.Vertical.BUSINESS))
        approved = _num(getattr(profile, "underwriting_approved_amount", None))
        if approved and not draft.get("offered_amount"):
            draft["loan_amount"] = approved
            draft["offered_amount"] = approved
        industry = getattr(profile, "naics_label", None) or getattr(profile, "industry", None)
        if industry and not draft.get("collateral_type"):
            draft["collateral_type"] = "Business assets"
            draft["summary"] = f"Operating business: {industry}."

    if sheet is not None:
        snapshot["term_sheet"] = {
            "version": sheet.version,
            "facility_type": sheet.facility_type,
            "funding_party_kind": sheet.funding_party_kind,
            "approved_amount": _str(sheet.approved_amount),
            "rate_pct": _str(sheet.rate_pct),
            "term_months": sheet.term_months,
            "expected_funding_date": str(sheet.expected_funding_date or ""),
            "activation_date": str(sheet.activation_date or ""),
            "maturity_date": str(sheet.maturity_date or ""),
        }
        draft["loan_amount"] = _num(sheet.approved_amount) or draft.get("loan_amount")
        draft["offered_amount"] = _num(sheet.approved_amount) or draft.get("offered_amount")
        draft["note_rate_pct"] = _num(sheet.rate_pct) or draft.get("note_rate_pct")
        draft["term_months"] = sheet.term_months or draft.get("term_months")
        draft["maturity_date"] = sheet.maturity_date or draft.get("maturity_date")
        if sheet.facility_type and not draft.get("program_label"):
            draft["program_label"] = sheet.facility_type.replace("_", " ").capitalize()

    if draft.get("offered_amount"):
        offered = Decimal(str(draft["offered_amount"]))
        # A sensible default the desk can change: 1% of the offering, floored
        # at $10K and rounded to the nearest $5K.
        default_min = max(offered / Decimal(100), Decimal(10000))
        draft["minimum_investment"] = (default_min / Decimal(5000)).quantize(Decimal("1")) * Decimal(5000)
    draft["source_snapshot"] = snapshot
    return draft
