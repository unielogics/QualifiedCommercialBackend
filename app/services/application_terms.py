from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.services import client_room
from app.models.application_profile import ApplicationProfile
from app.models.application_terms import ApplicationTermSheet
from app.models.loan import Loan
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.application_terms import (
    ClientTermsCalculation,
    ClientTermsRead,
    ClientTermsWrite,
    LoanTypeOption,
)
from app.services import application_profiles as profiles
from app.services import file_contacts, funding_programs

CADENCE_PERIODS_PER_YEAR: dict[str, float] = {
    # Existing refinance math treats "daily" as business-day repayment:
    # 21 collections/month, not 365 calendar days.
    "daily": 252.0,
    "weekly": 51.96,
    "biweekly": 25.98,
    "monthly": 12.0,
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value).replace("$", "").replace(",", "").replace("x", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _first_number(mappings: list[dict], *keys: str) -> float | None:
    for mapping in mappings:
        for key in keys:
            parsed = _number(mapping.get(key))
            if parsed is not None:
                return parsed
    return None


def _nested_mappings(value: dict | None) -> list[dict]:
    if not isinstance(value, dict):
        return []
    rows = [value]
    for key in ("key_metrics", "basics", "numbers", "asset", "property", "property_details"):
        nested = value.get(key)
        if isinstance(nested, dict):
            rows.append(nested)
    return rows


def repayment_math(
    *,
    amount: float,
    apr_pct: float,
    term_months: int,
    frequency: str,
    custom_payments_per_year: int | None = None,
) -> tuple[float, int, float, float, float, float]:
    """Return periodic payment, count, periods/year, annual service, total, cost."""
    periods = (
        float(custom_payments_per_year or 0)
        if frequency == "custom"
        else CADENCE_PERIODS_PER_YEAR.get(frequency, 0.0)
    )
    if periods <= 0:
        raise ValueError("A valid repayment frequency is required")
    # Terms are positive, so floor(x + .5) gives a clear half-up rule that is
    # identical in Python and the browser preview. Python's built-in round()
    # uses ties-to-even and can otherwise disagree with JavaScript.
    payment_count = max(1, math.floor(term_months * periods / 12.0 + 0.5))
    periodic_rate = (apr_pct / 100.0) / periods
    if periodic_rate == 0:
        payment = amount / payment_count
    else:
        payment = amount * periodic_rate / (1 - (1 + periodic_rate) ** (-payment_count))
    # DSCR uses debt service actually due over the next 12 months. A six-month
    # offer must not be annualized into twelve months of payments that do not
    # exist.
    annual_debt_service = payment * min(float(payment_count), periods)
    total = payment * payment_count
    return (
        round(payment, 2),
        payment_count,
        round(periods, 3),
        round(annual_debt_service, 2),
        round(total, 2),
        round(max(0.0, total - amount), 2),
    )


@dataclass(frozen=True)
class DscrContext:
    method: str
    before: float | None
    cash_flow: float | None
    cash_flow_label: str
    current_annual_debt_service: float | None
    real_estate_carrying_costs: float | None
    source: str


async def _dscr_context(db: AsyncSession, profile: ApplicationProfile) -> DscrContext:
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    snapshot = dict(intake.result_snapshot or {}) if intake else {}
    state = dict(intake.intake_state or {}) if intake else {}
    mappings = _nested_mappings(snapshot) + _nested_mappings(state)
    intelligence = await profiles.intelligence_state(db, profile)
    current_metric = next((item for item in intelligence.metrics if item.key == "dscr"), None)
    current_dscr = _number(current_metric.value) if current_metric else None

    is_real_estate = profile.vertical == "real_estate" or bool(intake and intake.asset_rows)
    if not is_real_estate:
        inputs = intelligence.dscr_inputs or {}
        cash_flow = _number(inputs.get("bankable_ebitda"))
        current_annual = _number(inputs.get("annual_debt_service"))
        before = current_dscr
        if before is None and cash_flow is not None and current_annual and current_annual > 0:
            before = round(cash_flow / current_annual, 3)
        return DscrContext(
            method="business",
            before=before,
            cash_flow=cash_flow,
            cash_flow_label="Bankable annual EBITDA",
            current_annual_debt_service=current_annual,
            real_estate_carrying_costs=None,
            source="Verified file evidence: bankable EBITDA and current annual debt service",
        )

    loan = await db.get(Loan, profile.loan_id) if profile.loan_id else None
    rent = float(loan.monthly_rent) if loan and loan.monthly_rent is not None else _first_number(
        mappings,
        "monthly_rent",
        "in_place_monthly_rent",
        "market_monthly_rent",
        "gross_monthly_rent",
    )
    taxes = _first_number(mappings, "annual_taxes", "annual_property_taxes")
    insurance = _first_number(mappings, "annual_insurance", "insurance_annual_premium")
    hoa = _first_number(mappings, "monthly_hoa")
    if loan is not None:
        loan_taxes = float(loan.annual_taxes) if loan.annual_taxes is not None else None
        loan_insurance = float(loan.annual_insurance) if loan.annual_insurance is not None else None
        loan_hoa = float(loan.monthly_hoa) if loan.monthly_hoa is not None else None
        # Loan defaults are zero, so only positive taxes/insurance prove that
        # these required carrying costs were actually entered. HOA may truly
        # be zero and is optional once taxes and insurance are known.
        taxes = taxes if taxes is not None else (loan_taxes if loan_taxes and loan_taxes > 0 else None)
        insurance = insurance if insurance is not None else (loan_insurance if loan_insurance and loan_insurance > 0 else None)
        hoa = hoa if hoa is not None else (loan_hoa or 0)
    carrying = None
    if taxes is not None and insurance is not None:
        carrying = float(taxes or 0) + float(insurance or 0) + float(hoa or 0) * 12
    current_pitia = _first_number(mappings, "monthly_pitia", "estimated_pitia", "pitia")
    current_annual = current_pitia * 12 if current_pitia is not None else None
    annual_rent = rent * 12 if rent is not None else None
    # Keep both sides of the comparison on the same real-estate basis. Generic
    # AI DSCR may be EBITDA-based and is not comparable with rent / PITIA.
    before = float(loan.dscr) if loan and loan.dscr is not None else None
    if before is None and annual_rent is not None and current_annual and current_annual > 0:
        before = round(annual_rent / current_annual, 3)
    return DscrContext(
        method="real_estate",
        before=before,
        cash_flow=annual_rent,
        cash_flow_label="Annual gross rent",
        current_annual_debt_service=current_annual,
        real_estate_carrying_costs=carrying,
        source="Verified property evidence: rent divided by annualized PITIA",
    )


def _calculation(
    *,
    context: DscrContext,
    payment: float | None,
    payment_count: int | None,
    payments_per_year: float | None,
    new_annual_debt_service: float | None,
    amount: float | None,
    total_repayment: float | None,
    treatment: str = "additive",
    retained_annual_debt_service: float | None = None,
) -> ClientTermsCalculation:
    projected: float | None = None
    after: float | None = None
    if new_annual_debt_service is not None:
        if context.method == "real_estate":
            if treatment == "refinance":
                if (
                    context.real_estate_carrying_costs is not None
                    and retained_annual_debt_service is not None
                ):
                    projected = (
                        new_annual_debt_service
                        + context.real_estate_carrying_costs
                        + retained_annual_debt_service
                    )
            elif context.current_annual_debt_service is not None:
                projected = new_annual_debt_service + context.current_annual_debt_service
        elif treatment == "refinance":
            if retained_annual_debt_service is not None:
                projected = new_annual_debt_service + retained_annual_debt_service
        elif context.current_annual_debt_service is not None:
            projected = new_annual_debt_service + context.current_annual_debt_service
    if context.cash_flow is not None and projected and projected > 0:
        after = round(context.cash_flow / projected, 3)

    missing: list[str] = []
    if context.before is None:
        missing.append("current DSCR evidence")
    if context.cash_flow is None:
        missing.append(context.cash_flow_label.lower())
    if context.method == "business" and treatment == "additive" and context.current_annual_debt_service is None:
        missing.append("current annual debt service")
    if context.method == "real_estate":
        if treatment == "refinance" and context.real_estate_carrying_costs is None:
            missing.append("taxes, insurance, or HOA")
        if treatment == "additive" and context.current_annual_debt_service is None:
            missing.append("current annual PITIA")
    status_value = "ready" if context.before is not None and after is not None else "needs_evidence"
    if status_value == "ready":
        explanation = (
            "Current and projected DSCR are calculated from the file evidence and the proposed payment schedule."
        )
    else:
        explanation = "More evidence is needed for a complete before-and-after DSCR: " + ", ".join(dict.fromkeys(missing or ["offer terms"])) + "."
    financing_cost = (
        round(total_repayment - amount, 2)
        if total_repayment is not None and amount is not None
        else None
    )
    return ClientTermsCalculation(
        periodic_payment=payment,
        payment_count=payment_count,
        payments_per_year=payments_per_year,
        annual_debt_service=new_annual_debt_service,
        total_repayment=total_repayment,
        financing_cost=financing_cost,
        dscr_before=context.before,
        dscr_after=after,
        cash_flow_value=context.cash_flow,
        cash_flow_label=context.cash_flow_label,
        current_annual_debt_service=context.current_annual_debt_service,
        annual_property_carrying_costs=context.real_estate_carrying_costs,
        projected_annual_debt_service=projected,
        dscr_method=context.method,  # type: ignore[arg-type]
        dscr_status=status_value,  # type: ignore[arg-type]
        dscr_explanation=explanation,
        source=context.source,
    )


async def loan_type_options(db: AsyncSession, profile: ApplicationProfile) -> list[LoanTypeOption]:
    catalog = await funding_programs.public_catalog(db)
    compatible = [row for row in catalog if profile.vertical in row.verticals]
    return [
        LoanTypeOption(value=row.program_key, label=row.name, description=row.short_description)
        for row in compatible
    ]


async def current_term_sheet(db: AsyncSession, profile_id: UUID) -> ApplicationTermSheet | None:
    return (
        await db.execute(
            select(ApplicationTermSheet).where(
                ApplicationTermSheet.profile_id == profile_id,
                ApplicationTermSheet.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()


async def lock_profile_terms(db: AsyncSession, profile_id: UUID) -> None:
    """Serialize every mutation of a profile's current terms version."""
    await db.execute(
        select(ApplicationProfile.id)
        .where(ApplicationProfile.id == profile_id)
        .with_for_update()
    )


async def _recipient_state(db: AsyncSession, profile: ApplicationProfile) -> tuple[str | None, bool]:
    sources = await file_contacts.load_sources(db, profile)
    recipient = await file_contacts.client_recipient(db, profile, sources)
    if profile.primary_bucket_id is not None:
        room = await client_room.active_link(db, profile.primary_bucket_id)
        if room is not None and room.recipient_email:
            recipient.email = room.recipient_email.strip().lower()
    suppressed = bool(sources.intake and sources.intake.client_contact_suppressed)
    return recipient.email, suppressed


async def read_terms(
    db: AsyncSession,
    profile: ApplicationProfile,
    row: ApplicationTermSheet | None = None,
) -> ClientTermsRead:
    row = row if row is not None else await current_term_sheet(db, profile.id)
    options = await loan_type_options(db, profile)
    if row is not None and not any(item.value == row.program_key for item in options):
        # Preserve an existing version's immutable catalog snapshot even if the
        # program is later retired. The operator can still see and version the
        # saved structure without silently substituting a different product.
        options.insert(
            0,
            LoanTypeOption(
                value=row.program_key,
                label=row.program_name,
                description="Previously selected program",
            ),
        )
    client_email, suppressed = await _recipient_state(db, profile)
    context = await _dscr_context(db, profile)
    if row is None:
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
        initial_amount = profile.underwriting_term_sheet_amount or profile.underwriting_approved_amount
        if initial_amount is None and intake is not None:
            initial_amount = intake.requested_loan_amount
        calc = _calculation(
            context=context,
            payment=None,
            payment_count=None,
            payments_per_year=None,
            new_annual_debt_service=None,
            amount=None,
            total_repayment=None,
        )
        return ClientTermsRead(
            profile_id=profile.id,
            amount=float(initial_amount) if initial_amount is not None else None,
            expiration_days=7,
            closing_estimate_days=5,
            client_email=client_email,
            direct_client_contact_suppressed=suppressed,
            loan_type_options=options,
            calculation=calc,
        )

    total = float(row.periodic_payment) * int(row.payment_count)
    calc = ClientTermsCalculation(
        periodic_payment=float(row.periodic_payment),
        payment_count=row.payment_count,
        payments_per_year=float(row.payments_per_year),
        annual_debt_service=float(row.annual_new_debt_service),
        total_repayment=round(total, 2),
        financing_cost=round(total - float(row.amount), 2),
        dscr_before=float(row.dscr_before) if row.dscr_before is not None else None,
        dscr_after=float(row.dscr_after) if row.dscr_after is not None else None,
        cash_flow_value=float(row.cash_flow_value) if row.cash_flow_value is not None else None,
        cash_flow_label=row.cash_flow_label,
        current_annual_debt_service=float(row.current_annual_debt_service) if row.current_annual_debt_service is not None else None,
        annual_property_carrying_costs=(
            float(row.annual_property_carrying_costs)
            if row.annual_property_carrying_costs is not None
            else None
        ),
        projected_annual_debt_service=float(row.projected_annual_debt_service) if row.projected_annual_debt_service is not None else None,
        dscr_method=row.dscr_method,  # type: ignore[arg-type]
        dscr_status=row.dscr_status,  # type: ignore[arg-type]
        dscr_explanation=row.dscr_explanation,
        source=row.dscr_source,
    )
    return ClientTermsRead(
        profile_id=profile.id,
        version=row.version,
        status=row.status,  # type: ignore[arg-type]
        loan_type=row.program_key,
        loan_type_label=row.program_name,
        amount=float(row.amount),
        apr_pct=float(row.apr_pct),
        term_months=row.term_months,
        funder_type=row.funder_type,  # type: ignore[arg-type]
        funder_name=row.funder_name,
        repayment_frequency=row.repayment_frequency,  # type: ignore[arg-type]
        custom_payments_per_year=round(float(row.payments_per_year)) if row.repayment_frequency == "custom" else None,
        custom_repayment_label=row.custom_repayment_label,
        debt_service_treatment=row.debt_service_treatment,  # type: ignore[arg-type]
        retained_annual_debt_service=float(row.retained_annual_debt_service) if row.retained_annual_debt_service is not None else None,
        expiration_days=row.expiration_days,
        closing_estimate_days=row.closing_estimate_days,
        co_brand_enabled=row.co_brand_enabled,
        sponsor_name=row.sponsor_name,
        client_note=row.client_note,
        conditions=list(row.conditions or []),
        issued_at=row.issued_at,
        expires_on=row.expires_on,
        updated_at=row.updated_at,
        updated_by_user_id=row.created_by_user_id,
        client_email=client_email,
        direct_client_contact_suppressed=suppressed,
        loan_type_options=options,
        calculation=calc,
    )


async def save_terms(
    db: AsyncSession,
    profile: ApplicationProfile,
    user: User,
    payload: ClientTermsWrite,
) -> ApplicationTermSheet:
    # Serialize version creation per profile. Without this row lock, two quick
    # saves can both derive the same version number and collide at commit time.
    await lock_profile_terms(db, profile.id)
    options = await loan_type_options(db, profile)
    current = await current_term_sheet(db, profile.id)
    actual_version = current.version if current is not None else 0
    if actual_version != payload.expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "These terms changed in another session. Reload the file before saving a new version.",
        )
    if current is not None and not any(item.value == current.program_key for item in options):
        options.insert(
            0,
            LoanTypeOption(
                value=current.program_key,
                label=current.program_name,
                description="Previously selected program",
            ),
        )
    chosen = next((item for item in options if item.value == payload.loan_type), None)
    if chosen is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a loan type from the active funding catalog")
    payment, count, periods, annual, _, _ = repayment_math(
        amount=payload.amount,
        apr_pct=payload.apr_pct,
        term_months=payload.term_months,
        frequency=payload.repayment_frequency,
        custom_payments_per_year=payload.custom_payments_per_year,
    )
    context = await _dscr_context(db, profile)
    calc = _calculation(
        context=context,
        payment=payment,
        payment_count=count,
        payments_per_year=periods,
        new_annual_debt_service=annual,
        amount=payload.amount,
        total_repayment=payment * count,
        treatment=payload.debt_service_treatment,
        retained_annual_debt_service=payload.retained_annual_debt_service,
    )
    now = datetime.now(UTC)
    if current is not None:
        current.is_current = False
        current.status = "superseded"
    row = ApplicationTermSheet(
        profile_id=profile.id,
        version=(current.version + 1 if current else 1),
        is_current=True,
        status="draft",
        program_key=chosen.value,
        program_name=chosen.label,
        amount=payload.amount,
        apr_pct=payload.apr_pct,
        term_months=payload.term_months,
        funder_type=payload.funder_type,
        funder_name=payload.funder_name,
        repayment_frequency=payload.repayment_frequency,
        payments_per_year=periods,
        custom_repayment_label=payload.custom_repayment_label,
        debt_service_treatment=payload.debt_service_treatment,
        retained_annual_debt_service=payload.retained_annual_debt_service,
        periodic_payment=payment,
        payment_count=count,
        annual_new_debt_service=annual,
        projected_annual_debt_service=calc.projected_annual_debt_service,
        cash_flow_value=calc.cash_flow_value,
        cash_flow_label=calc.cash_flow_label,
        current_annual_debt_service=calc.current_annual_debt_service,
        annual_property_carrying_costs=calc.annual_property_carrying_costs,
        dscr_before=calc.dscr_before,
        dscr_after=calc.dscr_after,
        dscr_method=calc.dscr_method,
        dscr_status=calc.dscr_status,
        dscr_explanation=calc.dscr_explanation,
        dscr_source=calc.source,
        expiration_days=payload.expiration_days,
        closing_estimate_days=payload.closing_estimate_days,
        expires_on=(now + timedelta(days=payload.expiration_days)).date(),
        co_brand_enabled=payload.co_brand_enabled,
        sponsor_name=payload.sponsor_name if payload.co_brand_enabled else None,
        client_note=payload.client_note,
        conditions=payload.conditions,
        created_by_user_id=user.id,
    )
    db.add(row)
    profile.underwriting_term_sheet_amount = payload.amount
    if calc.dscr_before is not None:
        profile.underwriting_current_dscr = calc.dscr_before
    if calc.dscr_after is not None:
        profile.underwriting_approved_dscr = calc.dscr_after
    profile.underwriting_updated_by_user_id = user.id
    profile.underwriting_updated_at = now
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "term_sheet.saved",
        f"Saved client terms version {row.version}",
        target_type="application_term_sheet",
        target_id=row.id,
        metadata={
            "version": row.version,
            "program_key": row.program_key,
            "amount": float(row.amount),
            "apr_pct": float(row.apr_pct),
            "term_months": row.term_months,
            "repayment_frequency": row.repayment_frequency,
            "dscr_before": calc.dscr_before,
            "dscr_after": calc.dscr_after,
        },
    )
    return row


def issue(row: ApplicationTermSheet) -> None:
    if row.issued_at is None:
        row.issued_at = datetime.now(UTC)
        row.expires_on = (row.issued_at + timedelta(days=row.expiration_days)).date()
    row.status = "issued"
