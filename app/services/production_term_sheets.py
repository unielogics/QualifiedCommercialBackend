"""The term sheet: the loan terms a super admin or underwriter records on the
file before the final (Program Activation and Production) package can be
drafted. Append-only versions; one `current` row per profile."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerBusiness
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.lender import Lender
from app.models.production_package import ProductionPackage, ProductionTermSheet
from app.models.user import User
from app.services import application_profiles as profiles
from app.services import production_arrangement as pa
from app.services.payment_authorization import client_ip

TERM_ROLES: frozenset[Role] = frozenset({Role.SUPER_ADMIN, Role.LOAN_EXEC})
TERM_FIELDS: tuple[str, ...] = (
    "funding_party_kind", "lender_id", "funding_party_name", "facility_type", "approved_amount", "min_activation_amount",
    "rate_pct", "term_months", "monthly_debt_service", "debt_service_is_level_payment", "expected_funding_date",
    "activation_date", "commencement_date", "maturity_date", "use_of_funds", "conditions", "notes", "extra",
)


def _now() -> datetime:
    return datetime.now(UTC)


def add_months(d: date, months: int) -> date:
    y, m = d.year + (d.month - 1 + months) // 12, (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def require_term_role(user: User) -> None:
    if user.role not in TERM_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin or underwriter role required to record loan terms")


async def current_sheet(db: AsyncSession, profile_id: UUID) -> ProductionTermSheet | None:
    return (
        await db.execute(
            select(ProductionTermSheet).where(ProductionTermSheet.profile_id == profile_id, ProductionTermSheet.status == "current")
        )
    ).scalar_one_or_none()


async def sheet_history(db: AsyncSession, profile_id: UUID) -> list[ProductionTermSheet]:
    return list(
        (
            await db.execute(
                select(ProductionTermSheet).where(ProductionTermSheet.profile_id == profile_id).order_by(ProductionTermSheet.version.desc())
            )
        ).scalars().all()
    )


def sheet_terms(sheet: ProductionTermSheet) -> dict[str, Any]:
    """The sheet as the pure functions expect it (JSON-safe)."""
    return pa.jsonable({
        "id": sheet.id, "version": sheet.version, "status": sheet.status,
        **{f: getattr(sheet, f) for f in TERM_FIELDS},
        "entered_at": sheet.entered_at, "entered_by_user_id": sheet.entered_by_user_id,
    })


async def defaults(db: AsyncSession, profile: ApplicationProfile, parent: ProductionPackage | None) -> dict[str, Any]:
    """Prefill for a new sheet: underwriting amounts, the executed stage-one arrangement, the dealer's use of proceeds."""
    arrangement: dict[str, Any] = {}
    if parent is not None:
        if parent.frozen_revision_id and parent.status == "executed":
            from app.models.production_package import ProductionPackageRevision

            rev = await db.get(ProductionPackageRevision, parent.frozen_revision_id)
            arrangement = ((rev.snapshot or {}).get("arrangement") or {}) if rev is not None else {}
        else:
            arrangement = parent.arrangement or {}
    src: dict[str, str] = {}
    out: dict[str, Any] = {}

    def put(key: str, value: Any, source: str) -> None:
        if value in (None, "", 0, 0.0):
            return
        out[key] = value
        src[key] = source

    approved = profile.underwriting_approved_amount or profile.underwriting_term_sheet_amount
    put("approved_amount", float(approved) if approved else None, "underwriting")
    if "approved_amount" not in out:
        put("approved_amount", pa._num(arrangement.get("requested")), "stage_one")
    put("min_activation_amount", pa._num(arrangement.get("min_activation")), "stage_one")
    put("facility_type", arrangement.get("facility_type"), "stage_one")
    put("rate_pct", pa._num(arrangement.get("dealer_cof")), "stage_one")
    put("term_months", int(pa._num(arrangement.get("term"))) or None, "stage_one")
    if out.get("approved_amount") and out.get("term_months"):
        put("monthly_debt_service", round(pa.level_payment(out["approved_amount"], out.get("rate_pct", 0.0), out["term_months"]), 2), "level_payment")
    put("funding_party_kind", arrangement.get("funding_party") or "Lender", "stage_one")
    dealer = await db.get(DealerBusiness, profile.dealer_id) if profile.dealer_id else None
    if dealer is not None and dealer.use_of_proceeds:
        put("use_of_funds_note", " · ".join(str(u) for u in dealer.use_of_proceeds) + (f" — {dealer.use_of_proceeds_note}" if dealer.use_of_proceeds_note else ""), "dealer")
    today = date.today()
    funding = today
    put("expected_funding_date", funding.isoformat(), "today")
    put("activation_date", funding.isoformat(), "funding")
    first_next = (funding.replace(day=1) + timedelta(days=32)).replace(day=1)
    put("commencement_date", first_next.isoformat(), "funding")
    if out.get("term_months"):
        put("maturity_date", add_months(funding, int(out["term_months"])).isoformat(), "funding")
    lenders = (await db.execute(select(Lender).where(Lender.is_active.is_(True)).order_by(Lender.name))).scalars().all()
    return {"values": out, "sources": src, "lenders": [{"id": str(lender.id), "name": lender.name} for lender in lenders]}


def _validate_body(body: dict[str, Any]) -> None:
    errors = pa.validate_terms(body)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "term_sheet_invalid", "errors": errors})


