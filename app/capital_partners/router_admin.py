"""The desk's side: /capital/admin/* (super admin only).

Access requests → invite; members → review; listings → publish and run;
subscriptions → documents, signed, funded; participations → servicer status;
transfers → consent; filings → the Regulation D tracker; issuer settings.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.models.user import User
from app.schemas.investor_returns import InvestorReturnsQuery, InvestorReturnsRead
from app.services import clerk as clerk_service
from app.services.payment_authorization import client_ip
from app.services.user_access import console_keys

from . import enums as E
from . import schemas as S
from .deps import CpAdmin
from .models import (
    CpAccessRequest,
    CpDocumentAccess,
    CpFiling,
    CpListing,
    CpListingDocument,
    CpListingDocumentGrant,
    CpListingEvent,
    CpMember,
    CpParticipation,
    CpQuestionnaire,
    CpSubscription,
    CpTransfer,
)
from .services import emails, storage
from .services import filings as F
from .services import listings as L
from .services import members as M
from .services import prefill as P
from .services import refs as R
from .services import sync as SYNC

router = APIRouter(prefix="/capital/admin", tags=["capital-admin"])
Db = Annotated[AsyncSession, Depends(get_db)]


def _rule(exc: L.RuleError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.message})


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Access requests
# ---------------------------------------------------------------------------


@router.get("/access-requests", response_model=list[S.AccessRequestOut])
async def list_access_requests(admin: CpAdmin, db: Db, status_filter: str | None = Query(None)) -> list[S.AccessRequestOut]:
    stmt = select(CpAccessRequest).order_by(CpAccessRequest.created_at.desc())
    if status_filter:
        stmt = stmt.where(CpAccessRequest.status == status_filter)
    rows = (await db.execute(stmt.limit(500))).scalars().all()
    return [S.AccessRequestOut.model_validate(r) for r in rows]


@router.get("/membership-policy", response_model=S.MembershipPolicyOut)
async def get_membership_policy(admin: CpAdmin, db: Db) -> S.MembershipPolicyOut:
    from .models import CpIssuerSettings
    from .services import membership_policy
    settings = (await db.execute(select(CpIssuerSettings).where(CpIssuerSettings.key == "default"))).scalar_one_or_none()
    return S.MembershipPolicyOut(**membership_policy.describe(settings))


@router.get("/access-requests/{request_id}", response_model=S.AccessRequestOut)
async def get_access_request(request_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.AccessRequestOut:
    row = await db.get(CpAccessRequest, request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found.")
    return S.AccessRequestOut.model_validate(row)


@router.post("/access-requests/{request_id}/contacted", response_model=S.AccessRequestOut)
async def mark_contacted(request_id: uuid.UUID, payload: S.AccessRequestActionIn, admin: CpAdmin, db: Db) -> S.AccessRequestOut:
    row = await db.get(CpAccessRequest, request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found.")
    if row.status == E.AccessRequestStatus.NEW:
        row.status = E.AccessRequestStatus.CONTACTED
    row.handled_by_id, row.handled_at, row.handled_note = admin.id, _now(), payload.note
    await db.flush()
    return S.AccessRequestOut.model_validate(row)


@router.post("/access-requests/{request_id}/decline", response_model=S.AccessRequestOut)
async def decline_access_request(request_id: uuid.UUID, payload: S.AccessRequestActionIn, admin: CpAdmin, db: Db) -> S.AccessRequestOut:
    row = await db.get(CpAccessRequest, request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found.")
    row.status = E.AccessRequestStatus.DECLINED
    row.handled_by_id, row.handled_at, row.handled_note = admin.id, _now(), payload.note
    await db.flush()
    return S.AccessRequestOut.model_validate(row)


async def _invite(db: AsyncSession, admin: User, *, email: str, name: str, access_request: CpAccessRequest | None) -> S.InviteResult:
    email = email.strip().lower()
    existing = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if existing is not None and existing.role != Role.CAPITAL_PARTNER:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "email_in_use", "message": f"{email} already has a {existing.role} login. Capital partners need their own address."},
        )
    user = existing
    created = False
    if user is None:
        user = User(email=email, name=name.strip() or email.split("@")[0], role=Role.CAPITAL_PARTNER)
        db.add(user)
        await db.flush()
        created = True
    member = await M.ensure_member_for_user(db, user, display_name=user.name, access_request=access_request, invited_by_id=admin.id)
    settings = get_settings()
    invitation = None
    if created or user.clerk_id is None:
        invitation = await clerk_service.invite_user(
            email=email,
            name=user.name,
            role=Role.CAPITAL_PARTNER,
            redirect_url=f"{settings.capital_app_url.rstrip('/')}/sign-in",
            account_types=console_keys(user),
            account_status="active",
        )
        if hasattr(user, "last_invited_at"):
            user.last_invited_at = _now()
            user.last_invite_status = "sent" if invitation is not None else "failed"
    if access_request is not None:
        access_request.status = E.AccessRequestStatus.INVITED
        access_request.invited_user_id = user.id
        access_request.handled_by_id, access_request.handled_at = admin.id, _now()
    await db.flush()
    await emails.member_invited(db, to=email, name=user.name)
    sent = invitation is not None
    message = (
        "Invitation sent. The member signs up on the portal from the Clerk email."
        if sent
        else ("This address already has a portal login." if not created and user.clerk_id else "Member row created; the Clerk invitation did not go out (check CLERK_SECRET_KEY).")
    )
    return S.InviteResult(ok=True, user_id=user.id, member_id=member.id, invitation_sent=sent, message=message)


@router.post("/access-requests/{request_id}/invite", response_model=S.InviteResult)
async def invite_from_request(request_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.InviteResult:
    row = await db.get(CpAccessRequest, request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found.")
    return await _invite(db, admin, email=row.email, name=row.full_name, access_request=row)


class _DirectInvite(S.AccessRequestActionIn):
    email: str
    name: str


@router.post("/members/invite", response_model=S.InviteResult)
async def invite_direct(payload: _DirectInvite, admin: CpAdmin, db: Db) -> S.InviteResult:
    if "@" not in payload.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a valid email address.")
    return await _invite(db, admin, email=payload.email, name=payload.name, access_request=None)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def _member_out(db: AsyncSession, member: CpMember, user: User) -> S.MemberAdminOut:
    M.refresh_status(member)
    await db.flush()
    await db.refresh(member, attribute_names=["created_at", "updated_at"])
    latest = await db.get(CpQuestionnaire, member.last_questionnaire_id) if member.last_questionnaire_id else None
    subs = (await db.execute(select(func.count()).select_from(CpSubscription).where(CpSubscription.member_id == member.id))).scalar_one()
    parts = (await db.execute(select(func.count()).select_from(CpParticipation).where(CpParticipation.member_id == member.id))).scalar_one()
    return S.MemberAdminOut(
        id=member.id, user_id=user.id, email=user.email, name=user.name, status=member.status, investing_as=member.investing_as,
        display_name=member.display_name, phone=member.phone, state_of_residence=member.state_of_residence,
        capital_band=member.capital_band, source=member.source, relationship_established_at=member.relationship_established_at,
        waiting_period_days=member.waiting_period_days, waiting_period_ends_at=member.waiting_period_ends_at,
        approved_at=member.approved_at, accreditation_basis=member.accreditation_basis, review_notes=member.review_notes,
        suspended_at=member.suspended_at, suspended_reason=member.suspended_reason, invited_at=member.invited_at,
        created_at=member.created_at, entities=[S.EntityOut.model_validate(e) for e in member.entities],
        latest_questionnaire=S.QuestionnaireOut.model_validate(latest) if latest else None,
        subscriptions_count=int(subs), participations_count=int(parts), gate=S.GateOut(**M.gate_state(member)),
    )


@router.get("/members", response_model=list[S.MemberAdminOut])
async def list_members(admin: CpAdmin, db: Db, status_filter: str | None = Query(None), q: str | None = Query(None)) -> list[S.MemberAdminOut]:
    stmt = select(CpMember, User).join(User, User.id == CpMember.user_id).options(selectinload(CpMember.entities)).order_by(CpMember.created_at.desc())
    if status_filter:
        stmt = stmt.where(CpMember.status == status_filter)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(User.email).like(needle) | func.lower(User.name).like(needle) | func.lower(func.coalesce(CpMember.display_name, "")).like(needle))
    rows = (await db.execute(stmt.limit(500))).all()
    return [await _member_out(db, m, u) for m, u in rows]


@router.get("/members/{member_id}", response_model=S.MemberAdminOut)
async def get_member(member_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.MemberAdminOut:
    row = (
        await db.execute(select(CpMember, User).join(User, User.id == CpMember.user_id).options(selectinload(CpMember.entities)).where(CpMember.id == member_id))
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
    return await _member_out(db, row[0], row[1])


@router.post("/members/{member_id}/review", response_model=S.MemberAdminOut)
async def review_member(member_id: uuid.UUID, payload: S.MemberReviewIn, admin: CpAdmin, db: Db) -> S.MemberAdminOut:
    row = (
        await db.execute(select(CpMember, User).join(User, User.id == CpMember.user_id).options(selectinload(CpMember.entities)).where(CpMember.id == member_id))
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found.")
    member, user = row
    try:
        await M.review_member(
            db, member, decision=payload.decision, reviewer_id=admin.id, notes=payload.notes,
            relationship_established_at=payload.relationship_established_at, waiting_period_days=payload.waiting_period_days,
            accreditation_basis=payload.accreditation_basis,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if payload.decision == "approve":
        ends = member.waiting_period_ends_at
        await emails.member_approved(db, to=user.email, name=member.display_name or user.name, waiting_ends=ends.strftime("%b %d, %Y") if ends and M.aware(ends) > _now() else None)
    elif payload.decision == "needs_changes":
        await emails.member_needs_changes(db, to=user.email, name=member.display_name or user.name, notes=payload.notes)
    return await _member_out(db, member, user)


# ---------------------------------------------------------------------------
# Sources, prefill, listings
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=list[S.SourceRow])
async def sources(admin: CpAdmin, db: Db, q: str | None = Query(None), vertical: str = Query("all"), include_all: bool = Query(False)) -> list[S.SourceRow]:
    rows = await P.sources(db, admin, q=q, vertical=vertical, include_all=include_all)
    return [S.SourceRow(**r) for r in rows]


@router.get("/sources/lookup", response_model=S.SourceLookup)
async def source_lookup(admin: CpAdmin, db: Db, profile_id: Annotated[uuid.UUID | None, Query()] = None, loan_id: Annotated[uuid.UUID | None, Query()] = None) -> S.SourceLookup:
    """The file page asks before offering "Publish to marketplace": is there a
    listing already, and are loan terms on record."""
    if not profile_id and not loan_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Pass profile_id or loan_id.")
    return S.SourceLookup(**await P.lookup(db, loan_id=loan_id, profile_id=profile_id))


@router.get("/sources/suggest-terms", response_model=S.OfferSuggestions)
async def suggest_terms(admin: CpAdmin, db: Db, profile_id: Annotated[uuid.UUID, Query()], program_keys: Annotated[str | None, Query()] = None) -> S.OfferSuggestions:
    """Suggested products and terms for an unfunded file, from the cockpit's
    income figure, today's debt service and each program's published terms."""
    from app.models.application_profile import ApplicationProfile
    from app.services import offer_suggestions

    profile = await db.get(ApplicationProfile, profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application profile not found.")
    keys = [key.strip() for key in (program_keys or "").split(",") if key.strip()] or None
    return S.OfferSuggestions(**await offer_suggestions.suggest_offers(db, profile, program_keys=keys))


@router.post("/listings/prefill")
async def prefill_listing(payload: S.PrefillIn, admin: CpAdmin, db: Db) -> dict[str, Any]:
    loan_id, profile_id = payload.loan_id, payload.profile_id
    if not loan_id and not profile_id and payload.intake_id:
        try:
            profile_id = await P.resolve_intake_profile(db, admin, payload.intake_id)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        await db.commit()
    if not loan_id and not profile_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Pass loan_id, profile_id or intake_id.")
    try:
        draft = await P.prefill(db, admin, loan_id=loan_id, profile_id=profile_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    out = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in draft.items()}
    # The new-listing page redirects to this instead of cutting a duplicate.
    out["existing_listing"] = await P.existing_listing(db, loan_id=loan_id, profile_id=profile_id)
    return out


@router.post("/listings/ai-draft", response_model=S.ListingAIDraftOut)
async def draft_listing(payload: S.ListingAIDraftIn, admin: CpAdmin, db: Db) -> S.ListingAIDraftOut:
    """Reviewable copy and known capital figures; no listing is published."""
    if not payload.profile_id and not payload.loan_id and not payload.intake_id:
        raise HTTPException(422, "Pass profile_id, loan_id or intake_id.")
    from .services import listing_draft

    try:
        return S.ListingAIDraftOut(**await listing_draft.prepare(db, admin, payload))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


async def _listing_admin_out(db: AsyncSession, listing: CpListing) -> S.ListingAdminOut:
    # created_at / updated_at are database-stamped and expire on every write
    # (see TimestampMixin); load them before the serializer touches them.
    await db.refresh(listing, attribute_names=["created_at", "updated_at"])
    committed = (await L.committed_amounts(db, [listing.id])).get(listing.id, L.ZERO)
    sub_rows = (
        await db.execute(
            select(CpSubscription, CpMember, User).join(CpMember, CpMember.id == CpSubscription.member_id).join(User, User.id == CpMember.user_id)
            .options(selectinload(CpMember.entities)).where(CpSubscription.listing_id == listing.id).order_by(CpSubscription.requested_at.desc())
        )
    ).all()
    subs = []
    for sub, member, user in sub_rows:
        d = S.SubscriptionOut.model_validate(sub).model_dump()
        d.update(
            listing_ref=listing.ref, listing_name=listing.name, entity_name=next((e.name for e in member.entities if e.id == sub.entity_id), None),
            member_id=member.id, member_name=member.display_name or user.name, member_email=user.email, member_state=member.state_of_residence,
            ack_ppm=sub.ack_ppm, ack_accredited=sub.ack_accredited, acknowledged_at=sub.acknowledged_at, cancelled_at=sub.cancelled_at,
            cancel_reason=sub.cancel_reason, declined_at=sub.declined_at, admin_notes=sub.admin_notes,
        )
        subs.append(S.SubscriptionAdminOut(**d))
    part_rows = (
        await db.execute(
            select(CpParticipation, CpMember, User).join(CpMember, CpMember.id == CpParticipation.member_id).join(User, User.id == CpMember.user_id)
            .options(selectinload(CpMember.entities)).where(CpParticipation.listing_id == listing.id).order_by(CpParticipation.funded_at.desc().nullslast())
        )
    ).all()
    parts = []
    for part, member, user in part_rows:
        active = await L.active_transfer_for(db, part.id)
        parts.append(
            S.ParticipationAdminOut(
                id=part.id, listing_id=listing.id, listing_ref=listing.ref, listing_name=listing.name, vertical=listing.vertical,
                vertical_label=E.VERTICAL_LABELS.get(listing.vertical, listing.vertical), note_rate_pct=listing.note_rate_pct,
                entity_id=part.entity_id, entity_name=next((e.name for e in member.entities if e.id == part.entity_id), None),
                invested_amount=part.invested_amount, principal_outstanding=part.principal_outstanding,
                interest_received_ytd=part.interest_received_ytd, interest_received_total=part.interest_received_total,
                next_payment_on=part.next_payment_on, servicer_status=part.servicer_status, status_as_of=part.status_as_of,
                funded_at=part.funded_at, closed_at=part.closed_at, maturity_date=listing.maturity_date,
                transferable=L._d(part.principal_outstanding) > 0 and active is None, member_id=member.id,
                member_name=member.display_name or user.name, member_email=user.email, subscription_id=part.subscription_id,
            )
        )
    events = (await db.execute(select(CpListingEvent).where(CpListingEvent.listing_id == listing.id).order_by(CpListingEvent.occurred_at.desc()).limit(200))).scalars().all()
    for ev in events:
        await db.refresh(ev, attribute_names=["occurred_at"])
    investor_count = (await F.investor_counts(db)).get(listing.id, 0)
    sync = await SYNC.sync_for_listing(db, listing)
    from .services.ppm import bind_access_state
    await bind_access_state(db, listing, for_publication=True)
    publish_issues = L.publish_issues(listing) if listing.status in {E.ListingStatus.DRAFT, E.ListingStatus.OPEN} else []
    return S.ListingAdminOut(
        **L.card(listing, committed),
        sync=sync,
        summary=listing.summary, guarantee_text=listing.guarantee_text, borrower_track_record=listing.borrower_track_record,
        exit_plan=listing.exit_plan, valuation_amount=listing.valuation_amount, valuation_source=listing.valuation_source,
        title_status=listing.title_status, servicer_label=listing.servicer_label,
        documents=[S.DocumentOut(**L.document_out(d)) for d in listing.documents if d.status != E.DocumentStatus.SUPERSEDED],
        my_subscriptions=[], can_subscribe=False, source_kind=listing.source_kind, loan_id=listing.loan_id, profile_id=listing.profile_id,
        first_sale_at=listing.first_sale_at, published_by_id=listing.published_by_id, publish_reason=listing.publish_reason,
        withdrawn_at=listing.withdrawn_at, withdrawn_reason=listing.withdrawn_reason, source_snapshot=listing.source_snapshot or {},
        created_by_id=listing.created_by_id, created_at=listing.created_at, updated_at=listing.updated_at, investor_count=investor_count,
        subscriptions=subs, participations=parts, events=[S.ListingEventOut.model_validate(e) for e in events],
        publish_blockers=[issue["message"] for issue in publish_issues], publish_issues=publish_issues,
    )


async def _load_admin_listing(db: AsyncSession, listing_id: uuid.UUID) -> CpListing:
    listing = await L.load_listing(db, listing_id=listing_id)
    if listing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Listing not found.")
    return listing


@router.get("/listings", response_model=list[S.ListingCard])
async def list_listings(admin: CpAdmin, db: Db, status_filter: str | None = Query(None)) -> list[S.ListingCard]:
    stmt = select(CpListing).order_by(CpListing.created_at.desc())
    if status_filter:
        stmt = stmt.where(CpListing.status == status_filter)
    rows = (await db.execute(stmt.limit(500))).scalars().all()
    committed = await L.committed_amounts(db, [r.id for r in rows])
    return [S.ListingCard(**L.card(r, committed.get(r.id, L.ZERO))) for r in rows]


_LISTING_FIELDS = [
    "source_kind", "loan_id", "profile_id", "vertical", "program_label", "name", "summary", "city", "state", "collateral_type",
    "lien_position", "note_rate_pct", "term_months", "ltv_pct", "ltv_kind", "loan_amount", "offered_amount", "minimum_investment",
    "payment_frequency", "personal_guarantee", "guarantee_text", "borrower_track_record", "exit_plan", "valuation_amount",
    "valuation_source", "title_status", "servicer_label", "maturity_date", "closes_at", "source_snapshot", "products",
]


def _apply(listing: CpListing, payload: S.ListingIn) -> None:
    data = payload.model_dump(exclude_unset=True)
    for key in _LISTING_FIELDS:
        if key in data:
            value = data[key]
            if key == "state" and value:
                value = value.upper()
            if key == "products":
                # JSONB: decimals become plain numbers, and exactly one product is primary.
                products = [
                    {k: (float(v) if isinstance(v, Decimal) else v) for k, v in item.model_dump().items()}
                    for item in payload.products or []
                ]
                if products and not any(item.get("primary") for item in products):
                    products[0]["primary"] = True
                value = products
            setattr(listing, key, value)

    if listing.profile_id is None:
        from app.services.loan_dates import as_date, loan_dates
        snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
        previous_dates = snapshot.get("loan_dates") if isinstance(snapshot.get("loan_dates"), dict) else {}
        funding = data.get("expected_funding_date") if "expected_funding_date" in data else previous_dates.get("expected_funding_date")
        mode = data.get("maturity_date_mode") if "maturity_date_mode" in data else previous_dates.get("maturity_date_mode")
        if "maturity_date" in data and "maturity_date_mode" not in data and (not previous_dates.get("maturity_date_mode") or str(data["maturity_date"]) != str(previous_dates.get("maturity_date"))):
            mode = "manual" if data["maturity_date"] else "automatic"
        try:
            dates = loan_dates(funding, listing.term_months, listing.maturity_date, mode)
        except (ValueError, OverflowError) as exc:
            field = "maturity_date" if "maturity" in str(exc).lower() else "expected_funding_date"
            raise HTTPException(422, detail=[{"loc": ["body", field], "msg": str(exc), "type": "value_error"}]) from exc
        listing.source_snapshot = {**snapshot, "loan_dates": dates}
        listing.maturity_date = as_date(dates["maturity_date"])


async def _apply_file_financing(db: AsyncSession, listing: CpListing, *, prior: Any = None, payload: S.ListingIn | None = None) -> None:
    """Staff saves inherit the file's loan rather than creating another copy."""
    if listing.profile_id is None:
        return
    from app.models.application_profile import ApplicationProfile

    if isinstance(db, AsyncSession):
        profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == listing.profile_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    else:
        profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file behind this listing was not found.")
    fresh = await P.readiness_snapshot(db, profile)
    if payload is not None:
        from .services.listing_draft import source_fingerprint

        current_budget = ((fresh.get("financials") or {}).get("deal_budget") or {}).get("current") or {}
        if payload.expected_deal_budget_revision is not None and payload.expected_deal_budget_revision != int(current_budget.get("revision") or 0):
            raise HTTPException(409, "The cost budget changed after this draft was prepared. Reload and review the latest draft before applying it.")
        if payload.expected_source_fingerprint is not None and payload.expected_source_fingerprint != source_fingerprint(fresh):
            raise HTTPException(409, "The file or financing changed after this draft was prepared. Draft again before applying it.")
    await _guard_financing_change(db, prior or listing, fresh)
    SYNC.apply_canonical_financing(listing, fresh)
    listing.source_snapshot = {**(listing.source_snapshot or {}), **fresh}
    if payload is not None and payload.ai_draft_id:
        listing.source_snapshot = {**listing.source_snapshot, "ai_draft": {"id": payload.ai_draft_id,
            "source_fingerprint": payload.expected_source_fingerprint, "deal_budget_revision": payload.expected_deal_budget_revision}}


