"""Listings, subscriptions, participations and transfers: the rules.

Routers validate shapes and call in here; nothing in here reads a request.
Money is Decimal end to end. ``committed_amount`` is derived from the
subscriptions that hold an allocation, never stored.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import enums as E
from ..models import (
    CpListing,
    CpListingDocument,
    CpListingEvent,
    CpMember,
    CpMemberEntity,
    CpParticipation,
    CpSubscription,
    CpTransfer,
)
from . import filings as filing_svc
from . import refs as ref_svc

ZERO = Decimal("0")


class RuleError(ValueError):
    """A business rule said no. Routers map it to a 409 with the code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _d(value: Any) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def now_utc() -> datetime:
    return datetime.now(UTC)


async def log_event(
    db: AsyncSession, listing_id: uuid.UUID, kind: str, *, actor_user_id: uuid.UUID | None, detail: dict | None = None
) -> None:
    db.add(CpListingEvent(listing_id=listing_id, kind=kind, actor_user_id=actor_user_id, detail=detail or {}, occurred_at=now_utc()))
    await db.flush()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def committed_amounts(db: AsyncSession, listing_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    if not listing_ids:
        return {}
    rows = (
        await db.execute(
            select(CpSubscription.listing_id, func.coalesce(func.sum(CpSubscription.amount), 0))
            .where(CpSubscription.listing_id.in_(listing_ids))
            .where(CpSubscription.status.in_(sorted(E.COMMITTED_STATUSES)))
            .group_by(CpSubscription.listing_id)
        )
    ).all()
    return {listing_id: _d(total) for listing_id, total in rows}


def remaining_amount(listing: CpListing, committed: Decimal) -> Decimal:
    offered = _d(listing.offered_amount)
    return max(offered - committed, ZERO)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def days_left(listing: CpListing, now: datetime | None = None) -> int | None:
    if listing.closes_at is None or listing.status not in E.SUBSCRIBABLE_STATUSES:
        return None
    now = now or now_utc()
    delta = _aware(listing.closes_at) - now
    return max(0, delta.days + (1 if delta.seconds > 0 and delta.days >= 0 else 0))


def card(listing: CpListing, committed: Decimal, *, now: datetime | None = None) -> dict[str, Any]:
    offered = _d(listing.offered_amount)
    remaining = remaining_amount(listing, committed)
    pct = int(round(100 * float(committed) / float(offered))) if offered > 0 else 0
    place = ", ".join(p for p in (listing.city, listing.state) if p) or None
    return {
        "id": listing.id,
        "ref": listing.ref,
        "name": listing.name,
        "vertical": listing.vertical,
        "vertical_label": E.VERTICAL_LABELS.get(listing.vertical, listing.vertical),
        "program_label": listing.program_label,
        "city": listing.city,
        "state": listing.state,
        "place": place,
        "collateral_type": listing.collateral_type,
        "lien_position": listing.lien_position,
        "note_rate_pct": listing.note_rate_pct,
        "term_months": listing.term_months,
        "ltv_pct": listing.ltv_pct,
        "ltv_kind": listing.ltv_kind,
        "minimum_investment": listing.minimum_investment,
        "loan_amount": listing.loan_amount,
        "offered_amount": listing.offered_amount,
        "committed_amount": committed,
        "remaining_amount": remaining,
        "pct_committed": min(pct, 100),
        "payment_frequency": listing.payment_frequency,
        "personal_guarantee": listing.personal_guarantee,
        "status": listing.status,
        "outcome": listing.outcome,
        "products": [project_product_payments(product) for product in listing.products or []],
        "opens_at": listing.opens_at,
        "closes_at": listing.closes_at,
        "days_left": days_left(listing, now),
        "funded_at": listing.funded_at,
        "repaid_at": listing.repaid_at,
        "maturity_date": listing.maturity_date,
        "published_at": listing.published_at,
    }


def project_product_payments(product: dict[str, Any]) -> dict[str, Any]:
    """Correct displayed installment metadata without rewriting an offer."""
    out = deepcopy(product)
    if out.get("repayment_config") is None:
        return out
    from pydantic import ValidationError

    from ..schemas import ListingProductIn

    try:
        projected = ListingProductIn.model_validate(out).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError, OverflowError):
        out.update(recorded_monthly_payment=out.get("monthly_payment"), monthly_payment=None,
                   coverage_payment_monthly=None, interest_only_payment_monthly=None,
                   repayment_payment_monthly=None, maturity_balance=None, payment_phases=[],
                   payment_projection_unavailable=True)
        return out
    out.pop("payment_projection_unavailable", None)
    for key in ("monthly_payment", "coverage_payment_monthly", "interest_only_payment_monthly",
                "repayment_payment_monthly", "maturity_balance", "payment_phases"):
        out[key] = projected[key]
    return out


#: The cockpit figures a member may see on an offering.
PARTNER_FINANCIAL_KEYS = (
    "generated_at",
    "bank_months",
    "bank_months_accepted",
    "avg_monthly_deposits",
    "annualized_deposits",
    "avg_monthly_net",
    "average_balance",
    "nsf_total",
    "deposit_trend_pct",
    "dscr",
    "dscr_basis",
    "dscr_status",
    "pro_forma_dscr",
    "dscr_after_plan",
    "debt_service_after_monthly",
    "annual_debt_service",
    "net_profit",
    "net_profit_basis",
    "net_profit_period",
    "tax_year",
    "tax_gross_receipts",
    "tax_income",
)