async def record_sheet(
    db: AsyncSession, *, profile: ApplicationProfile, user: User, body: dict[str, Any], request: Request | None,
) -> tuple[ProductionTermSheet, ProductionPackage | None]:
    """Create or supersede the profile's current term sheet. Returns (sheet, re-applied draft final or None)."""
    require_term_role(user)
    body = dict(body)
    if body.get("lender_id"):
        lender = await db.get(Lender, UUID(str(body["lender_id"])))
        if lender is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown lender")
        body.setdefault("funding_party_name", lender.name)
        if not body.get("funding_party_name"):
            body["funding_party_name"] = lender.name
    if body.get("monthly_debt_service") in (None, "", 0) and body.get("approved_amount") and body.get("term_months"):
        body["monthly_debt_service"] = round(pa.level_payment(float(body["approved_amount"]), float(body.get("rate_pct") or 0), int(body["term_months"])), 2)
        body["debt_service_is_level_payment"] = True
    _validate_body(body)
    previous = await current_sheet(db, profile.id)
    if previous is not None:
        previous = await db.get(ProductionTermSheet, previous.id, with_for_update=True)
    # A final that is out for signature or executed pins its terms.
    child = (
        await db.execute(
            select(ProductionPackage).where(
                ProductionPackage.profile_id == profile.id, ProductionPackage.stage == 2, ProductionPackage.status != "void"
            )
        )
    ).scalar_one_or_none()
    if child is not None and child.status == "out_for_signature":
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "final_out_for_signature", "message": "Reopen the final before changing the terms."})
    if child is not None and child.status == "executed":
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "final_executed", "message": "The final has been executed; its terms are fixed."})
    now = _now()
    version = (previous.version + 1) if previous is not None else 1
    if previous is not None:
        previous.status = "superseded"
        previous.superseded_at = now
        previous.superseded_by_user_id = user.id
        await db.flush()
    sheet = ProductionTermSheet(
        profile_id=profile.id, version=version, status="current",
        funding_party_kind=str(body.get("funding_party_kind")), lender_id=UUID(str(body["lender_id"])) if body.get("lender_id") else None,
        funding_party_name=str(body.get("funding_party_name")).strip()[:180], facility_type=str(body.get("facility_type"))[:48],
        approved_amount=float(body["approved_amount"]), min_activation_amount=float(body["min_activation_amount"]),
        rate_pct=float(body.get("rate_pct") or 0), term_months=int(body["term_months"]),
        monthly_debt_service=float(body["monthly_debt_service"]), debt_service_is_level_payment=bool(body.get("debt_service_is_level_payment", False)),
        expected_funding_date=_as_date(body.get("expected_funding_date")), activation_date=_as_date(body.get("activation_date")),
        commencement_date=_as_date(body.get("commencement_date")), maturity_date=_as_date(body.get("maturity_date")),
        use_of_funds=pa.jsonable(body.get("use_of_funds")) if body.get("use_of_funds") else None,
        conditions=(body.get("conditions") or None), notes=(body.get("notes") or None), extra=pa.jsonable(body.get("extra") or {}),
        supersedes_id=previous.id if previous is not None else None, entered_by_user_id=user.id, entered_at=now, entered_ip=client_ip(request),
    )
    db.add(sheet)
    await db.flush()
    # Write-through so the Underwriting tab agrees.
    from app.routers.application_profiles import apply_underwriting_changes

    changes: dict[str, Any] = {"approved_amount": float(body["approved_amount"])}
    if not profile.underwriting_term_sheet_amount:
        changes["term_sheet_amount"] = float(body["approved_amount"])
    if profile.underwriting_status in {"submitted", "collecting_docs", "in_underwriting"}:
        changes["underwriting_status"] = "term_sheet_provided"
    await apply_underwriting_changes(db, profile, user, changes)
    await profiles.log_profile_action(
        db, profile, user, "production_term_sheet.recorded",
        f"Term sheet v{version} recorded: {pa.money(float(body['approved_amount']))} at {float(body.get('rate_pct') or 0):g}% for {int(body['term_months'])} months",
        target_type="production_term_sheet", target_id=sheet.id,
        metadata={"version": version, "supersedes_id": str(previous.id) if previous else None, "terms": sheet_terms(sheet), "ip": client_ip(request)},
    )
    reapplied: ProductionPackage | None = None
    if child is not None and child.status == "draft":
        from app.services.production_packages import reapply_terms

        reapplied = await reapply_terms(db, child, sheet, user)
    return sheet, reapplied


async def withdraw_sheet(db: AsyncSession, *, profile: ApplicationProfile, user: User, reason: str) -> ProductionTermSheet:
    require_term_role(user)
    sheet = await current_sheet(db, profile.id)
    if sheet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No term sheet on file")
    in_use = (
        await db.execute(
            select(ProductionPackage.id).where(ProductionPackage.term_sheet_id == sheet.id, ProductionPackage.status != "void").limit(1)
        )
    ).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "sheet_in_use", "message": "Void the final that uses this term sheet first."})
    sheet.status = "withdrawn"
    sheet.withdrawn_at = _now()
    sheet.withdrawn_by_user_id = user.id
    sheet.withdraw_reason = reason
    await db.flush()
    await profiles.log_profile_action(
        db, profile, user, "production_term_sheet.withdrawn", f"Term sheet v{sheet.version} withdrawn: {reason}",
        target_type="production_term_sheet", target_id=sheet.id, metadata={"reason": reason},
    )
    return sheet


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
