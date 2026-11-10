"""The term sheet: the loan terms a super admin or underwriter records on the
file before the final (Program Activation and Production) package can be
drafted. Append-only versions; one `current` row per profile."""

from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
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
from app.services import production_term_structure as term_structure
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
    raw = {
        "id": sheet.id, "version": sheet.version, "status": sheet.status,
        **{f: getattr(sheet, f) for f in TERM_FIELDS},
        "entered_at": sheet.entered_at, "entered_by_user_id": sheet.entered_by_user_id,
    }
    # Promote the versioned JSON structure for downstream callers while still
    # returning ``extra`` intact for old integrations.
    raw.update(term_structure.public_values(raw))
    return pa.jsonable(raw)


def sheet_structure(sheet: ProductionTermSheet) -> dict[str, Any]:
    """Typed repayment/rate fields for API reads, PDFs, and offer summaries."""
    return term_structure.public_values({f: getattr(sheet, f, None) for f in TERM_FIELDS})


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
        if value in (None, "") or value in (0, 0.0) and key not in {"rate_pct", "periodic_payment", "monthly_equivalent_payment", "monthly_program_coverage_amount"}:
            return
        if src.get(key) == "financial_plan" and source != "financial_plan":
            return
        out[key] = value
        src[key] = source

    approved = profile.underwriting_approved_amount or profile.underwriting_term_sheet_amount
    put("approved_amount", float(approved) if approved else None, "underwriting")
    # The loan the desk modelled on the Financials tab, when it is not recorded yet.
    plan = profile.financial_plan if isinstance(getattr(profile, "financial_plan", None), dict) else {}
    if plan.get("new_loan_amount") and not plan.get("terms_ref"):
        put("approved_amount", pa._num(plan.get("new_loan_amount")), "financial_plan")
        put("rate_pct", pa._num(plan.get("new_loan_rate_pct")), "financial_plan")
        put("term_months", int(pa._num(plan.get("new_loan_term_months")) or 0) or None, "financial_plan")
        structure = str(plan.get("new_loan_structure") or "")
        io_months = int(pa._num(plan.get("new_loan_interest_only_months")) or 0)
        term = int(pa._num(plan.get("new_loan_term_months")) or 0)
        put("repayment_config", plan.get("new_loan_repayment_config"), "financial_plan")
        if plan.get("new_loan_repayment_config") is not None and plan.get("new_loan_payments_per_year"):
            cadence = float(plan["new_loan_payments_per_year"])
            put("payments_per_year", cadence, "financial_plan")
            put("payment_frequency", next((key for key, value in term_structure.CADENCE_PERIODS_PER_YEAR.items() if value == cadence), "custom"), "financial_plan")
        if plan.get("new_loan_interest_only") or structure == "interest_only" or (term and io_months >= term):
            put("repayment_structure", "interest_only", "financial_plan")
        elif structure == "simple_interest" and plan.get("new_loan_repayment_config") is not None:
            put("repayment_structure", "simple_interest", "financial_plan")
            put("interest_only_months", io_months, "financial_plan")
        elif io_months:
            put("repayment_structure", "interest_only_then_amortizing", "financial_plan")
            put("interest_only_months", io_months, "financial_plan")
        elif structure == "simple_interest" and term:
            # The dealer sheet has no simple-interest structure: a fixed payment
            # equal to the schedule's carries the same money.
            from app.services.loan_schedule import build_schedule

            summary = build_schedule(pa._num(plan.get("new_loan_amount")), pa._num(plan.get("new_loan_rate_pct")), term, structure="simple_interest")
            if summary:
                put("repayment_structure", "fixed_payment", "financial_plan")
                put("periodic_payment", summary["payment_after"], "financial_plan")
                put("lender_payment_override", True, "financial_plan")
    if "approved_amount" not in out:
        put("approved_amount", pa._num(arrangement.get("requested")), "stage_one")
    put("min_activation_amount", pa._num(arrangement.get("min_activation")), "stage_one")
    put("facility_type", arrangement.get("facility_type"), "stage_one")
    put("rate_pct", pa._num(arrangement.get("dealer_cof")), "stage_one")
    put("term_months", int(pa._num(arrangement.get("term"))) or None, "stage_one")
    if out.get("approved_amount") and out.get("term_months"):
        put("monthly_debt_service", round(pa.level_payment(out["approved_amount"], out.get("rate_pct", 0.0), out["term_months"]), 2), "level_payment")
        put("periodic_payment", out["monthly_debt_service"], "level_payment")
        put("monthly_equivalent_payment", out["monthly_debt_service"], "level_payment")
        put("monthly_program_coverage_amount", out["monthly_debt_service"], "level_payment")
    put("facility_kind", term_structure.infer_facility_kind(out.get("facility_type")), "stage_one")
    put("repayment_structure", "fully_amortizing", "level_payment")
    put("payment_frequency", "monthly", "level_payment")
    put("payments_per_year", 12, "level_payment")
    put("rate_structure", "fixed", "level_payment")
    put("structure_version", term_structure.STRUCTURE_VERSION, "level_payment")
    put("funding_party_kind", arrangement.get("funding_party") or "Lender", "stage_one")
    put("funder_type", term_structure.infer_funder_type(out.get("funding_party_kind")), "stage_one")
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
    if out.get("repayment_config") is not None:
        calculated = term_structure.calculate(out)
        for key in ("repayment_config", "periodic_payment", "post_io_payment", "monthly_equivalent_payment", "monthly_program_coverage_amount", "monthly_program_coverage_basis", "balloon_amount", "schedule"):
            out[key] = calculated.get(key)
        out["monthly_debt_service"] = calculated["monthly_program_coverage_amount"]
    lenders = (await db.execute(select(Lender).where(Lender.is_active.is_(True)).order_by(Lender.name))).scalars().all()
    return {"values": out, "sources": src, "lenders": [{"id": str(lender.id), "name": lender.name} for lender in lenders]}