async def _guard_financing_change(db: AsyncSession, listing: CpListing, fresh: dict[str, Any]) -> None:
    if not SYNC.financing_changed(listing, fresh):
        return
    funded = listing.status in {E.ListingStatus.FUNDED, E.ListingStatus.REPAID, E.ListingStatus.WORKOUT}
    committed = (await L.committed_amounts(db, [listing.id])).get(listing.id, Decimal(0)) if listing.id else Decimal(0)
    if funded or committed > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "This listing has committed or funded capital. Financing and recorded collateral cannot be changed through a file refresh; review the existing commitments before issuing a revised offering.")


async def _guard_committed_listing_edit(db: AsyncSession, listing: CpListing, payload: S.ListingIn) -> None:
    protected = {"loan_id", "profile_id", "loan_amount", "offered_amount", "minimum_investment", "note_rate_pct", "term_months", "products",
        "collateral_type", "lien_position", "ltv_pct", "ltv_kind", "payment_frequency", "maturity_date", "personal_guarantee", "expected_funding_date", "maturity_date_mode"}
    values = payload.model_dump(exclude_unset=True)
    from app.services.loan_dates import listing_dates
    existing_dates = listing_dates(listing)
    def comparable(key, value):
        if key in {"expected_funding_date", "maturity_date"}:
            return str(value) if value is not None else None
        if key == "products":
            return SYNC.economic_products(value)
        if key in {"loan_amount", "offered_amount", "minimum_investment", "note_rate_pct", "term_months", "ltv_pct"}:
            return SYNC.economic_number(value)
        return SYNC.normalized_financing(value)
    if not any(comparable(key, existing_dates.get(key) if key in {"expected_funding_date", "maturity_date_mode"} else getattr(listing, key, None)) != comparable(key, values[key]) for key in protected & values.keys()):
        return
    committed = (await L.committed_amounts(db, [listing.id])).get(listing.id, Decimal(0))
    if listing.status in {E.ListingStatus.FUNDED, E.ListingStatus.REPAID, E.ListingStatus.WORKOUT} or committed > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "This offering has committed or funded capital. Its financing, participation amount and linked file cannot be edited without reviewing those commitments.")