def partner_operating_earnings(value: Any) -> dict[str, Any] | None:
    """Published figures without access to the file's private source inventory."""
    if not isinstance(value, dict):
        return None
    from app.services.financial_metric_reporting import noi_governance_brief

    def fields(row, keys):
        return {key: row[key] for key in keys if key in row} if isinstance(row, dict) else {}

    trend = value.get("revenue_trend") or {}
    noi = value.get("noi") or {}
    noi_governance = noi_governance_brief(noi, internal=False)
    if noi and not noi.get("policy_version"):
        previous = noi.get("historical") or {}
        noi_governance.update(policy_version="legacy-unversioned",
            status="provisional" if previous.get("annualized_value") is not None or previous.get("value") is not None else "unavailable",
            review_needed=True, status_reason="This published NOI snapshot predates the current calculation policy. Refresh the financial package to review its NOI basis.",
            reason_codes=["legacy_noi_snapshot"])
    potential = value.get("potential") or {}
    improvement = value.get("improvement_plan") or {}

    def plan_projection(row):
        return fields(row, ("status", "earnings", "noi", "first_year_earnings", "dscr_current", "dscr_with_refinance",
            "annual_debt_service", "refinance_annual_debt_service", "target_gap_current", "target_gap_refinance"))

    def plan_adjustment(row):
        safe = fields(row, ("label", "kind", "review_state", "net_annual_impact", "first_year_impact", "implementation_one_time",
            "effective_date", "explanation", "source_note", "owner_name", "included_in_approved_projection", "issues", "risk_flags", "source_names"))
        safe["calculation"] = fields(row.get("calculation"), ("cadence", "units_per_period", "current_amount", "proposed_amount",
            "current_unit_price", "proposed_unit_price", "current_unit_cost", "proposed_unit_cost", "contribution_margin_pct",
            "processing_volume", "processing_transaction_count", "current_processing_rate_pct", "proposed_processing_rate_pct",
            "current_processing_rate_basis", "proposed_processing_rate_basis", "current_processing_fixed_fee", "proposed_processing_fixed_fee",
            "current_processing_per_transaction_fee", "proposed_processing_per_transaction_fee"))
        return safe

    safe_improvement = None
    if improvement:
        safe_improvement = {
            "baseline": {**fields(improvement.get("baseline"), ("period", "currency", "basis", "revenue", "earnings", "earnings_kind", "noi", "noi_kind", "noi_label", "noi_year", "noi_period", "noi_currency", "noi_basis", "noi_staff_reviewed", "noi_provisional", "dscr", "annual_debt_service", "provisional")),
                "noi_governance": noi_governance_brief((improvement.get("baseline") or {}).get("noi_governance"), internal=False)},
            "projection": {key: plan_projection((improvement.get("projection") or {}).get(key)) for key in ("proposed", "approved")},
            "totals": {key: fields((improvement.get("totals") or {}).get(key), ("annual_impact", "monthly_impact", "first_year_impact", "one_time_cost", "complete", "count")) for key in ("proposed", "approved")},
            "adjustments": [plan_adjustment(row) for row in improvement.get("adjustments", []) if isinstance(row, dict)],
            "missing": list(improvement.get("missing") or []), "risk_flags": list(improvement.get("risk_flags") or []),
            "notes": list(improvement.get("notes") or []), "all_actions_reviewed": bool(improvement.get("all_actions_reviewed")),
        }

    def bridge(row):
        if not isinstance(row, dict):
            return None
        return {**fields(row, ("annual_debt_service", "dscr", "no_debt_service", "earnings_less_debt_service", "funding_shortfall")),
                "targets": [fields(target, ("ratio", "earnings_required", "margin_pct", "improvement_required")) for target in row.get("targets", [])]}

    return {
        "historical": fields(value.get("historical"), ("value", "annualized_value", "label", "kind", "period", "months_covered",
                                                       "currency", "complete", "provisional", "staff_adjusted")) or None,
        "improvement_plan": safe_improvement,
        "revenue_trend": {
            **fields(trend, ("status",)),
            "periods": [fields(row, ("year", "period_start", "period_end", "months_covered", "revenue", "average_monthly_revenue",
                                    "ebitda", "ebitda_kind", "currency", "basis")) for row in trend.get("periods", []) if isinstance(row, dict)],
            "comparison": fields(trend.get("comparison"), ("prior_year", "current_year", "prior_revenue", "current_revenue",
                                                            "change_amount", "change_pct", "monthly_change", "comparable", "direction")) or None,
            "forecast": fields(trend.get("forecast"), ("status", "method", "target_year", "projected_revenue", "change_amount",
                                                        "change_pct", "projected_ebitda", "ebitda_kind", "margin")) or None,
        },
        "potential": {
            **fields(potential, ("available",)),
            "baseline": fields(potential.get("baseline"), ("revenue", "earnings", "kind", "margin_pct", "period", "currency", "basis", "provisional", "annualized")),
            "current": bridge(potential.get("current")), "plan": bridge(potential.get("plan")),
            "forecast": fields(potential.get("forecast"), ("status", "earnings", "revenue", "kind", "dscr_current", "dscr_plan")) or None,
        } if potential else None,
        "noi": {
            **noi_governance,
            "reason": noi.get("reason"),
            "definition": fields(noi.get("definition"), ("key", "label", "formula", "includes_depreciation_and_amortization")),
            "historical": fields(noi.get("historical"), ("value", "annualized_value", "label", "kind", "period", "year", "months_covered", "currency", "basis", "complete", "provisional", "staff_reviewed", "tax_derived_estimate", "source_accepted")) or None,
            "source_historical": fields(noi.get("source_historical"), ("value", "annualized_value", "label", "kind", "period", "year", "currency", "basis", "complete", "provisional")) or None,
            "years": [fields(row, ("year", "period", "value", "annualized_value", "currency", "basis", "complete", "provisional")) for row in noi.get("years", [])],
            "trend": fields(noi.get("trend"), ("direction", "comparable", "prior_year", "current_year", "change_amount", "change_pct")),
            "scenario": fields(noi.get("scenario"), ("current", "plan", "annual_debt_service_current", "annual_debt_service_plan", "after_debt_service_current", "after_debt_service_plan")),
        } if noi else None,
        "qualification_policy": "Historical earnings and illustrative forecasts are separate. Forecasts do not establish current program eligibility.",
    }