def _validate_body(body: dict[str, Any]) -> None:
    errors = pa.validate_terms(body) + term_structure.validation_errors(body)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "term_sheet_invalid", "errors": errors})


async def _add_dscr_snapshot(
    db: AsyncSession,
    profile: ApplicationProfile,
    values: dict[str, Any],
    budget: dict[str, Any] | None = None,
    budget_context: dict[str, Any] | None = None,
) -> None:
    """Reuse the application-term DSCR evidence pipeline when it is available.

    Missing evidence is a first-class result and must not prevent the desk from
    recording otherwise valid terms.  A provider/evidence failure is likewise
    captured as unavailable instead of replacing a correctly calculated offer.
    """
    try:
        from app.services import application_terms

        context = await application_terms._dscr_context(db, profile)
        calculation = application_terms._calculation(
            context=context,
            payment=values.get("periodic_payment"),
            payment_count=values.get("payment_count"),
            payments_per_year=values.get("payments_per_year"),
            new_annual_debt_service=values.get("annual_debt_service"),
            amount=values.get("payment_basis_amount"),
            total_repayment=values.get("total_repayment"),
            treatment=str(values.get("debt_service_treatment") or "additive"),
            retained_annual_debt_service=values.get("retained_annual_debt_service"),
        )
        if budget is not None:
            from app.services import deal_budget
            from app.services.financial_plan_budget import apply_to_terms_calculation

            terms = {**_budget_terms(values), **(budget_context or {})}
            terms["coverage_context"] = {"income_annual": calculation.cash_flow_value,
                "debt_service_annual": calculation.projected_annual_debt_service,
                "new_loan_annual_debt_service": values.get("annual_debt_service"),
                "base_dscr": calculation.dscr_after, "basis": calculation.cash_flow_label}
            values["deal_budget_calculation"] = deal_budget.calculate(budget, terms, getattr(profile, "use_of_funds", None))
            if getattr(context, "operating_plan", None):
                values["deal_budget_calculation"]["operating_plan"] = dict(context.operating_plan)
            calculation = apply_to_terms_calculation(calculation, budget, values["deal_budget_calculation"])
        values.update(
            {
                "dscr_before": calculation.dscr_before,
                "dscr_after": calculation.dscr_after,
                "dscr_status": calculation.dscr_status,
                "dscr_explanation": calculation.dscr_explanation,
                "dscr_source": calculation.source,
            }
        )
    except Exception:
        values.update(
            {
                "dscr_before": None,
                "dscr_after": None,
                "dscr_status": "needs_evidence",
                "dscr_explanation": "DSCR could not be completed from the evidence currently on file.",
                "dscr_source": "File evidence unavailable",
            }
        )


