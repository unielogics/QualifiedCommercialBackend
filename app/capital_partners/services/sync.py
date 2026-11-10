"""What the marketplace carries against what the file says now.

A listing is cut from a file and then the file keeps moving: the desk
changes the plan, records terms, refreshes the package. The listing holds a
stamp of what was last pushed (the fingerprint of each section) so every
screen can say "published", "up to date" or "these changed since": the
figures, the plan, the proposed terms, the programs and the package PDFs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile

from ..models import CpListing

SECTION_LABELS: dict[str, str] = {
    "figures": "Financial figures",
    "plan": "The desk's plan",
    "terms": "Proposed terms",
    "collateral": "Collateral disclosure",
    "programs": "Programs",
    "package": "Package PDFs",
}


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def proposed_terms(cockpit: dict[str, Any]) -> dict[str, Any] | None:
    """The terms the package and the listing speak of: the recorded term
    sheet first, else the loan in the desk's plan, with the schedule and the
    principal at risk (financial_cockpit.proposed_terms_block)."""
    from app.services import financial_cockpit

    if "proposed_terms" in cockpit:
        return cockpit.get("proposed_terms")
    return financial_cockpit.proposed_terms_block(cockpit)


def file_sections(
    cockpit: dict[str, Any], *, program_keys: list[str], package: dict[str, Any] | None
) -> dict[str, Any]:
    """The comparable sections of a file, free of timestamps."""
    from app.services import financial_cockpit
    from app.services.financial_metric_reporting import operating_earnings_brief

    figures = {
        k: v
        for k, v in financial_cockpit.listing_summary(cockpit).items()
        if k not in {"generated_at", "plan"}
    }
    figures["operating_earnings"] = operating_earnings_brief(cockpit)
    plan = financial_cockpit.plan_summary(cockpit)
    sections = {
        "figures": figures,
        "plan": {
            "active": plan["active"],
            "modifications": plan["modifications"],
            "impact": plan["impact"],
            "note": plan["note"],
        },
        "terms": {
            "requested_amount": cockpit.get("requested_amount"),
            "proposed": proposed_terms(cockpit),
        },
        "programs": sorted(program_keys),
        "package": package,
    }
    if isinstance(cockpit.get("collateral"), dict):
        sections["collateral"] = deepcopy(cockpit["collateral"])
    return sections


def canonical_financing_fields(financials: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """One primary loan from the saved file; alternative products stay explicit.

    This runs only on a staff save/refresh. It never publishes a listing and
    does not turn a new collateral proposal into recorded security.
    """
    terms = financials.get("proposed_terms")
    if not isinstance(terms, dict) or terms.get("active_proposal") is False or terms.get("amount") is None:
        return {}
    products = [deepcopy(row) for row in existing.get("products") or [] if isinstance(row, dict)]
    old_primary = next((row for row in products if row.get("primary")), {})
    key = terms.get("program_key") or old_primary.get("program_key")
    label = terms.get("program_name") or old_primary.get("label") or "Proposed financing"
    ppy = terms.get("payments_per_year")
    frequency = next((name for name, value in (("monthly", 12), ("weekly", 52), ("biweekly", 26), ("daily", 252)) if ppy is not None and abs(float(ppy) - value) <= .1), "custom" if ppy else "monthly")
    dscr = financials.get("dscr_after_plan")
    if dscr is None:
        dscr = financials.get("pro_forma_dscr")
    primary = {"program_key": key, "label": label, "primary": True, "financing_source": "canonical_financing",
        **{name: deepcopy(terms.get(name)) for name in ("amount", "rate_pct", "term_months", "structure", "interest_only",
            "interest_only_months", "repayment_config", "payments_per_year", "monthly_payment", "coverage_payment_monthly",
            "interest_only_payment_monthly", "repayment_payment_monthly", "maturity_balance", "payment_phases")},
        "payment_frequency": frequency, "dscr": dscr, "factor_rate": None,
        "notes": str(terms.get("describe") or terms.get("payment_note") or "Terms mirror the current saved financial file.")[:600]}
    primary["interest_only"] = bool(primary["interest_only"])
    primary["payment_phases"] = primary["payment_phases"] or []
    alternatives = [dict(row, primary=False) for row in products
        if row.get("financing_source") != "canonical_financing" and not (row.get("primary") and row.get("program_key") == key)]
    result = {"loan_amount": terms["amount"], "note_rate_pct": terms.get("rate_pct"),
        "term_months": terms.get("term_months"), "program_label": label, "products": [primary, *alternatives]}
    # Offered capital may be an intentional participation, separate from total
    # financing. A full-loan offering follows changes in that total.
    if existing.get("offered_amount") is None or existing.get("offered_amount") == existing.get("loan_amount"):
        result["offered_amount"] = terms["amount"]
    return result


def apply_canonical_financing(listing: CpListing, fresh: dict[str, Any]) -> None:
    existing = {key: getattr(listing, key, None) for key in ("products", "loan_amount", "offered_amount")}
    for key, value in canonical_financing_fields(fresh.get("financials") or {}, existing).items():
        setattr(listing, key, value)


def normalized_financing(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: normalized_financing(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized_financing(item) for item in value]
    return value


def economic_products(products: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    keys = ("program_key", "amount", "rate_pct", "factor_rate", "term_months", "structure", "repayment_config",
        "payments_per_year", "monthly_payment", "coverage_payment_monthly", "maturity_balance")
    numeric = {"amount", "rate_pct", "factor_rate", "term_months", "payments_per_year", "monthly_payment", "coverage_payment_monthly", "maturity_balance"}
    return [{**{key: economic_number(row.get(key)) if key in numeric else normalized_financing(row.get(key)) for key in keys},
        "primary": bool(row.get("primary")), "interest_only": bool(row.get("interest_only")),
        "interest_only_months": economic_number(row.get("interest_only_months")) or 0,
        "payment_frequency": row.get("payment_frequency") or "monthly"} for row in products or []]


def economic_number(value: Any) -> Any:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        return number.normalize() if number.is_finite() else str(value)
    except (TypeError, ValueError, ArithmeticError):
        return value


def financing_changed(listing: CpListing, fresh: dict[str, Any]) -> bool:
    """Economic/security changes need a separate process once capital is held."""
    normalized = normalized_financing
    existing = {key: getattr(listing, key, None) for key in ("products", "loan_amount", "offered_amount")}
    updates = canonical_financing_fields(fresh.get("financials") or {}, existing)
    if not updates:
        return False
    for key in ("loan_amount", "offered_amount", "note_rate_pct", "term_months"):
        if key in updates and economic_number(getattr(listing, key, None)) != economic_number(updates[key]):
            return True
    primary = next((row for row in existing["products"] or [] if row.get("primary")), {})
    if economic_products([dict(primary, primary=True)]) != economic_products([updates["products"][0]]):
        return True
    old = ((getattr(listing, "source_snapshot", None) or {}).get("financials") or {}).get("proposed_terms") or {}
    new = (fresh.get("financials") or {}).get("proposed_terms") or {}
    return normalized(old.get("collateral_snapshot")) != normalized(new.get("collateral_snapshot"))


def fingerprints(sections: dict[str, Any]) -> dict[str, str]:
    return {key: _fingerprint(value) for key, value in sections.items()}


def sync_state(listing: CpListing, current: dict[str, str]) -> dict[str, Any]:
    """Published or not, up to date or not, and which sections moved since."""
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    published = snapshot.get("published") if isinstance(snapshot.get("published"), dict) else None
    stamped: dict[str, str] = dict((published or {}).get("fingerprints") or {})
    changed = [
        {"key": key, "label": SECTION_LABELS.get(key, key)}
        for key in SECTION_LABELS
        if key in current
        and stamped.get(key) != current[key]
        and not (key == "package" and current[key] == _fingerprint(None) and key not in stamped)
    ]
    return {
        "published": published is not None,
        "published_at": (published or {}).get("at"),
        "published_by": (published or {}).get("by"),
        "package": (published or {}).get("package"),
        "up_to_date": published is not None and not changed,
        "changed": changed,
        "listing_status": listing.status,
    }


def mark_published(
    listing: CpListing,
    current: dict[str, str],
    *,
    by: uuid.UUID | None,
    keys: list[str] | None = None,
    package: dict[str, Any] | None = None,
) -> None:
    """Stamp the listing with what was just pushed: every section, or the
    named ones, keeping the stamp of the rest."""
    snapshot = dict(listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {})
    previous = snapshot.get("published") if isinstance(snapshot.get("published"), dict) else {}
    stamped = dict((previous or {}).get("fingerprints") or {})
    for key in keys or list(current):
        if key in current:
            stamped[key] = current[key]
    snapshot["published"] = {
        "at": datetime.now(UTC).isoformat(),
        "by": str(by) if by else None,
        "fingerprints": stamped,
        "package": package if package is not None else (previous or {}).get("package"),
    }
    listing.source_snapshot = snapshot


async def current_package(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any] | None:
    """Artifact identities and selected plan revision, free of generated timestamps."""
    if not getattr(profile, "intake_id", None):
        return None
    from app.routers.dealer_ai_intake import (
        _artifact_package_metadata,
        _artifact_pdf_metadata,
        _latest_artifact,
    )

    summary = await _latest_artifact(db, profile.intake_id, "executive_summary")
    packet = await _latest_artifact(db, profile.intake_id, "lender_packet")
    if summary is None or packet is None:
        return None
    s_meta, p_meta = _artifact_package_metadata(summary), _artifact_package_metadata(packet)
    if not s_meta.get("generation_id") or s_meta.get("generation_id") != p_meta.get(
        "generation_id"
    ):
        return None
    result = {
        "generation_id": str(s_meta.get("generation_id")),
        "summary_artifact_id": str(summary.id),
        "packet_artifact_id": str(packet.id),
    }
    pdf = _artifact_pdf_metadata(summary)
    if "investor_presentation" in (pdf.get("package_document_types") or []):
        presentation = await _latest_artifact(db, profile.intake_id, "investor_presentation")
        metadata = _artifact_package_metadata(presentation)
        valid = presentation is not None and metadata.get("status") == "current" and metadata.get("generation_id") == s_meta.get("generation_id") and _artifact_pdf_metadata(presentation).get("package_input_sha256") == pdf.get("package_input_sha256")
        result["presentation_artifact_id"] = str(presentation.id) if valid else None
        result["presentation_state"] = "current" if valid else "missing_or_stale"
    if isinstance(pdf.get("package_options"), dict):
        from app.models.financial_form_version import FinancialFormDraft
        from app.services.financial_workspace import existing_family
        from app.services.investor_package import options

        choices = options(pdf["package_options"])
        result["package_options"] = choices
        result["input_sha256"] = pdf.get("package_input_sha256")
        result["operating_plan"] = None
        if choices["include_operating_plan"]:
            family = await existing_family(db, profile.id, "operating_plan")
            if family:
                if choices["include_working_operating_plan"]:
                    draft = await db.get(FinancialFormDraft, family.id)
                    result["operating_plan"] = {"family_id": str(family.id), "revision": draft.revision if draft else None}
                else:
                    result["operating_plan"] = {"family_id": str(family.id), "version_id": str(family.current_version_id) if family.current_version_id else None}
    return result


async def current_fingerprints(
    db: AsyncSession, profile: ApplicationProfile
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """(fingerprints, sections, cockpit) for the file as it stands."""
    from app.services import application_programs as programs
    from app.services import financial_cockpit

    cockpit = await financial_cockpit.load_cockpit(db, profile)
    selections = await programs.active_selections(db, profile.id)
    sections = file_sections(
        cockpit,
        program_keys=[s.program_key for s in selections],
        package=await current_package(db, profile),
    )
    return fingerprints(sections), sections, cockpit


async def sync_for_listing(db: AsyncSession, listing: CpListing) -> dict[str, Any] | None:
    if listing.profile_id is None:
        return None
    profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None:
        return None
    current, _sections, _cockpit = await current_fingerprints(db, profile)
    return sync_state(listing, current)