def partner_financials(listing: CpListing) -> dict[str, Any] | None:
    """The underwriting figures a member sees on an offering: what the file
    showed when the desk cut or last refreshed the listing, and the desk's
    plan beside it (what changes, the coverage before and after). None for a
    listing that was not cut from a file."""
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    financials = snapshot.get("financials")
    if not isinstance(financials, dict):
        return None
    out: dict[str, Any] = {k: financials.get(k) for k in PARTNER_FINANCIAL_KEYS if financials.get(k) is not None}
    earnings = partner_operating_earnings(financials.get("operating_earnings"))
    if earnings is not None:
        out["operating_earnings"] = earnings
    plan = financials.get("plan") if isinstance(financials.get("plan"), dict) else {}
    if plan.get("active"):
        out["plan"] = {
            "modifications": [
                {"kind": m.get("kind"), "label": m.get("label"), "detail": m.get("detail")}
                for m in plan.get("modifications") or []
                if isinstance(m, dict)
            ],
            "impact": plan.get("impact") or {},
        }
    terms = financials.get("proposed_terms")
    if isinstance(terms, dict) and terms.get("amount"):
        out["proposed_terms"] = partner_terms(terms)
    if isinstance(financials.get("deal_budget"), dict):
        from app.services.financing_budget_disclosure import public_budget

        out["deal_budget"] = public_budget(financials["deal_budget"])
    if snapshot.get("refreshed_at"):
        out["refreshed_at"] = snapshot["refreshed_at"]
    return out or None


PARTNER_TERMS_KEYS = (
    "deal_budget_revision",
    "current_deal_budget_revision",
    "deal_budget_stale",
    "active_proposal",
    "program_key",
    "program_name",
    "terms_state",
    "recorded_terms_ref",
    "collateral_revision",
    "current_collateral_revision",
    "collateral_stale",
    "source",
    "label",
    "amount",
    "rate_pct",
    "term_months",
    "structure",
    "structure_label",
    "interest_only",
    "interest_only_months",
    "repayment_config",
    "payments_per_year",
    "monthly_payment",
    "coverage_payment_monthly",
    "regular_payment_monthly",
    "interest_only_payment_monthly",
    "repayment_payment_monthly",
    "maturity_balance",
    "payment_phases",
    "payment_io_monthly",
    "payment_after_monthly",
    "describe",
    "payment_note",
    "schedule",
    "schedule_rows",
    "risk",
)


def partner_terms(terms: dict[str, Any]) -> dict[str, Any]:
    """The proposed terms a member sees: the structure, the schedule and the
    principal at risk, with explicit investor-safe recorded cost disclosures."""
    result = {k: terms.get(k) for k in PARTNER_TERMS_KEYS if terms.get(k) is not None}
    # Partial/legacy schedule callers have supplied no security disclosure.
    # Do not turn that absence into a claim that collateral was disabled.
    if "collateral_snapshot" in terms or terms.get("recorded_terms_ref"):
        result["collateral"] = partner_collateral(terms.get("collateral_snapshot"),
            recorded=bool(terms.get("recorded_terms_ref")) or terms.get("source") == "term_sheet")
    if isinstance(terms.get("deal_budget_snapshot"), dict) and terms.get("deal_budget_revision") is not None:
        from app.services.financing_budget_disclosure import public_budget

        recorded = bool(terms.get("recorded_terms_ref")) or terms.get("source") == "term_sheet"
        result["deal_budget"] = public_budget({"recorded": terms["deal_budget_snapshot"] if recorded else {},
            "current": {"revision": terms.get("current_deal_budget_revision")} if recorded else terms["deal_budget_snapshot"],
            "basis": "recorded_terms" if recorded else "staff_proposal", "stale": terms.get("deal_budget_stale"),
            "calculation": terms.get("deal_budget_calculation") or {}})
    return result


def partner_collateral(value: Any, *, recorded: bool) -> dict[str, Any]:
    """Only declared security facts, never private notes or evidence identifiers."""
    raw = value if isinstance(value, dict) else {}
    allowed = ("asset_type", "description", "property_type", "estimated_value", "existing_liens_total",
        "available_lien_amount", "proposed_lien_position", "valuation_date", "currency")
    def project(row):
        result = {key: deepcopy(row[key]) for key in allowed if row.get(key) is not None}
        if isinstance(row.get("address"), dict):
            result["address"] = {key: row["address"][key] for key in ("street", "city", "state", "postal_code") if row["address"].get(key)}
        return result
    enabled = bool(raw.get("enabled"))
    return {"enabled": enabled, "revision": raw.get("revision"),
        "basis": "recorded_terms" if recorded and raw else "unrecorded_terms" if recorded else "staff_proposal",
        "properties": [project(row) for row in raw.get("properties") or [] if isinstance(row, dict)] if enabled else [],
        "assets": [project(row) for row in raw.get("assets") or [] if isinstance(row, dict)] if enabled else [],
        "status": "Declared only; ownership, valuation, priority and lending capacity require review."}


def document_out(doc: CpListingDocument) -> dict[str, Any]:
    return {
        "id": doc.id,
        "kind": doc.kind,
        "kind_label": E.DOCUMENT_KIND_LABELS.get(doc.kind, doc.kind),
        "name": doc.name,
        "content_type": doc.content_type,
        "size_bytes": doc.size_bytes,
        "version": doc.version,
        "status": doc.status,
    }


def ready_documents(listing: CpListing) -> list[CpListingDocument]:
    return [d for d in listing.documents if d.status == E.DocumentStatus.READY]