def _budget_terms(values):
    kind = values.get("repayment_structure")
    return {"amount": values.get("payment_basis_amount") or values.get("approved_amount"),
        "rate_pct": values.get("effective_rate_pct", values.get("rate_pct")), "term_months": values.get("term_months"),
        "structure": "interest_only" if kind in {"interest_only", "revolving_interest_only"} else "simple_interest" if kind == "simple_interest" else "amortizing",
        "schedule_unavailable": kind in {"fixed_payment", "custom"} or bool(values.get("lender_payment_override")),
        "interest_only_months": values.get("interest_only_months"), "repayment_config": values.get("repayment_config"),
        "payments_per_year": values.get("payments_per_year") or 12}


def _apply_financed_principal(body, structure, gross):
    """A full term loan follows G; independent approved limits stay separate."""
    from app.services.deal_budget import number

    approved, basis = number(body.get("approved_amount")), number(structure.get("payment_basis_amount"))
    initial = number(structure.get("initial_draw_amount"))
    gross = number(gross)
    if initial is not None and initial != basis:
        raise HTTPException(422, "The actual draw and payment basis disagree. Confirm the funded principal before saving loan costs together.")
    full_facility = approved == basis and (initial is None or initial == approved)
    if full_facility:
        body.update(approved_amount=float(gross), payment_basis_amount=float(gross), initial_draw_amount=float(gross))
    elif (structure.get("facility_kind") in {"revolving_loc", "heloc", "hybrid"}
            and structure.get("repayment_structure") not in {"fixed_payment", "custom"}
            and not structure.get("lender_payment_override") and approved is not None and gross <= approved):
        body.update(payment_basis_amount=float(gross), initial_draw_amount=float(gross))
    else:
        raise HTTPException(422, "The calculated funded principal does not fit this independently approved facility or partial draw. Review its approved limit and funded draw before saving financed costs.")