async def _require_current_file_snapshot(db: AsyncSession, listing: CpListing) -> None:
    if listing.profile_id is None:
        return
    from app.models.application_profile import ApplicationProfile

    profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file behind this listing was not found.")
    current, _sections, _cockpit = await SYNC.current_fingerprints(db, profile)
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    stamped = snapshot.get("file_fingerprints") or (snapshot.get("published") or {}).get("fingerprints") or {}
    if any(stamped.get(key) != value for key, value in current.items() if key != "package"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The file changed since this listing was prepared. Refresh its financial snapshot and review the current financing and collateral before publishing.")


@router.post("/listings", response_model=S.ListingAdminOut, status_code=status.HTTP_201_CREATED)
async def create_listing(payload: S.ListingIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    if not payload.name or not payload.vertical:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A listing needs a name and a vertical.")
    if payload.profile_id or payload.loan_id:
        from app.models.application_profile import ApplicationProfile
        from app.models.loan import Loan

        source_model, source_id = (ApplicationProfile, payload.profile_id) if payload.profile_id else (Loan, payload.loan_id)
        await db.execute(select(source_model.id).where(source_model.id == source_id).with_for_update())
        existing = await P.existing_listing(db, profile_id=payload.profile_id, loan_id=payload.loan_id)
        if existing:
            previous = await _load_admin_listing(db, uuid.UUID(str(existing["id"])))
            if payload.ai_draft_id and ((previous.source_snapshot or {}).get("ai_draft") or {}).get("id") == payload.ai_draft_id:
                return await _listing_admin_out(db, previous)
            raise HTTPException(409, detail={"code": "listing_exists", "message": "This file already has an offering. Open it to review or update the draft.", "listing_id": existing["id"]})
    listing = CpListing(ref=await R.next_listing_ref(db, payload.vertical), status=E.ListingStatus.DRAFT, created_by_id=admin.id, vertical=payload.vertical, name=payload.name)
    _apply(listing, payload)
    await _apply_file_financing(db, listing, payload=payload)
    if listing.servicer_label is None:
        listing.servicer_label = (await M.issuer_settings(db)).servicer_label
    db.add(listing)
    await db.flush()
    await L.log_event(db, listing.id, "listing.created", actor_user_id=admin.id, detail={"source_kind": listing.source_kind})
    return await _listing_admin_out(db, await _load_admin_listing(db, listing.id))


@router.get("/listings/{listing_id}", response_model=S.ListingAdminOut)
async def get_listing(listing_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    return await _listing_admin_out(db, await _load_admin_listing(db, listing_id))


@router.patch("/listings/{listing_id}", response_model=S.ListingAdminOut)
async def patch_listing(listing_id: uuid.UUID, payload: S.ListingIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    listing = await _load_admin_listing(db, listing_id)
    if listing.status in {E.ListingStatus.REPAID, E.ListingStatus.WITHDRAWN}:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "closed", "message": "A repaid or withdrawn listing is read-only."})
    await _guard_committed_listing_edit(db, listing, payload)
    before = {k: str(getattr(listing, k)) for k in _LISTING_FIELDS if k != "source_snapshot"}
    prior = SimpleNamespace(**{key: deepcopy(getattr(listing, key, None)) for key in ("id", "status", "products", "loan_amount", "offered_amount", "minimum_investment", "note_rate_pct", "term_months", "source_snapshot")})
    _apply(listing, payload)
    await _apply_file_financing(db, listing, prior=prior, payload=payload)
    changed = {k: before[k] for k in before if before[k] != str(getattr(listing, k))}
    await db.flush()
    if changed:
        await L.log_event(db, listing.id, "listing.edited", actor_user_id=admin.id, detail={"fields": sorted(changed)})
    return await _listing_admin_out(db, listing)


@router.post("/listings/{listing_id}/publish", response_model=S.ListingAdminOut)
async def publish_listing(listing_id: uuid.UUID, payload: S.PublishIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    if not payload.confirmed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Confirm the publish.")
    listing = await _load_admin_listing(db, listing_id)
    await _require_current_file_snapshot(db, listing)
    try:
        await L.publish(db, listing, actor_user_id=admin.id, reason=payload.reason,
            expected_ppm_document_id=payload.expected_ppm_document_id,
            expected_ppm_source_fingerprint=payload.expected_ppm_source_fingerprint)
    except L.RuleError as exc:
        error = _rule(exc)
        if exc.code == "incomplete":
            error.detail["publish_issues"] = L.publish_issues(listing)
        raise error from exc
    return await _listing_admin_out(db, listing)


@router.post("/listings/{listing_id}/refresh-snapshot", response_model=S.ListingAdminOut)
async def refresh_listing_snapshot(listing_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    """Re-read the file behind a listing: program readiness, the cockpit's
    figures and the desk's plan (what changes, the coverage before and after),
    so what members see follows what the desk decided on the file."""
    listing = await _load_admin_listing(db, listing_id)
    if listing.profile_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This listing was not cut from a file, so there is nothing to re-read.")
    from app.models.application_profile import ApplicationProfile

    profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file behind this listing was not found.")
    fresh = await P.readiness_snapshot(db, profile)
    await _guard_financing_change(db, listing, fresh)
    SYNC.apply_canonical_financing(listing, fresh)
    refreshed_at = datetime.now(UTC).isoformat()
    listing.source_snapshot = {**(listing.source_snapshot or {}), **fresh, "refreshed_at": refreshed_at}
    # The figures, the plan, the terms and the programs just went onto the
    # listing: stamp them so the file page knows they are current.
    current, _sections, _cockpit = await SYNC.current_fingerprints(db, profile)
    SYNC.mark_published(listing, current, by=admin.id, keys=["figures", "plan", "terms", "collateral", "deal_budget", "programs"])
    financials = fresh.get("financials") or {}
    await L.log_event(
        db,
        listing.id,
        "snapshot_refreshed",
        actor_user_id=admin.id,
        detail={
            "dscr": financials.get("dscr"),
            "dscr_after_plan": financials.get("dscr_after_plan"),
            "plan_active": bool((financials.get("plan") or {}).get("active")),
            "refreshed_at": refreshed_at,
        },
    )
    await db.commit()
    return await _listing_admin_out(db, listing)


@router.post("/listings/{listing_id}/publish-package", response_model=S.ListingAdminOut)
async def publish_listing_package(listing_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    """Push the file's package onto the listing: the current, in-sync
    executive credit brief, lender packet and investor presentation become listing documents (the
    prior ones superseded), the snapshot is re-read, and the listing is
    stamped with what was pushed so unpublished changes can be named later.
    Members see the documents once the listing itself is published."""
    listing = await _load_admin_listing(db, listing_id)
    # Serialize retries/concurrent package updates and reload the attachment
    # receipt and rows after acquiring the lock, before choosing versions.
    await db.refresh(listing, attribute_names=["source_snapshot", "documents", "status"], with_for_update=True)
    if listing.profile_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This listing was not cut from a file, so there is no package to push.")
    if listing.status in {E.ListingStatus.REPAID, E.ListingStatus.WITHDRAWN}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This listing is closed.")
    from app.models.application_profile import ApplicationProfile
    from app.models.public_underwriting_intake import PublicUnderwritingIntake
    from app.routers.dealer_ai_intake import (
        _artifact_package_metadata,
        _artifact_pdf_metadata,
        _fresh_package_artifact_pair,
        _latest_artifact,
    )

    profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None or profile.intake_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file behind this listing was not found.")
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
    if intake is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file behind this listing was not found.")
    # 409 with the desk's own words when the pair is missing, split or stale.
    summary, packet = await _fresh_package_artifact_pair(db, intake)
    artifacts = [(summary, E.DocumentKind.EXECUTIVE_SUMMARY, "Executive credit brief"),
                 (packet, E.DocumentKind.UNDERWRITING_PACKAGE, "Underwriting package")]
    summary_pdf = _artifact_pdf_metadata(summary)
    if "investor_presentation" in (summary_pdf.get("package_document_types") or []):
        presentation = await _latest_artifact(db, intake.id, "investor_presentation")
        source = _artifact_package_metadata(presentation)
        if (presentation is None or source.get("status") != "current"
                or source.get("generation_id") != _artifact_package_metadata(summary).get("generation_id")
                or _artifact_pdf_metadata(presentation).get("package_input_sha256") != summary_pdf.get("package_input_sha256")):
            raise HTTPException(status.HTTP_409_CONFLICT, "Regenerate the package so the investor presentation uses the same current evidence snapshot.")
        artifacts.append((presentation, E.DocumentKind.INVESTOR_PRESENTATION, "Investor presentation"))
    fresh = await P.readiness_snapshot(db, profile)
    await _guard_financing_change(db, listing, fresh)
    if not storage.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Document storage is not available right now.")
    now = datetime.now(UTC)
    from .services.package_documents import attach_generated_documents

    pushed = await attach_generated_documents(db, listing, artifacts, actor_id=admin.id, now=now)
    if not any(kind == E.DocumentKind.INVESTOR_PRESENTATION for _artifact, kind, _name in artifacts):
        # A valid legacy two-document generation cannot leave a presentation
        # from another generation marked current beside its new PDFs.
        for document in listing.documents:
            if document.kind == E.DocumentKind.INVESTOR_PRESENTATION and document.status != E.DocumentStatus.SUPERSEDED:
                document.status = E.DocumentStatus.SUPERSEDED
                document.superseded_at = now
    SYNC.apply_canonical_financing(listing, fresh)
    listing.source_snapshot = {**(listing.source_snapshot or {}), **fresh, "refreshed_at": now.isoformat()}
    current, _sections, _cockpit = await SYNC.current_fingerprints(db, profile)
    package = await SYNC.current_package(db, profile)
    SYNC.mark_published(listing, current, by=admin.id, package={**(package or {}), "documents": pushed, "at": now.isoformat()})
    await L.log_event(db, listing.id, "package_published", actor_user_id=admin.id, detail={"documents": pushed, "generation_id": (package or {}).get("generation_id")})
    await db.commit()
    await db.refresh(listing, attribute_names=["documents"])
    return await _listing_admin_out(db, listing)


@router.post("/listings/{listing_id}/status", response_model=S.ListingAdminOut)
async def set_listing_status(listing_id: uuid.UUID, payload: S.StatusIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    listing = await _load_admin_listing(db, listing_id)
    try:
        await L.transition(db, listing, to=payload.status, actor_user_id=admin.id, reason=payload.reason, outcome=payload.outcome, effective_at=payload.effective_at)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    return await _listing_admin_out(db, listing)


@router.post("/listings/{listing_id}/withdraw", response_model=S.ListingAdminOut)
async def withdraw_listing(listing_id: uuid.UUID, payload: S.WithdrawIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    if not payload.confirmed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Confirm the withdrawal.")
    listing = await _load_admin_listing(db, listing_id)
    try:
        await L.withdraw(db, listing, actor_user_id=admin.id, reason=payload.reason)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    return await _listing_admin_out(db, listing)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@router.get("/listings/{listing_id}/ppm", response_model=S.PpmRead)
async def get_ppm(listing_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.PpmRead:
    from .services import ppm
    return S.PpmRead(**await ppm.read_state(db, await _load_admin_listing(db, listing_id)))


@router.get("/listings/{listing_id}/investor-returns", response_model=InvestorReturnsRead)
async def listing_investor_returns(listing_id: uuid.UUID, admin: CpAdmin, db: Db,
    query: Annotated[InvestorReturnsQuery, Depends()]) -> InvestorReturnsRead:
    from app.services.investor_returns import read_listing_projection
    listing = await _load_admin_listing(db, listing_id)
    return InvestorReturnsRead(**await read_listing_projection(listing, query=query))


@router.post("/listings/{listing_id}/ppm/generate", response_model=S.PpmRead)
async def generate_ppm(listing_id: uuid.UUID, payload: S.PpmGenerateIn, admin: CpAdmin, db: Db) -> S.PpmRead:
    from .services import ppm
    return S.PpmRead(**await ppm.generate(db, await _load_admin_listing(db, listing_id), admin.id, payload))


@router.patch("/listings/{listing_id}/ppm/draft", response_model=S.PpmRead)
async def save_ppm_draft(listing_id: uuid.UUID, payload: S.PpmDraftIn, admin: CpAdmin, db: Db) -> S.PpmRead:
    from .services import ppm
    return S.PpmRead(**await ppm.save_draft(db, await _load_admin_listing(db, listing_id), admin.id, payload))


@router.post("/listings/{listing_id}/ppm/review", response_model=S.PpmRead)
async def review_ppm(listing_id: uuid.UUID, payload: S.PpmReviewIn, admin: CpAdmin, db: Db) -> S.PpmRead:
    from .services import ppm
    return S.PpmRead(**await ppm.review(db, await _load_admin_listing(db, listing_id), admin.id, payload))


@router.post("/listings/{listing_id}/ppm/versions/{document_id}/url", response_model=S.DocumentUrlOut)
async def ppm_version_url(listing_id: uuid.UUID, document_id: uuid.UUID, request: Request, admin: CpAdmin, db: Db, download: bool = False) -> S.DocumentUrlOut:
    from .services import ppm
    listing = await _load_admin_listing(db, listing_id)
    state = await ppm.read_state(db, listing)
    allowed = {str(row["document_id"]) for row in state["history"]}
    if state["current_approved"]:
        allowed.add(str(state["current_approved"]["document_id"]))
    document = next((doc for doc in listing.documents if doc.id == document_id and str(doc.id) in allowed), None)
    if document is None:
        raise HTTPException(404, "PPM version not found.")
    url = storage.presign_get(document.s3_key, content_type="application/pdf", download_filename=document.name,
        disposition="attachment" if download else "inline")
    if url is None:
        raise HTTPException(503, "Document storage is not available right now.")
    await L.log_event(db, listing.id, "ppm.accessed", actor_user_id=admin.id,
        detail={"document_id": str(document.id), "version": document.version, "download": download,
            "ip_address": (client_ip(request) or "")[:64], "user_agent": (request.headers.get("user-agent") or "")[:400]})
    return S.DocumentUrlOut(url=url, expires_in_seconds=storage.DOCUMENT_URL_TTL_SECONDS)


@router.post("/listings/{listing_id}/ppm/versions/{document_id}/reinstate", response_model=S.PpmRead)
async def reinstate_ppm(listing_id: uuid.UUID, document_id: uuid.UUID, payload: S.PpmReinstateIn, admin: CpAdmin, db: Db) -> S.PpmRead:
    from .services import ppm
    return S.PpmRead(**await ppm.reinstate(db, await _load_admin_listing(db, listing_id), document_id, admin.id, payload))


@router.get("/listings/{listing_id}/file-documents")
async def admin_file_documents(listing_id: uuid.UUID, admin: CpAdmin, db: Db) -> dict[str, Any]:
    from .services import document_catalog

    listing = await _load_admin_listing(db, listing_id)
    return await document_catalog.catalog(db, listing, staff=True)


@router.get("/listings/{listing_id}/document-grants", response_model=list[S.DocumentGrantOut])
async def admin_document_grants(listing_id: uuid.UUID, admin: CpAdmin, db: Db) -> list[S.DocumentGrantOut]:
    from .services.document_catalog import grant_out

    await _load_admin_listing(db, listing_id)
    grants = (await db.execute(select(CpListingDocumentGrant).where(CpListingDocumentGrant.listing_id == listing_id).order_by(CpListingDocumentGrant.granted_at.desc()))).scalars().all()
    return [grant_out(grant) for grant in grants]


@router.put("/listings/{listing_id}/document-grants/{member_id}", response_model=S.DocumentGrantOut)
async def admin_set_document_grant(listing_id: uuid.UUID, member_id: uuid.UUID, payload: S.DocumentGrantIn, admin: CpAdmin, db: Db) -> S.DocumentGrantOut:
    from .services import document_catalog

    listing = await _load_admin_listing(db, listing_id)
    grant = await document_catalog.set_grant(db, listing, member_id, payload, admin.id)
    await db.commit()
    return document_catalog.grant_out(grant)


@router.post("/listings/{listing_id}/file-documents/{file_id}/url", response_model=S.DocumentUrlOut)
async def admin_original_url(listing_id: uuid.UUID, file_id: uuid.UUID, request: Request, admin: CpAdmin, db: Db, download: bool = False) -> S.DocumentUrlOut:
    from .services import document_catalog

    listing = await _load_admin_listing(db, listing_id)
    file = await document_catalog.authorized_original(db, listing, file_id, staff=True)
    url = storage.presign_get(file.s3_key, content_type=file.content_type, download_filename=file.file_name,
        disposition="attachment" if download else "inline", version_id=file.s3_version_id)
    if url is None:
        raise HTTPException(503, "Document storage is not available right now.")
    await L.log_event(db, listing.id, "source_document.accessed", actor_user_id=admin.id,
        detail={"file_id": str(file.id), "member_id": None, "staff_preview": True, "download": download,
            "ip_address": (client_ip(request) or "")[:64], "user_agent": (request.headers.get("user-agent") or "")[:400]})
    return S.DocumentUrlOut(url=url, expires_in_seconds=storage.DOCUMENT_URL_TTL_SECONDS)


@router.post("/listings/{listing_id}/documents/{document_id}/url", response_model=S.DocumentUrlOut)
async def admin_document_url(listing_id: uuid.UUID, document_id: uuid.UUID, request: Request, admin: CpAdmin, db: Db, download: bool = False) -> S.DocumentUrlOut:
    """Short-lived, audited access to an attached READY listing document."""
    listing = await _load_admin_listing(db, listing_id)
    from .services.ppm import bind_access_state
    await bind_access_state(db, listing)
    document = next((row for row in L.ready_documents(listing) if row.id == document_id), None)
    if document is None:
        raise HTTPException(404, "Document not found.")
    url = storage.presign_get(document.s3_key, content_type=document.content_type, download_filename=document.name,
        disposition="attachment" if download else "inline")
    if url is None:
        raise HTTPException(503, "Document storage is not available right now.")
    db.add(CpDocumentAccess(member_id=None, actor_user_id=admin.id, listing_id=listing.id, document_id=document.id,
        accessed_at=L.now_utc(), ip_address=(client_ip(request) or "")[:64] or None,
        user_agent=(request.headers.get("user-agent") or "")[:400] or None))
    await db.flush()
    return S.DocumentUrlOut(url=url, expires_in_seconds=storage.DOCUMENT_URL_TTL_SECONDS)


@router.post("/listings/{listing_id}/documents/upload-init", response_model=S.DocumentInitOut)
async def document_upload_init(listing_id: uuid.UUID, payload: S.DocumentInitIn, admin: CpAdmin, db: Db) -> S.DocumentInitOut:
    listing = await _load_admin_listing(db, listing_id)
    if payload.kind == E.DocumentKind.PPM:
        from .services import ppm
        await ppm._lock(db, listing)
        ppm._writable(listing)
    if not storage.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Document storage is not configured.")
    key = storage.build_key(listing.id, payload.name)
    latest = max((d.version for d in listing.documents if d.kind == payload.kind), default=0)
    doc = CpListingDocument(
        listing_id=listing.id, kind=payload.kind, name=payload.name.strip(), s3_key=key, content_type=payload.content_type,
        size_bytes=payload.size_bytes, version=latest + 1, status=E.DocumentStatus.PENDING, uploaded_by_id=admin.id,
    )
    db.add(doc)
    await db.flush()
    contract = storage.presign_put(key, content_type=payload.content_type)
    if contract is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not create an upload link.")
    return S.DocumentInitOut(document_id=doc.id, upload_url=str(contract["upload_url"]), headers=dict(contract["headers"]), s3_key=key)


@router.post("/listings/{listing_id}/documents/upload-complete", response_model=S.DocumentOut)
async def document_upload_complete(listing_id: uuid.UUID, payload: S.DocumentCompleteIn, admin: CpAdmin, db: Db) -> S.DocumentOut:
    listing = await _load_admin_listing(db, listing_id)
    doc = next((d for d in listing.documents if d.id == payload.document_id), None)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    if doc.kind == E.DocumentKind.PPM:
        from .services import ppm
        # Uploaded PPMs must be explicitly reviewed against the saved offering.
        # Previously completed READY uploads keep their historical approval.
        if doc.status == E.DocumentStatus.READY:
            return S.DocumentOut(**L.document_out(doc))
        await ppm._lock(db, listing)
        doc = next(d for d in listing.documents if d.id == payload.document_id)
    head = storage.head_object(doc.s3_key)
    if head is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "not_uploaded", "message": "The file has not arrived in storage yet."})
    doc.size_bytes = head.get("size_bytes") or doc.size_bytes
    doc.content_type = head.get("content_type") or doc.content_type
    if doc.kind == E.DocumentKind.PPM:
        if str(doc.content_type or "").split(";")[0].strip().lower() != "application/pdf":
            raise HTTPException(422, "A PPM must be a PDF document.")
        from .services import ppm
        await ppm.adopt_upload(db, listing, doc, admin.id)
        doc.uploaded_at = _now()
        await db.flush()
        return S.DocumentOut(**L.document_out(doc))
    doc.status = E.DocumentStatus.READY
    doc.uploaded_at = _now()
    for other in listing.documents:
        if other.id != doc.id and other.kind == doc.kind and other.status == E.DocumentStatus.READY:
            other.status = E.DocumentStatus.SUPERSEDED
            other.superseded_at = _now()
    await db.flush()
    await L.log_event(db, listing.id, "document.uploaded", actor_user_id=admin.id, detail={"document_id": str(doc.id), "kind": doc.kind, "version": doc.version})
    return S.DocumentOut(**L.document_out(doc))


@router.delete("/listings/{listing_id}/documents/{document_id}", response_model=S.DocumentOut)
async def document_supersede(listing_id: uuid.UUID, document_id: uuid.UUID, admin: CpAdmin, db: Db) -> S.DocumentOut:
    """Nothing is physically deleted: the row is superseded and the object stays."""
    listing = await _load_admin_listing(db, listing_id)
    doc = next((d for d in listing.documents if d.id == document_id), None)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    doc.status = E.DocumentStatus.SUPERSEDED
    doc.superseded_at = _now()
    await db.flush()
    await L.log_event(db, listing.id, "document.superseded", actor_user_id=admin.id, detail={"document_id": str(doc.id), "kind": doc.kind})
    return S.DocumentOut(**L.document_out(doc))


# ---------------------------------------------------------------------------
# Subscriptions and participations
# ---------------------------------------------------------------------------


async def _sub_bundle(db: AsyncSession, subscription_id: uuid.UUID) -> tuple[CpSubscription, CpListing, CpMember, User]:
    row = (
        await db.execute(
            select(CpSubscription, CpListing, CpMember, User).join(CpListing, CpListing.id == CpSubscription.listing_id)
            .join(CpMember, CpMember.id == CpSubscription.member_id).join(User, User.id == CpMember.user_id)
            .options(selectinload(CpListing.documents), selectinload(CpMember.entities)).where(CpSubscription.id == subscription_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")
    return row[0], row[1], row[2], row[3]


@router.get("/subscriptions", response_model=list[S.SubscriptionAdminOut])
async def list_subscriptions(admin: CpAdmin, db: Db, status_filter: str | None = Query(None), listing_id: Annotated[uuid.UUID | None, Query()] = None) -> list[S.SubscriptionAdminOut]:
    stmt = (
        select(CpSubscription, CpListing, CpMember, User).join(CpListing, CpListing.id == CpSubscription.listing_id)
        .join(CpMember, CpMember.id == CpSubscription.member_id).join(User, User.id == CpMember.user_id)
        .options(selectinload(CpMember.entities)).order_by(CpSubscription.requested_at.desc())
    )
    if status_filter:
        stmt = stmt.where(CpSubscription.status == status_filter)
    if listing_id:
        stmt = stmt.where(CpSubscription.listing_id == listing_id)
    out = []
    for sub, listing, member, user in (await db.execute(stmt.limit(500))).all():
        d = S.SubscriptionOut.model_validate(sub).model_dump()
        d.update(
            listing_ref=listing.ref, listing_name=listing.name, entity_name=next((e.name for e in member.entities if e.id == sub.entity_id), None),
            member_id=member.id, member_name=member.display_name or user.name, member_email=user.email, member_state=member.state_of_residence,
            ack_ppm=sub.ack_ppm, ack_accredited=sub.ack_accredited, acknowledged_at=sub.acknowledged_at, cancelled_at=sub.cancelled_at,
            cancel_reason=sub.cancel_reason, declined_at=sub.declined_at, admin_notes=sub.admin_notes,
        )
        out.append(S.SubscriptionAdminOut(**d))
    return out


@router.post("/subscriptions/{subscription_id}/send-documents", response_model=S.ListingAdminOut)
async def subscription_send_documents(subscription_id: uuid.UUID, payload: S.SubscriptionActionIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    sub, listing, member, user = await _sub_bundle(db, subscription_id)
    try:
        await L.send_documents(db, sub, actor_user_id=admin.id, notes=payload.notes)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    settings_row = await M.issuer_settings(db)
    await emails.subscription_documents_sent(
        db, to=user.email, name=member.display_name or user.name, ref=listing.ref, amount=f"${L._d(sub.amount):,.2f}", wiring_instructions=settings_row.wiring_instructions
    )
    return await _listing_admin_out(db, listing)


@router.post("/subscriptions/{subscription_id}/mark-signed", response_model=S.ListingAdminOut)
async def subscription_mark_signed(subscription_id: uuid.UUID, payload: S.SubscriptionActionIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    sub, listing, member, user = await _sub_bundle(db, subscription_id)
    try:
        await L.mark_signed(db, sub, listing, member, actor_user_id=admin.id, signed_at=payload.signed_at, notes=payload.notes)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    return await _listing_admin_out(db, listing)


@router.post("/subscriptions/{subscription_id}/mark-funded", response_model=S.ListingAdminOut)
async def subscription_mark_funded(subscription_id: uuid.UUID, payload: S.SubscriptionActionIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    sub, listing, member, user = await _sub_bundle(db, subscription_id)
    try:
        await L.mark_funded(db, sub, listing, actor_user_id=admin.id, funded_at=payload.funded_at, funded_amount=payload.funded_amount, notes=payload.notes)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    await emails.subscription_confirmed(db, to=user.email, name=member.display_name or user.name, ref=listing.ref, amount=f"${L._d(sub.funded_amount):,.2f}")
    return await _listing_admin_out(db, listing)


@router.post("/subscriptions/{subscription_id}/cancel", response_model=S.ListingAdminOut)
async def subscription_cancel(subscription_id: uuid.UUID, payload: S.SubscriptionActionIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    sub, listing, member, user = await _sub_bundle(db, subscription_id)
    try:
        await L.cancel_subscription(db, sub, listing, actor_user_id=admin.id, reason=payload.reason)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    return await _listing_admin_out(db, listing)


@router.post("/subscriptions/{subscription_id}/decline", response_model=S.ListingAdminOut)
async def subscription_decline(subscription_id: uuid.UUID, payload: S.SubscriptionActionIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    sub, listing, member, user = await _sub_bundle(db, subscription_id)
    try:
        await L.cancel_subscription(db, sub, listing, actor_user_id=admin.id, reason=payload.reason, declined=True)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    return await _listing_admin_out(db, listing)


def _patch_participation(part: CpParticipation, payload: S.ParticipationPatchIn, actor_id: uuid.UUID) -> None:
    data = payload.model_dump(exclude_unset=True, exclude={"id"})
    for key, value in data.items():
        setattr(part, key, value)
    if part.servicer_status == E.ServicerStatus.PAID_IN_FULL and part.closed_at is None:
        part.closed_at = _now()
        part.principal_outstanding = L.ZERO
    part.updated_by_id = actor_id


@router.patch("/participations/{participation_id}", response_model=S.ListingAdminOut)
async def patch_participation(participation_id: uuid.UUID, payload: S.ParticipationPatchIn, admin: CpAdmin, db: Db) -> S.ListingAdminOut:
    part = await db.get(CpParticipation, participation_id)
    if part is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Participation not found.")
    _patch_participation(part, payload, admin.id)
    await db.flush()
    await L.log_event(db, part.listing_id, "participation.updated", actor_user_id=admin.id, detail={"participation_id": str(part.id)})
    return await _listing_admin_out(db, await _load_admin_listing(db, part.listing_id))


@router.post("/participations/bulk-status")
async def bulk_participation_status(payload: S.ParticipationBulkIn, admin: CpAdmin, db: Db) -> dict[str, int]:
    updated = 0
    for item in payload.items:
        part = await db.get(CpParticipation, item.id)
        if part is None:
            continue
        _patch_participation(part, item, admin.id)
        updated += 1
    await db.flush()
    return {"updated": updated}


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


async def _transfer_admin_rows(db: AsyncSession, transfer_id: uuid.UUID | None = None, status_filter: str | None = None) -> list[S.TransferAdminOut]:
    seller = CpMember.__table__.alias("seller")
    stmt = select(CpTransfer, CpListing).join(CpListing, CpListing.id == CpTransfer.listing_id).order_by(CpTransfer.listed_at.desc())
    if transfer_id:
        stmt = stmt.where(CpTransfer.id == transfer_id)
    if status_filter:
        stmt = stmt.where(CpTransfer.status == status_filter)
    rows = (await db.execute(stmt.limit(500))).all()
    member_ids = {t.seller_member_id for t, _ in rows} | {t.buyer_member_id for t, _ in rows if t.buyer_member_id}
    names: dict[uuid.UUID, str] = {}
    if member_ids:
        for m, u in (await db.execute(select(CpMember, User).join(User, User.id == CpMember.user_id).where(CpMember.id.in_(member_ids)))).all():
            names[m.id] = m.display_name or u.name
    del seller
    out = []
    for t, listing in rows:
        out.append(
            S.TransferAdminOut(
                id=t.id, ref=t.ref, listing_id=listing.id, listing_ref=listing.ref, listing_name=listing.name, vertical=listing.vertical,
                vertical_label=E.VERTICAL_LABELS.get(listing.vertical, listing.vertical), principal_amount=t.principal_amount,
                note_rate_pct=listing.note_rate_pct, remaining_term_months=L.remaining_term_months(listing), ask_pct=t.ask_pct,
                implied_yield_pct=L.implied_yield(listing.note_rate_pct, t.ask_pct), listed_at=t.listed_at, status=t.status, is_mine=False,
                can_request=False, participation_id=t.participation_id, seller_member_id=t.seller_member_id, seller_name=names.get(t.seller_member_id),
                buyer_member_id=t.buyer_member_id, buyer_name=names.get(t.buyer_member_id) if t.buyer_member_id else None,
                requested_at=t.requested_at, consented_at=t.consented_at, completed_at=t.completed_at, declined_at=t.declined_at,
                withdrawn_at=t.withdrawn_at, notes=t.notes,
            )
        )
    return out


@router.get("/transfers", response_model=list[S.TransferAdminOut])
async def list_transfers(admin: CpAdmin, db: Db, status_filter: str | None = Query(None)) -> list[S.TransferAdminOut]:
    return await _transfer_admin_rows(db, status_filter=status_filter)


async def _decide(db: AsyncSession, admin: User, transfer_id: uuid.UUID, decision: str, notes: str | None) -> S.TransferAdminOut:
    t = await db.get(CpTransfer, transfer_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found.")
    try:
        await L.decide_transfer(db, transfer=t, decision=decision, actor_user_id=admin.id, notes=notes)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    for member_id in (t.seller_member_id, t.buyer_member_id):
        if not member_id:
            continue
        row = (await db.execute(select(CpMember, User).join(User, User.id == CpMember.user_id).where(CpMember.id == member_id))).first()
        if row:
            await emails.transfer_decided(db, to=row[1].email, name=row[0].display_name or row[1].name, transfer_ref=t.ref, decision={"consent": "consented to", "decline": "declined", "complete": "completed"}[decision])
    return (await _transfer_admin_rows(db, transfer_id=t.id))[0]


@router.post("/transfers/{transfer_id}/consent", response_model=S.TransferAdminOut)
async def transfer_consent(transfer_id: uuid.UUID, payload: S.TransferDecisionIn, admin: CpAdmin, db: Db) -> S.TransferAdminOut:
    return await _decide(db, admin, transfer_id, "consent", payload.notes)


@router.post("/transfers/{transfer_id}/decline", response_model=S.TransferAdminOut)
async def transfer_decline(transfer_id: uuid.UUID, payload: S.TransferDecisionIn, admin: CpAdmin, db: Db) -> S.TransferAdminOut:
    return await _decide(db, admin, transfer_id, "decline", payload.notes)


@router.post("/transfers/{transfer_id}/complete", response_model=S.TransferAdminOut)
async def transfer_complete(transfer_id: uuid.UUID, payload: S.TransferDecisionIn, admin: CpAdmin, db: Db) -> S.TransferAdminOut:
    return await _decide(db, admin, transfer_id, "complete", payload.notes)


# ---------------------------------------------------------------------------
# Filings and issuer settings
# ---------------------------------------------------------------------------


async def _filing_rows(db: AsyncSession, stmt) -> list[S.FilingOut]:
    rows = (await db.execute(stmt)).scalars().all()
    for f in rows:
        await db.refresh(f, attribute_names=["created_at", "updated_at"])
    listing_ids = {f.listing_id for f in rows if f.listing_id}
    listings: dict[uuid.UUID, CpListing] = {}
    if listing_ids:
        for lst in (await db.execute(select(CpListing).where(CpListing.id.in_(listing_ids)))).scalars().all():
            listings[lst.id] = lst
    counts = await F.investor_counts(db)
    out = []
    for f in rows:
        d = S.FilingOut.model_validate(f).model_dump()
        if f.listing_id and f.listing_id in listings:
            d.update(listing_ref=listings[f.listing_id].ref, listing_name=listings[f.listing_id].name, investor_count=counts.get(f.listing_id, 0))
        out.append(S.FilingOut(**d))
    return out


@router.get("/filings", response_model=list[S.FilingOut])
async def list_filings(admin: CpAdmin, db: Db, kind: str | None = Query(None), listing_id: Annotated[uuid.UUID | None, Query()] = None) -> list[S.FilingOut]:
    stmt = select(CpFiling).order_by(CpFiling.due_at.asc().nullslast(), CpFiling.created_at.desc())
    if kind:
        stmt = stmt.where(CpFiling.kind == kind)
    if listing_id:
        stmt = stmt.where(CpFiling.listing_id == listing_id)
    return await _filing_rows(db, stmt.limit(1000))


@router.get("/filings/summary", response_model=S.FilingsSummaryOut)
async def filings_summary(admin: CpAdmin, db: Db) -> S.FilingsSummaryOut:
    filings = (await db.execute(select(CpFiling))).scalars().all()
    summary = F.summary(filings)
    members = (await db.execute(select(CpMember))).scalars().all()
    approved = [m for m in members if m.status in {E.MemberStatus.APPROVED, E.MemberStatus.WAITING}]
    with_q = [m for m in members if m.last_questionnaire_id]
    waiting = [m for m in members if m.status == E.MemberStatus.WAITING]
    controls = [
        {"label": "Public pages show no offering terms", "value": "By design: terms live only behind the member gate"},
        {"label": "Relationship established before first offering view", "value": f"{len([m for m in approved if m.relationship_established_at])} of {len(approved)}"},
        {"label": "Accreditation questionnaire on file", "value": f"{len(with_q)} of {len(members)}"},
        {"label": "Waiting period enforced", "value": f"{len(waiting)} in progress"},
        {"label": "Every document download logged", "value": "cp_document_access"},
    ]
    return S.FilingsSummaryOut(**summary, controls=controls)


@router.post("/filings", response_model=S.FilingOut, status_code=status.HTTP_201_CREATED)
async def create_filing(payload: S.FilingIn, admin: CpAdmin, db: Db) -> S.FilingOut:
    data = payload.model_dump(exclude_unset=True)
    if data.get("state"):
        data["state"] = data["state"].upper()
    if not data.get("status"):
        data["status"] = E.FilingStatus.PENDING if payload.kind == E.FilingKind.BAD_ACTOR_CHECK else E.FilingStatus.DUE
    row = CpFiling(created_by_id=admin.id, **data)
    db.add(row)
    await db.flush()
    await db.refresh(row, attribute_names=["created_at", "updated_at"])
    return (await _filing_rows(db, select(CpFiling).where(CpFiling.id == row.id)))[0]


@router.patch("/filings/{filing_id}", response_model=S.FilingOut)
async def patch_filing(filing_id: uuid.UUID, payload: S.FilingPatchIn, admin: CpAdmin, db: Db) -> S.FilingOut:
    row = await db.get(CpFiling, filing_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Filing not found.")
    data = payload.model_dump(exclude_unset=True)
    if data.get("state"):
        data["state"] = data["state"].upper()
    was_filed = row.status == E.FilingStatus.FILED
    for key, value in data.items():
        setattr(row, key, value)
    if row.status == E.FilingStatus.FILED and row.filed_at is None:
        row.filed_at = _now().date()
    await db.flush()
    if row.status == E.FilingStatus.FILED and not was_filed and row.kind == E.FilingKind.FORM_D:
        await F.on_form_d_filed(db, row, filed_at=row.filed_at, actor_user_id=admin.id)
    await db.refresh(row, attribute_names=["created_at", "updated_at"])
    return (await _filing_rows(db, select(CpFiling).where(CpFiling.id == row.id)))[0]


@router.get("/issuer", response_model=S.IssuerSettingsOut)
async def get_issuer(admin: CpAdmin, db: Db) -> S.IssuerSettingsOut:
    return S.IssuerSettingsOut.model_validate(await M.issuer_settings(db))


@router.put("/issuer", response_model=S.IssuerSettingsOut)
async def put_issuer(payload: S.IssuerSettingsIn, admin: CpAdmin, db: Db) -> S.IssuerSettingsOut:
    row = await M.issuer_settings(db)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return S.IssuerSettingsOut.model_validate(row)


__all__ = ["router", "Request"]