def publish_blockers(listing: CpListing) -> list[str]:
    missing: list[str] = []
    if not listing.name:
        missing.append("name")
    if listing.note_rate_pct is None:
        missing.append("note_rate_pct")
    if not listing.term_months:
        missing.append("term_months")
    if _d(listing.offered_amount) <= 0:
        missing.append("offered_amount")
    if _d(listing.minimum_investment) <= 0:
        missing.append("minimum_investment")
    if _d(listing.minimum_investment) > _d(listing.offered_amount) > 0:
        missing.append("minimum_investment exceeds offered_amount")
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    financials = snapshot.get("financials") if isinstance(snapshot.get("financials"), dict) else {}
    terms = financials.get("proposed_terms") if isinstance(financials.get("proposed_terms"), dict) else {}
    if terms.get("active_proposal") is False:
        missing.append("Restore or synchronize the working financing proposal before publishing; issued terms are a historical reference")
    if terms.get("recorded_terms_ref") and (terms.get("collateral_stale") or terms.get("terms_state") == "differs"):
        missing.append("Save an updated terms version for the changed financing or collateral before publishing")
    budget = financials.get("deal_budget")
    if isinstance(budget, dict):
        if budget.get("stale") or terms.get("deal_budget_stale"):
            missing.append("Save an updated terms version for the changed cost budget before publishing")
        if (budget.get("calculation") or {}).get("status") != "ready":
            missing.append("Confirm the closing cost budget, funding mode and investor charges before publishing")
        principal = (budget.get("calculation") or {}).get("principal_solution") or {}
        if principal and (principal.get("status") != "calculated" or principal.get("requires_apply")):
            missing.append("Apply the solved gross principal to the shared loan before publishing")
        allocation = budget.get("current_allocation_reconciliation") or (budget.get("calculation") or {}).get("allocation_reconciliation") or {}
        if allocation.get("status") in {"overallocated", "unallocated"}:
            missing.append("Reconcile the current use-of-funds allocations with net loan proceeds at funding before publishing")
    if terms.get("amount") is not None:
        if _d(listing.offered_amount) > _d(terms["amount"]):
            missing.append("offered_amount exceeds the canonical financing amount")
        if any(_d(getattr(listing, field, None)) != _d(terms.get(key)) for field, key in (("loan_amount", "amount"), ("note_rate_pct", "rate_pct"), ("term_months", "term_months"))):
            missing.append("Refresh financing from the file before publishing")
    kinds = {d.kind for d in ready_documents(listing)}
    if E.DocumentKind.PPM not in kinds:
        missing.append("private placement memorandum not uploaded")
    return missing


@dataclass
class MarketFilters:
    vertical: str = "all"
    statuses: list[str] | None = None
    programs: list[str] | None = None
    collaterals: list[str] | None = None
    frequencies: list[str] | None = None
    lien: str | None = None
    state: str | None = None
    min_rate: Decimal | None = None
    max_term: int | None = None
    max_ltv: Decimal | None = None
    max_minimum: Decimal | None = None
    min_remaining: Decimal | None = None
    personal_guarantee: bool = False
    sort: str = "newest"


def _passes(listing: CpListing, committed: Decimal, f: MarketFilters, *, skip: str | None = None) -> bool:
    if skip != "status" and f.statuses and listing.status not in f.statuses:
        return False
    if skip != "program" and f.programs and listing.program_label not in f.programs:
        return False
    if skip != "collateral" and f.collaterals and (listing.collateral_type or "") not in f.collaterals:
        return False
    if skip != "frequency" and f.frequencies and listing.payment_frequency not in f.frequencies:
        return False
    if f.lien and f.lien != "any" and listing.lien_position != f.lien:
        return False
    if f.state and f.state != "any" and (listing.state or "") != f.state:
        return False
    if f.min_rate is not None and _d(listing.note_rate_pct) < f.min_rate:
        return False
    if f.max_term is not None and (listing.term_months or 0) > f.max_term:
        return False
    if f.max_ltv is not None and _d(listing.ltv_pct) > f.max_ltv:
        return False
    if f.max_minimum is not None and _d(listing.minimum_investment) > f.max_minimum:
        return False
    if f.min_remaining is not None and remaining_amount(listing, committed) < f.min_remaining:
        return False
    if f.personal_guarantee and not listing.personal_guarantee:
        return False
    return True


_SORTERS = {
    "newest": lambda lst, c: -(_aware(lst.published_at or lst.created_at)).timestamp(),
    "rate": lambda lst, c: -float(_d(lst.note_rate_pct)),
    "term": lambda lst, c: lst.term_months or 10**6,
    "ltv": lambda lst, c: float(_d(lst.ltv_pct)),
    "remaining": lambda lst, c: -float(remaining_amount(lst, c)),
}