async def record_sheet(
    db: AsyncSession, *, profile: ApplicationProfile, user: User, body: dict[str, Any], request: Request | None,
) -> tuple[ProductionTermSheet, ProductionPackage | None]:
    """Create or supersede the profile's current term sheet. Returns (sheet, re-applied draft final or None)."""
    require_term_role(user)
    body = deepcopy(body)
    combined = body.get("deal_budget") is not None
    combined_payload, fingerprint = None, None
    from app.services import deal_budget
    if combined:
        from app.schemas.production_package import ProductionTermSheetBody

        combined_payload = ProductionTermSheetBody.model_validate(body)
        body = combined_payload.model_dump(mode="json", exclude_unset=True)
        fingerprint = deal_budget.save_request_fingerprint("production", profile.id, combined_payload.model_dump(mode="json"))
    from app.services.application_terms import lock_profile_terms

    await lock_profile_terms(db, profile.id)
    if isinstance(db, AsyncSession) or combined and hasattr(db, "refresh"):
        await db.refresh(profile, attribute_names=["investor_collateral", "deal_budget", "financial_plan"])
    from app.services.investor_collateral import snapshot as collateral_snapshot

    frozen_collateral = collateral_snapshot(profile)
    frozen_budget = deal_budget.snapshot(profile)
    previous = await current_sheet(db, profile.id) if combined else None
    if combined and deal_budget.find_terms_save_receipt(profile, "production", combined_payload.idempotency_key,
            fingerprint, user, getattr(previous, "id", None), getattr(previous, "version", None)):
        return previous, None
    expected_budget = body.get("expected_deal_budget_revision")
    if expected_budget is None:
        expected_budget = (body.get("extra") or {}).get("expected_deal_budget_revision")
    if expected_budget is not None and expected_budget != frozen_budget["revision"]:
        raise HTTPException(409, "Deal costs changed. Reload before recording a new terms version.")
    from app.services.use_of_funds import source_funding_data
    requested_amount, _request_source, _purpose, _dealer = await source_funding_data(db, profile)
    expected_collateral = body.get("expected_collateral_revision")
    if expected_collateral is None:
        expected_collateral = (body.get("extra") or {}).get("expected_collateral_revision")
    if expected_collateral is not None and expected_collateral != frozen_collateral["revision"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "The collateral disclosure changed. Reload it before recording a new terms version.")
    if not combined:
        previous = await current_sheet(db, profile.id)
    expected_version = body.get("expected_version")
    if expected_version is not None and expected_version != (previous.version if previous is not None else 0):
        raise HTTPException(status.HTTP_409_CONFLICT, "These production terms changed in another session. Reload before saving a new version.")
    previous_extra = getattr(previous, "extra", None) or {}
    if previous_extra.get("repayment_config") is not None and "repayment_config" not in body and "repayment_config" not in (body.get("extra") or {}):
        raise HTTPException(status.HTTP_409_CONFLICT, "These production terms contain a flexible repayment schedule. Reload and include repayment_config, or explicitly send null to clear it, before saving a new version.")
    if body.get("lender_id"):
        lender = await db.get(Lender, UUID(str(body["lender_id"])))
        if lender is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown lender")
        body.setdefault("funding_party_name", lender.name)
        if not body.get("funding_party_name"):
            body["funding_party_name"] = lender.name
    now = _now()
    try:
        structure = term_structure.calculate(body, entered_on=now.date())
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "term_sheet_invalid", "errors": [str(exc)]}) from exc
    if combined:
        from app.services import financial_plan

        draft = combined_payload.financial_plan
        financial_plan.validate_plan_write(getattr(profile, "financial_plan", None), draft)
        if deal_budget._timestamp((getattr(profile, "financial_plan", None) or {}).get("updated_at")) != deal_budget._timestamp(combined_payload.expected_plan_updated_at):
            raise HTTPException(409, "The financial plan changed. Reload before saving the loan and costs together.")
        frozen_budget, principal = await deal_budget.stage_for_terms(db, profile, combined_payload.deal_budget, _budget_terms({**body, **structure}), user)
        _apply_financed_principal(body, structure, principal["gross_loan_amount"])
        try:
            structure = term_structure.calculate(body, entered_on=now.date())
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "term_sheet_invalid", "errors": [str(exc)]}) from exc
        expected_budget = frozen_budget["revision"]
        record = draft.model_dump(mode="json", exclude={"expected_updated_at", "expected_deal_budget_revision"})
        overrides = dict(record.get("overrides") or {})
        if structure.get("debt_service_treatment") != "refinance" and (overrides.get("debt_service_note") or "").startswith("Refinance per "):
            overrides.pop("debt_service_monthly", None)
            overrides.pop("debt_service_note", None)
            record["overrides"] = overrides or None
        record.update(updated_at=now.isoformat(), updated_by=str(user.id))
        profile.financial_plan = financial_plan.normalize_plan(record)
    await deal_budget.enforce_origination_cap(db, profile, frozen_budget, _budget_terms({**body, **structure})["amount"] if combined else body.get("approved_amount"))
    from app.services.financial_plan_budget import validate_write
    validate_write(frozen_budget, expected_budget, _budget_terms({**body, **structure}))
    await _add_dscr_snapshot(db, profile, structure, frozen_budget,
        {"requested_amount": deal_budget.money(requested_amount), "proposal_use_of_funds": body.get("use_of_funds"),
            "proposal_use_of_funds_amount": structure.get("payment_basis_amount") or body.get("approved_amount")})
    body["rate_pct"] = structure["effective_rate_pct"]
    body["monthly_debt_service"] = structure["monthly_program_coverage_amount"] if structure.get("repayment_config") is not None else structure["monthly_equivalent_payment"]
    body["debt_service_is_level_payment"] = term_structure.is_level_payment(
        structure,
        term_months=int(body.get("term_months") or 0),
    )
    body["extra"] = term_structure.stored_extra(body.get("extra"), structure)
    if combined:
        # Only new combined versions opt into this funded-principal adapter;
        # historical unconfigured sheets retain their previous interpretation.
        body["extra"]["combined_funded_principal_v1"] = True
    # Security belongs to this immutable version, regardless of any stale or
    # caller-supplied snapshot in extra. Legacy sheets are left unrecorded.
    body["extra"].update(collateral_snapshot=frozen_collateral, collateral_revision=frozen_collateral["revision"])
    body["extra"].update(deal_budget_snapshot=frozen_budget, deal_budget_revision=frozen_budget["revision"],
        deal_budget_calculation=structure.get("deal_budget_calculation") or deal_budget.calculate(frozen_budget, {"amount": structure.get("payment_basis_amount") or body.get("approved_amount"),
            "requested_amount": deal_budget.money(requested_amount), "proposal_use_of_funds": body.get("use_of_funds"),
            "proposal_use_of_funds_amount": structure.get("payment_basis_amount") or body.get("approved_amount"),
            "rate_pct": body["rate_pct"], "term_months": body.get("term_months"),
            "structure": "interest_only" if structure.get("repayment_structure") in {"interest_only", "revolving_interest_only"} else "simple_interest" if structure.get("repayment_structure") == "simple_interest" else "amortizing",
            "schedule_unavailable": structure.get("repayment_structure") in {"fixed_payment", "custom"} or bool(structure.get("lender_payment_override")),
            "interest_only_months": structure.get("interest_only_months"), "repayment_config": structure.get("repayment_config"),
            "payments_per_year": structure.get("payments_per_year") or 12}, getattr(profile, "use_of_funds", None)))
    # Validate the enriched values, not only the sparse request.  This keeps
    # every saved version internally complete even when the caller is legacy.
    validation_view = {**body, **structure}
    validation_view["rate_pct"] = body["rate_pct"]
    validation_view["monthly_debt_service"] = body["monthly_debt_service"]
    _validate_body(validation_view)
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
    from app.services import plan_terms_link

    await plan_terms_link.adopt_for_profile(db, profile, plan_terms_link.recorded_from_production_sheet(sheet), user=user)
    await profiles.log_profile_action(
        db, profile, user, "production_term_sheet.recorded",
        f"Term sheet v{version} recorded: {pa.money(float(body['approved_amount']))} at {float(body.get('rate_pct') or 0):g}% for {int(body['term_months'])} months ({term_structure.structure_label(structure['repayment_structure'])})",
        target_type="production_term_sheet", target_id=sheet.id,
        metadata={"version": version, "supersedes_id": str(previous.id) if previous else None, "terms": sheet_terms(sheet), "ip": client_ip(request)},
    )
    reapplied: ProductionPackage | None = None
    if child is not None and child.status == "draft":
        from app.services.production_packages import reapply_terms

        reapplied = await reapply_terms(db, child, sheet, user)
    if combined:
        deal_budget.record_terms_save_receipt(profile, "production", combined_payload.idempotency_key, fingerprint, user, sheet.id, sheet.version)
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
    from app.services import plan_terms_link

    await plan_terms_link.release_for_profile(db, profile, str(sheet.id), user=user)
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