async def market(db: AsyncSession, f: MarketFilters) -> dict[str, Any]:
    """Everything the marketplace page needs in one answer: the matching
    cards, facet counts (each counted with its own facet skipped, so a
    selection never zeroes its siblings), the vertical counts and the stats."""
    rows = (
        await db.execute(
            select(CpListing)
            .where(CpListing.status.in_(sorted(E.MARKET_STATUSES)))
            .order_by(CpListing.published_at.desc().nullslast())
        )
    ).scalars().all()
    committed = await committed_amounts(db, [r.id for r in rows])
    base = [r for r in rows if f.vertical == "all" or r.vertical == f.vertical]
    now = now_utc()

    def facet(key: str, attr, values: list[str], labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
        out = []
        for v in values:
            count = sum(1 for r in base if _passes(r, committed.get(r.id, ZERO), f, skip=key) and attr(r) == v)
            out.append({"value": v, "label": (labels or {}).get(v, v), "count": count})
        return out

    status_labels = {"open": "Open", "closing": "Closing", "fully_subscribed": "Fully subscribed"}
    freq_labels = {"monthly": "Monthly", "quarterly": "Quarterly", "at_maturity": "At maturity"}
    programs = sorted({r.program_label for r in rows if r.program_label})
    collaterals = sorted({r.collateral_type for r in rows if r.collateral_type})
    states = sorted({r.state for r in rows if r.state})
    facets = {
        "status": facet("status", lambda r: r.status, ["open", "closing", "fully_subscribed"], status_labels),
        "program": facet("program", lambda r: r.program_label, programs),
        "collateral": facet("collateral", lambda r: r.collateral_type, collaterals),
        "frequency": facet("frequency", lambda r: r.payment_frequency, ["monthly", "quarterly", "at_maturity"], freq_labels),
        "state": [{"value": s, "label": s, "count": sum(1 for r in base if r.state == s)} for s in states],
    }
    matched = [r for r in base if _passes(r, committed.get(r.id, ZERO), f)]
    matched.sort(key=lambda r: _SORTERS.get(f.sort, _SORTERS["newest"])(r, committed.get(r.id, ZERO)))

    open_rows = [r for r in base if r.status in E.SUBSCRIBABLE_STATUSES]
    available = sum((remaining_amount(r, committed.get(r.id, ZERO)) for r in open_rows), ZERO)
    weighted = None
    if available > 0:
        weighted = sum(
            (_d(r.note_rate_pct) * remaining_amount(r, committed.get(r.id, ZERO)) for r in open_rows), ZERO
        ) / available
        weighted = weighted.quantize(Decimal("0.01"))
    avg_ltv = None
    if open_rows:
        avg_ltv = (sum((_d(r.ltv_pct) for r in open_rows), ZERO) / len(open_rows)).quantize(Decimal("0.1"))
    return {
        "items": [card(r, committed.get(r.id, ZERO), now=now) for r in matched],
        "total": len(matched),
        "stats": {
            "open_count": len(open_rows),
            "available_amount": available,
            "weighted_note_rate_pct": weighted,
            "average_ltv_pct": avg_ltv,
        },
        "facets": facets,
        "vertical_counts": {
            "all": len(rows),
            **{v: sum(1 for r in rows if r.vertical == v) for v in ("real_estate", "auto", "business")},
        },
        "as_of": now,
    }


async def load_listing(db: AsyncSession, *, ref: str | None = None, listing_id: uuid.UUID | None = None) -> CpListing | None:
    stmt = select(CpListing).options(selectinload(CpListing.documents))
    stmt = stmt.where(CpListing.ref == ref) if ref else stmt.where(CpListing.id == listing_id)
    return (await db.execute(stmt)).scalar_one_or_none()


def hold_months(funded_at: datetime | None, repaid_at: datetime | None) -> int | None:
    if not funded_at or not repaid_at:
        return None
    months = (repaid_at.year - funded_at.year) * 12 + (repaid_at.month - funded_at.month)
    if repaid_at.day < funded_at.day:
        months -= 1
    return max(0, months)


async def sold(db: AsyncSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(CpListing)
            .where(CpListing.status.in_(sorted(E.SOLD_STATUSES)))
            .order_by(CpListing.funded_at.desc().nullslast(), CpListing.published_at.desc().nullslast())
        )
    ).scalars().all()
    out_rows = []
    holds = []
    for r in rows:
        h = hold_months(r.funded_at, r.repaid_at)
        if h is not None:
            holds.append(h)
        out_rows.append(
            {
                "id": r.id,
                "ref": r.ref,
                "name": r.name,
                "vertical": r.vertical,
                "vertical_label": E.VERTICAL_LABELS.get(r.vertical, r.vertical),
                "program_label": r.program_label,
                "place": ", ".join(p for p in (r.city, r.state) if p) or None,
                "offered_amount": r.offered_amount,
                "note_rate_pct": r.note_rate_pct,
                "term_months": r.term_months,
                "funded_at": r.funded_at,
                "repaid_at": r.repaid_at,
                "hold_months": h,
                "status": r.status,
                "outcome": r.outcome or ("performing" if r.status in {"fully_subscribed", "funded"} else None),
            }
        )
    repaid = [r for r in rows if r.status == E.ListingStatus.REPAID]
    return {
        "stats": {
            "count": len(rows),
            "capital_placed": sum((_d(r.offered_amount) for r in rows), ZERO),
            "repaid_count": len(repaid),
            "average_hold_months": int(round(sum(holds) / len(holds))) if holds else None,
        },
        "rows": out_rows,
    }


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def create_subscription(
    db: AsyncSession,
    *,
    listing: CpListing,
    member: CpMember,
    entity_id: uuid.UUID | None,
    amount: Decimal,
    ack_ppm: bool,
    ack_accredited: bool,
    ip_address: str | None,
    user_agent: str | None,
) -> CpSubscription:
    if listing.status not in E.SUBSCRIBABLE_STATUSES:
        raise RuleError("not_open", "This offering is not open to subscribe.")
    if not (ack_ppm and ack_accredited):
        raise RuleError("acknowledgements_required", "Both acknowledgements are required.")
    if entity_id is not None and not any(e.id == entity_id for e in member.entities):
        raise RuleError("entity_not_yours", "That investing entity does not belong to this membership.")
    committed = (await committed_amounts(db, [listing.id])).get(listing.id, ZERO)
    remaining = remaining_amount(listing, committed)
    minimum = _d(listing.minimum_investment)
    if amount < minimum:
        raise RuleError("below_minimum", f"The minimum subscription is {minimum:,.2f}.")
    if amount > remaining:
        raise RuleError("exceeds_remaining", f"Only {remaining:,.2f} remains available.")
    existing = (
        await db.execute(
            select(CpSubscription).where(
                CpSubscription.listing_id == listing.id,
                CpSubscription.member_id == member.id,
                CpSubscription.entity_id.is_(None) if entity_id is None else CpSubscription.entity_id == entity_id,
                CpSubscription.status.in_(sorted(E.COMMITTED_STATUSES)),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise RuleError("already_subscribed", "This entity already holds an allocation in this offering.")
    now = now_utc()
    row = CpSubscription(
        listing_id=listing.id,
        member_id=member.id,
        entity_id=entity_id,
        amount=amount,
        status=E.SubscriptionStatus.REQUESTED,
        ack_ppm=True,
        ack_accredited=True,
        acknowledged_at=now,
        requested_at=now,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(row)
    await db.flush()
    await log_event(db, listing.id, "subscription.requested", actor_user_id=member.user_id, detail={"subscription_id": str(row.id), "amount": str(amount)})
    if remaining - amount <= 0 and listing.status != E.ListingStatus.FULLY_SUBSCRIBED:
        listing.status = E.ListingStatus.FULLY_SUBSCRIBED
        await log_event(db, listing.id, "status.fully_subscribed", actor_user_id=None, detail={"auto": True})
    await db.flush()
    return row


_SUB_TRANSITIONS: dict[str, set[str]] = {
    "send_documents": {E.SubscriptionStatus.REQUESTED},
    "mark_signed": {E.SubscriptionStatus.REQUESTED, E.SubscriptionStatus.DOCUMENTS_SENT},
    "mark_funded": {E.SubscriptionStatus.SIGNED},
    "cancel": {E.SubscriptionStatus.REQUESTED, E.SubscriptionStatus.DOCUMENTS_SENT, E.SubscriptionStatus.SIGNED},
    "decline": {E.SubscriptionStatus.REQUESTED, E.SubscriptionStatus.DOCUMENTS_SENT},
}


def _check_sub(action: str, sub: CpSubscription) -> None:
    if sub.status not in _SUB_TRANSITIONS[action]:
        raise RuleError("bad_transition", f"Cannot {action.replace('_', ' ')} a subscription that is {sub.status}.")


async def send_documents(db: AsyncSession, sub: CpSubscription, *, actor_user_id: uuid.UUID, notes: str | None) -> None:
    _check_sub("send_documents", sub)
    sub.status = E.SubscriptionStatus.DOCUMENTS_SENT
    sub.documents_sent_at = now_utc()
    sub.acted_by_id = actor_user_id
    if notes:
        sub.admin_notes = notes
    await log_event(db, sub.listing_id, "subscription.documents_sent", actor_user_id=actor_user_id, detail={"subscription_id": str(sub.id)})


async def mark_signed(
    db: AsyncSession, sub: CpSubscription, listing: CpListing, member: CpMember, *, actor_user_id: uuid.UUID, signed_at: datetime | None, notes: str | None
) -> list:
    _check_sub("mark_signed", sub)
    when = signed_at or now_utc()
    sub.status = E.SubscriptionStatus.SIGNED
    sub.signed_at = when
    sub.acted_by_id = actor_user_id
    if notes:
        sub.admin_notes = notes
    created = await filing_svc.on_binding_subscription(db, listing=listing, member=member, signed_at=when, actor_user_id=actor_user_id)
    await log_event(
        db, sub.listing_id, "subscription.signed", actor_user_id=actor_user_id,
        detail={"subscription_id": str(sub.id), "filings_created": [f.kind for f in created]},
    )
    return created


async def mark_funded(
    db: AsyncSession, sub: CpSubscription, listing: CpListing, *, actor_user_id: uuid.UUID, funded_at: datetime | None, funded_amount: Decimal | None, notes: str | None
) -> CpParticipation:
    _check_sub("mark_funded", sub)
    when = funded_at or now_utc()
    amount = funded_amount if funded_amount is not None else _d(sub.amount)
    sub.status = E.SubscriptionStatus.FUNDED
    sub.funded_at = when
    sub.funded_amount = amount
    sub.acted_by_id = actor_user_id
    if notes:
        sub.admin_notes = notes
    part = CpParticipation(
        subscription_id=sub.id,
        listing_id=sub.listing_id,
        member_id=sub.member_id,
        entity_id=sub.entity_id,
        invested_amount=amount,
        principal_outstanding=amount,
        servicer_status=E.ServicerStatus.CURRENT,
        status_as_of=when.date(),
        funded_at=when,
        updated_by_id=actor_user_id,
    )
    db.add(part)
    await db.flush()
    await log_event(db, sub.listing_id, "subscription.funded", actor_user_id=actor_user_id, detail={"subscription_id": str(sub.id), "amount": str(amount)})
    return part


async def cancel_subscription(db: AsyncSession, sub: CpSubscription, listing: CpListing, *, actor_user_id: uuid.UUID | None, reason: str | None, declined: bool = False) -> None:
    _check_sub("decline" if declined else "cancel", sub)
    now = now_utc()
    if declined:
        sub.status = E.SubscriptionStatus.DECLINED
        sub.declined_at = now
    else:
        sub.status = E.SubscriptionStatus.CANCELLED
        sub.cancelled_at = now
    sub.cancel_reason = reason
    sub.acted_by_id = actor_user_id
    # An allocation freed on a fully subscribed listing reopens it.
    if listing.status == E.ListingStatus.FULLY_SUBSCRIBED and listing.funded_at is None:
        listing.status = E.ListingStatus.OPEN
    await log_event(db, sub.listing_id, "subscription.declined" if declined else "subscription.cancelled", actor_user_id=actor_user_id, detail={"subscription_id": str(sub.id), "reason": reason})


# ---------------------------------------------------------------------------
# Listing lifecycle
# ---------------------------------------------------------------------------


async def publish(db: AsyncSession, listing: CpListing, *, actor_user_id: uuid.UUID, reason: str) -> None:
    if listing.status != E.ListingStatus.DRAFT:
        raise RuleError("not_draft", "Only a draft can be published.")
    blockers = publish_blockers(listing)
    if blockers:
        raise RuleError("incomplete", "Fill in: " + "; ".join(blockers))
    if not listing.ref:
        listing.ref = await ref_svc.next_listing_ref(db, listing.vertical)
    now = now_utc()
    listing.status = E.ListingStatus.OPEN
    listing.published_at = now
    listing.published_by_id = actor_user_id
    listing.publish_reason = reason
    listing.opens_at = listing.opens_at or now
    await log_event(db, listing.id, "listing.published", actor_user_id=actor_user_id, detail={"reason": reason})


_LISTING_TRANSITIONS: dict[str, set[str]] = {
    E.ListingStatus.OPEN: {E.ListingStatus.CLOSING, E.ListingStatus.FULLY_SUBSCRIBED, E.ListingStatus.FUNDED},
    E.ListingStatus.CLOSING: {E.ListingStatus.OPEN, E.ListingStatus.FULLY_SUBSCRIBED, E.ListingStatus.FUNDED},
    E.ListingStatus.FULLY_SUBSCRIBED: {E.ListingStatus.OPEN, E.ListingStatus.FUNDED},
    E.ListingStatus.FUNDED: {E.ListingStatus.REPAID, E.ListingStatus.WORKOUT},
    E.ListingStatus.WORKOUT: {E.ListingStatus.REPAID, E.ListingStatus.FUNDED},
    E.ListingStatus.REPAID: set(),
}


async def transition(
    db: AsyncSession, listing: CpListing, *, to: str, actor_user_id: uuid.UUID, reason: str | None, outcome: str | None, effective_at: datetime | None
) -> None:
    allowed = _LISTING_TRANSITIONS.get(listing.status, set())
    if to not in allowed:
        raise RuleError("bad_transition", f"A listing that is {listing.status} cannot move to {to}.")
    when = effective_at or now_utc()
    listing.status = to
    if to == E.ListingStatus.FUNDED:
        listing.funded_at = listing.funded_at or when
        listing.outcome = outcome or E.ListingOutcome.PERFORMING
    elif to == E.ListingStatus.REPAID:
        listing.repaid_at = when
        listing.outcome = outcome or E.ListingOutcome.REPAID_IN_FULL
    elif to == E.ListingStatus.WORKOUT:
        listing.outcome = E.ListingOutcome.IN_WORKOUT
    elif outcome:
        listing.outcome = outcome
    if to == E.ListingStatus.OPEN and listing.closes_at and _aware(listing.closes_at) < when:
        listing.closes_at = None
    await log_event(db, listing.id, f"status.{to}", actor_user_id=actor_user_id, detail={"reason": reason, "outcome": listing.outcome})


async def withdraw(db: AsyncSession, listing: CpListing, *, actor_user_id: uuid.UUID, reason: str) -> None:
    if listing.status in {E.ListingStatus.FUNDED, E.ListingStatus.REPAID, E.ListingStatus.WORKOUT}:
        raise RuleError("bad_transition", "A funded listing cannot be withdrawn.")
    open_subs = (
        await db.execute(
            select(func.count()).select_from(CpSubscription).where(
                CpSubscription.listing_id == listing.id, CpSubscription.status.in_(sorted(E.SOLD_SUBSCRIPTION_STATUSES))
            )
        )
    ).scalar_one()
    if open_subs:
        raise RuleError("has_binding_subscriptions", "Cancel or fund the signed subscriptions before withdrawing.")
    listing.status = E.ListingStatus.WITHDRAWN
    listing.withdrawn_at = now_utc()
    listing.withdrawn_reason = reason
    await log_event(db, listing.id, "listing.withdrawn", actor_user_id=actor_user_id, detail={"reason": reason})


# ---------------------------------------------------------------------------
# Portfolio and transfers
# ---------------------------------------------------------------------------


def remaining_term_months(listing: CpListing, today: date | None = None) -> int | None:
    if listing.maturity_date is None:
        if listing.funded_at and listing.term_months:
            end_year = listing.funded_at.year + (listing.funded_at.month - 1 + listing.term_months) // 12
            end_month = (listing.funded_at.month - 1 + listing.term_months) % 12 + 1
            maturity = date(end_year, end_month, min(listing.funded_at.day, 28))
        else:
            return None
    else:
        maturity = listing.maturity_date
    today = today or now_utc().date()
    months = (maturity.year - today.year) * 12 + (maturity.month - today.month)
    return max(0, months)


def implied_yield(note_rate_pct: Any, ask_pct: Any) -> Decimal | None:
    rate = _d(note_rate_pct)
    ask = _d(ask_pct)
    if ask <= 0:
        return None
    return (rate * Decimal(100) / ask).quantize(Decimal("0.01"))


async def active_transfer_for(db: AsyncSession, participation_id: uuid.UUID) -> CpTransfer | None:
    return (
        await db.execute(
            select(CpTransfer).where(
                CpTransfer.participation_id == participation_id,
                CpTransfer.status.in_(["listed", "requested", "pending_consent", "approved"]),
            )
        )
    ).scalar_one_or_none()


async def list_participation(
    db: AsyncSession, *, participation: CpParticipation, member: CpMember, principal_amount: Decimal, ask_pct: Decimal
) -> CpTransfer:
    if participation.member_id != member.id:
        raise RuleError("not_yours", "You can only list a participation you hold.")
    if _d(participation.principal_outstanding) <= 0:
        raise RuleError("nothing_outstanding", "This participation has no principal outstanding.")
    if principal_amount > _d(participation.principal_outstanding):
        raise RuleError("exceeds_outstanding", "You cannot list more than the principal outstanding.")
    if await active_transfer_for(db, participation.id) is not None:
        raise RuleError("already_listed", "This participation is already listed for transfer.")
    row = CpTransfer(
        ref=await ref_svc.next_transfer_ref(db),
        participation_id=participation.id,
        listing_id=participation.listing_id,
        seller_member_id=member.id,
        principal_amount=principal_amount,
        ask_pct=ask_pct,
        status=E.TransferStatus.LISTED,
        listed_at=now_utc(),
    )
    db.add(row)
    await db.flush()
    await log_event(db, participation.listing_id, "transfer.listed", actor_user_id=member.user_id, detail={"transfer_id": str(row.id), "ref": row.ref})
    return row


async def request_transfer(db: AsyncSession, *, transfer: CpTransfer, buyer: CpMember) -> None:
    if transfer.status != E.TransferStatus.LISTED:
        raise RuleError("not_available", "This participation is no longer available.")
    if transfer.seller_member_id == buyer.id:
        raise RuleError("own_listing", "You listed this participation.")
    transfer.status = E.TransferStatus.PENDING_CONSENT
    transfer.buyer_member_id = buyer.id
    transfer.requested_at = now_utc()
    await log_event(db, transfer.listing_id, "transfer.requested", actor_user_id=buyer.user_id, detail={"transfer_id": str(transfer.id)})


async def withdraw_transfer(db: AsyncSession, *, transfer: CpTransfer, member: CpMember) -> None:
    if transfer.seller_member_id != member.id:
        raise RuleError("not_yours", "Only the seller can withdraw a listing.")
    if transfer.status not in {E.TransferStatus.LISTED, E.TransferStatus.REQUESTED, E.TransferStatus.PENDING_CONSENT}:
        raise RuleError("bad_transition", "This transfer can no longer be withdrawn.")
    transfer.status = E.TransferStatus.WITHDRAWN
    transfer.withdrawn_at = now_utc()
    await log_event(db, transfer.listing_id, "transfer.withdrawn", actor_user_id=member.user_id, detail={"transfer_id": str(transfer.id)})


async def decide_transfer(db: AsyncSession, *, transfer: CpTransfer, decision: str, actor_user_id: uuid.UUID, notes: str | None) -> CpParticipation | None:
    """consent | decline | complete. Completing moves the principal: the
    buyer gets a participation, the seller's outstanding shrinks or closes."""
    now = now_utc()
    if notes:
        transfer.notes = notes
    if decision == "consent":
        if transfer.status != E.TransferStatus.PENDING_CONSENT:
            raise RuleError("bad_transition", "Only a requested transfer can be consented to.")
        transfer.status = E.TransferStatus.APPROVED
        transfer.consented_at = now
        transfer.consented_by_id = actor_user_id
        await log_event(db, transfer.listing_id, "transfer.consented", actor_user_id=actor_user_id, detail={"transfer_id": str(transfer.id)})
        return None
    if decision == "decline":
        if transfer.status not in {E.TransferStatus.PENDING_CONSENT, E.TransferStatus.APPROVED, E.TransferStatus.LISTED}:
            raise RuleError("bad_transition", "This transfer cannot be declined now.")
        transfer.status = E.TransferStatus.DECLINED
        transfer.declined_at = now
        transfer.consented_by_id = actor_user_id
        await log_event(db, transfer.listing_id, "transfer.declined", actor_user_id=actor_user_id, detail={"transfer_id": str(transfer.id)})
        return None
    if decision == "complete":
        if transfer.status != E.TransferStatus.APPROVED or transfer.buyer_member_id is None:
            raise RuleError("bad_transition", "Consent first; a transfer completes after consent and settlement.")
        seller_part = await db.get(CpParticipation, transfer.participation_id)
        if seller_part is None:
            raise RuleError("missing_participation", "The seller's participation no longer exists.")
        amount = _d(transfer.principal_amount)
        if amount > _d(seller_part.principal_outstanding):
            raise RuleError("exceeds_outstanding", "The seller no longer holds that much principal.")
        buyer_default = (
            await db.execute(
                select(CpMemberEntity).where(CpMemberEntity.member_id == transfer.buyer_member_id, CpMemberEntity.is_default.is_(True)).limit(1)
            )
        ).scalar_one_or_none()
        # The buyer's holding is recorded as a funded subscription so every
        # participation keeps one provenance row, and Form D counts stay true.
        sub = CpSubscription(
            listing_id=transfer.listing_id,
            member_id=transfer.buyer_member_id,
            entity_id=buyer_default.id if buyer_default else None,
            amount=amount,
            status=E.SubscriptionStatus.FUNDED,
            ack_ppm=True,
            ack_accredited=True,
            acknowledged_at=now,
            requested_at=now,
            signed_at=now,
            funded_at=now,
            funded_amount=amount,
            admin_notes=f"Secondary transfer {transfer.ref} from participation {seller_part.id}",
            acted_by_id=actor_user_id,
        )
        db.add(sub)
        await db.flush()
        buyer_part = CpParticipation(
            subscription_id=sub.id,
            listing_id=transfer.listing_id,
            member_id=transfer.buyer_member_id,
            entity_id=sub.entity_id,
            invested_amount=amount * _d(transfer.ask_pct) / Decimal(100),
            principal_outstanding=amount,
            servicer_status=seller_part.servicer_status,
            status_as_of=seller_part.status_as_of,
            next_payment_on=seller_part.next_payment_on,
            funded_at=now,
            updated_by_id=actor_user_id,
        )
        db.add(buyer_part)
        seller_part.principal_outstanding = _d(seller_part.principal_outstanding) - amount
        if seller_part.principal_outstanding <= 0:
            seller_part.servicer_status = E.ServicerStatus.PAID_IN_FULL
            seller_part.closed_at = now
        seller_part.updated_by_id = actor_user_id
        transfer.status = E.TransferStatus.COMPLETED
        transfer.completed_at = now
        await db.flush()
        await log_event(db, transfer.listing_id, "transfer.completed", actor_user_id=actor_user_id, detail={"transfer_id": str(transfer.id), "amount": str(amount)})
        return buyer_part
    raise RuleError("bad_decision", f"Unknown decision {decision!r}.")
