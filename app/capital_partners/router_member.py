"""The member portal's API: /capital/*.

Every read of an offering passes through ``ClearedMember`` (approved and
past the waiting period, or an operator in preview). Writes that commit a
member (subscribe, list a transfer) pass through ``SubscribingMember``,
which excludes preview.
"""

from __future__ import annotations

import csv
import io
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.schemas.investor_returns import InvestorReturnsQuery, InvestorReturnsRead
from app.services.payment_authorization import client_ip

from . import enums as E
from . import schemas as S
from .deps import ClearedMember, Member, SubscribingMember
from .models import (
    CpDocumentAccess,
    CpListing,
    CpMemberEntity,
    CpParticipation,
    CpQuestionnaire,
    CpSubscription,
    CpTransfer,
)
from .services import emails, storage
from .services import listings as L
from .services import members as M

router = APIRouter(prefix="/capital", tags=["capital-member"])
Db = Annotated[AsyncSession, Depends(get_db)]

STATUS_LABELS = {
    "requested": "Questionnaire pending",
    "questionnaire": "Questionnaire pending",
    "under_review": "Under review",
    "waiting": "Waiting period",
    "approved": "Accredited · approved",
    "suspended": "Suspended",
    "preview": "Operator preview",
}


def _rule(exc: L.RuleError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.message})


def _entity_name(member, entity_id: uuid.UUID | None) -> str | None:
    if member is None or entity_id is None:
        return None
    return next((e.name for e in member.entities if e.id == entity_id), None)


# ---------------------------------------------------------------------------
# Me, questionnaire, entities
# ---------------------------------------------------------------------------


@router.get("/me", response_model=S.MeOut)
async def me(ctx: Member, db: Db) -> S.MeOut:
    if ctx.preview:
        return S.MeOut(
            user_id=ctx.user.id, email=ctx.user.email, name=ctx.user.name, role=str(ctx.user.role), preview=True,
            member_id=None, status="preview", status_label=STATUS_LABELS["preview"], investing_as=None,
            display_name=ctx.user.name, state_of_residence=None, gate=None, entities=[], has_questionnaire=False,
            questionnaire_outcome=None, questionnaire_notes=None,
        )
    m = ctx.member
    latest = await db.get(CpQuestionnaire, m.last_questionnaire_id) if m.last_questionnaire_id else None
    return S.MeOut(
        user_id=ctx.user.id, email=ctx.user.email, name=ctx.user.name, role=str(ctx.user.role), preview=False,
        member_id=m.id, status=m.status, status_label=STATUS_LABELS.get(m.status, m.status),
        investing_as=m.investing_as, display_name=m.display_name or ctx.user.name, state_of_residence=m.state_of_residence,
        gate=S.GateOut(**M.gate_state(m)), entities=[S.EntityOut.model_validate(e) for e in m.entities],
        has_questionnaire=latest is not None, questionnaire_outcome=latest.outcome if latest else None,
        questionnaire_notes=latest.review_notes if latest and latest.outcome == E.QuestionnaireOutcome.NEEDS_CHANGES else None,
    )


@router.get("/questionnaire", response_model=S.QuestionnaireOut | None)
async def current_questionnaire(ctx: Member, db: Db) -> S.QuestionnaireOut | None:
    if ctx.preview or not ctx.member.last_questionnaire_id:
        return None
    row = await db.get(CpQuestionnaire, ctx.member.last_questionnaire_id)
    return S.QuestionnaireOut.model_validate(row) if row else None


@router.post("/questionnaire", response_model=S.QuestionnaireOut, status_code=status.HTTP_201_CREATED)
async def submit_questionnaire(payload: S.QuestionnaireIn, ctx: Member, request: Request, db: Db) -> S.QuestionnaireOut:
    if ctx.preview:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": "preview_only", "message": "Operators preview the portal only."})
    m = ctx.member
    for ent in payload.entities:
        if not any(e.name.strip().lower() == ent.name.strip().lower() for e in m.entities):
            m.entities.append(CpMemberEntity(name=ent.name.strip(), kind=ent.kind, state=ent.state, accreditation_basis=ent.accreditation_basis, is_default=ent.is_default))
    if not m.entities and payload.investing_as == E.InvestingAs.INDIVIDUAL:
        m.entities.append(CpMemberEntity(name=m.display_name or ctx.user.name, kind=E.EntityKind.INDIVIDUAL, state=payload.state_of_residence, is_default=True))
    await db.flush()
    try:
        row = await M.submit_questionnaire(
            db, m, answers=payload.answers, basis=payload.basis, investing_as=payload.investing_as,
            state_of_residence=(payload.state_of_residence or "").upper() or None, entity_id=payload.entity_id,
            ip_address=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        )
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": "suspended", "message": "This membership is suspended."}) from exc
    settings_row = await M.issuer_settings(db)
    await emails.questionnaire_received(db, to=ctx.user.email, name=m.display_name or ctx.user.name)
    recipients = [e for e in (settings_row.notify_emails or []) if e]
    if recipients:
        await emails.notify_desk(
            db, recipients=recipients, subject=f"Questionnaire submitted: {m.display_name or ctx.user.email}",
            body=f"{m.display_name or ctx.user.email} submitted accreditation questionnaire v{row.version}. Review it under Marketplace > Members.",
            template_key="cp.questionnaire_submitted",
        )
    return S.QuestionnaireOut.model_validate(row)


@router.post("/entities", response_model=S.EntityOut, status_code=status.HTTP_201_CREATED)
async def add_entity(payload: S.EntityIn, ctx: Member, db: Db) -> S.EntityOut:
    if ctx.preview:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": "preview_only", "message": "Operators preview the portal only."})
    ent = CpMemberEntity(member_id=ctx.member.id, name=payload.name.strip(), kind=payload.kind, state=payload.state, accreditation_basis=payload.accreditation_basis, is_default=payload.is_default)
    if payload.is_default:
        for e in ctx.member.entities:
            e.is_default = False
    db.add(ent)
    await db.flush()
    return S.EntityOut.model_validate(ent)


@router.patch("/entities/{entity_id}", response_model=S.EntityOut)
async def patch_entity(entity_id: uuid.UUID, payload: S.EntityIn, ctx: Member, db: Db) -> S.EntityOut:
    if ctx.preview:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": "preview_only", "message": "Operators preview the portal only."})
    ent = next((e for e in ctx.member.entities if e.id == entity_id), None)
    if ent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entity not found.")
    ent.name, ent.kind, ent.state, ent.accreditation_basis = payload.name.strip(), payload.kind, payload.state, payload.accreditation_basis
    if payload.is_default:
        for e in ctx.member.entities:
            e.is_default = e.id == entity_id
    await db.flush()
    return S.EntityOut.model_validate(ent)


# ---------------------------------------------------------------------------
# Marketplace
# ---------------------------------------------------------------------------


def _csv_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


@router.get("/listings", response_model=S.ListingsPage)
async def listings(
    ctx: ClearedMember,
    db: Db,
    vertical: str = Query("all"),
    status_in: str | None = Query(None, alias="status"),
    program: str | None = None,
    collateral: str | None = None,
    frequency: str | None = None,
    lien: str | None = None,
    state: str | None = None,
    min_rate: Decimal | None = None,
    max_term: int | None = None,
    max_ltv: Decimal | None = None,
    max_minimum: Decimal | None = None,
    min_remaining: Decimal | None = None,
    personal_guarantee: bool = False,
    sort: str = Query("newest"),
) -> S.ListingsPage:
    f = L.MarketFilters(
        vertical=vertical if vertical in {"all", "real_estate", "auto", "business"} else "all",
        statuses=_csv_list(status_in) or ["open", "closing"],
        programs=_csv_list(program), collaterals=_csv_list(collateral), frequencies=_csv_list(frequency),
        lien=lien, state=(state or "").upper() or None, min_rate=min_rate, max_term=max_term, max_ltv=max_ltv,
        max_minimum=max_minimum, min_remaining=min_remaining, personal_guarantee=personal_guarantee,
        sort=sort if sort in {"newest", "rate", "term", "ltv", "remaining"} else "newest",
    )
    return S.ListingsPage(**await L.market(db, f))


async def _listing_or_404(db: AsyncSession, ref: str) -> CpListing:
    listing = await L.load_listing(db, ref=ref.upper())
    if listing is None or listing.status in {E.ListingStatus.DRAFT, E.ListingStatus.WITHDRAWN}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offering not found.")
    return listing


@router.get("/listings/{ref}", response_model=S.ListingDetail)
async def listing_detail(ref: str, ctx: ClearedMember, db: Db) -> S.ListingDetail:
    listing = await _listing_or_404(db, ref)
    from .services.ppm import bind_access_state
    ppm_access = await bind_access_state(db, listing)
    committed = (await L.committed_amounts(db, [listing.id])).get(listing.id, L.ZERO)
    mine: list[dict[str, Any]] = []
    if ctx.member is not None:
        subs = (
            await db.execute(
                select(CpSubscription).where(CpSubscription.listing_id == listing.id, CpSubscription.member_id == ctx.member.id).order_by(CpSubscription.requested_at.desc())
            )
        ).scalars().all()
        mine = [
            {**S.SubscriptionOut.model_validate(s).model_dump(), "listing_ref": listing.ref, "listing_name": listing.name, "entity_name": _entity_name(ctx.member, s.entity_id)}
            for s in subs
        ]
    return S.ListingDetail(
        **L.card(listing, committed),
        summary=listing.summary, guarantee_text=listing.guarantee_text, borrower_track_record=listing.borrower_track_record,
        exit_plan=listing.exit_plan, valuation_amount=listing.valuation_amount, valuation_source=listing.valuation_source,
        title_status=listing.title_status, servicer_label=listing.servicer_label,
        documents=[S.DocumentOut(**L.document_out(d)) for d in L.ready_documents(listing)],
        my_subscriptions=[S.SubscriptionOut(**s) for s in mine],
        can_subscribe=(not ctx.preview) and listing.status in E.SUBSCRIBABLE_STATUSES and L.remaining_amount(listing, committed) > 0 and (not ppm_access["managed"] or ppm_access["current"]),
        financials=L.partner_financials(listing),
    )


@router.get("/listings/{ref}/investor-returns", response_model=InvestorReturnsRead)
async def listing_investor_returns(ref: str, ctx: ClearedMember, db: Db,
    query: Annotated[InvestorReturnsQuery, Depends()]) -> InvestorReturnsRead:
    from app.services.investor_returns import read_listing_projection

    from .services.ppm import bind_access_state

    listing = await _listing_or_404(db, ref)
    access = await bind_access_state(db, listing)
    if access["managed"] and not access["current"]:
        raise HTTPException(409, {"code": "offering_update_pending_publication",
            "message": "The offering update awaits publication. Retry after the desk publishes its reviewed update."})
    return InvestorReturnsRead(**await read_listing_projection(listing, query=query, member=True))


@router.post("/listings/{ref}/documents/{document_id}/url", response_model=S.DocumentUrlOut)
async def document_url(ref: str, document_id: uuid.UUID, ctx: ClearedMember, request: Request, db: Db) -> S.DocumentUrlOut:
    listing = await _listing_or_404(db, ref)
    from .services.ppm import bind_access_state
    await bind_access_state(db, listing)
    doc = next((d for d in L.ready_documents(listing) if d.id == document_id), None)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    url = storage.presign_get(doc.s3_key, content_type=doc.content_type, download_filename=doc.name)
    if url is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Document storage is not available right now.")
    db.add(
        CpDocumentAccess(
            member_id=ctx.member.id if ctx.member else None, actor_user_id=ctx.user.id, listing_id=listing.id, document_id=doc.id,
            accessed_at=L.now_utc(), ip_address=(client_ip(request) or "")[:64] or None, user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        )
    )
    await db.flush()
    return S.DocumentUrlOut(url=url, expires_in_seconds=storage.DOCUMENT_URL_TTL_SECONDS)


@router.get("/listings/{ref}/file-documents")
async def file_documents(ref: str, ctx: ClearedMember, db: Db) -> dict[str, Any]:
    from .services import document_catalog

    listing = await _listing_or_404(db, ref)
    return await document_catalog.catalog(db, listing, member_id=ctx.member.id if ctx.member else None)


@router.post("/listings/{ref}/file-documents/{file_id}/url", response_model=S.DocumentUrlOut)
async def original_document_url(ref: str, file_id: uuid.UUID, ctx: ClearedMember, request: Request, db: Db, download: bool = False) -> S.DocumentUrlOut:
    from .services import document_catalog

    listing = await _listing_or_404(db, ref)
    file = await document_catalog.authorized_original(db, listing, file_id, member_id=ctx.member.id if ctx.member else None)
    url = storage.presign_get(file.s3_key, content_type=file.content_type, download_filename=file.file_name,
        disposition="attachment" if download else "inline", version_id=file.s3_version_id)
    if url is None:
        raise HTTPException(503, "Document storage is not available right now.")
    await L.log_event(db, listing.id, "source_document.accessed", actor_user_id=ctx.user.id,
        detail={"file_id": str(file.id), "member_id": str(ctx.member.id), "download": download,
            "ip_address": (client_ip(request) or "")[:64], "user_agent": (request.headers.get("user-agent") or "")[:400]})
    return S.DocumentUrlOut(url=url, expires_in_seconds=storage.DOCUMENT_URL_TTL_SECONDS)


@router.post("/listings/{ref}/subscriptions", response_model=S.SubscriptionOut, status_code=status.HTTP_201_CREATED)
async def subscribe(ref: str, payload: S.SubscriptionIn, ctx: SubscribingMember, request: Request, db: Db) -> S.SubscriptionOut:
    listing = await _listing_or_404(db, ref)
    try:
        row = await L.create_subscription(
            db, listing=listing, member=ctx.member, entity_id=payload.entity_id, amount=payload.amount,
            ack_ppm=payload.ack_ppm, ack_accredited=payload.ack_accredited,
            ip_address=(client_ip(request) or "")[:64] or None, user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        )
    except L.RuleError as exc:
        raise _rule(exc) from exc
    settings_row = await M.issuer_settings(db)
    recipients = [e for e in (settings_row.notify_emails or []) if e]
    if recipients:
        await emails.subscription_received_desk(
            db, recipients=recipients, ref=listing.ref, member_name=ctx.member.display_name or ctx.user.email, amount=f"${payload.amount:,.2f}"
        )
    out = S.SubscriptionOut.model_validate(row).model_dump()
    out.update(listing_ref=listing.ref, listing_name=listing.name, entity_name=_entity_name(ctx.member, row.entity_id))
    return S.SubscriptionOut(**out)


@router.get("/subscriptions", response_model=list[S.SubscriptionOut])
async def my_subscriptions(ctx: ClearedMember, db: Db) -> list[S.SubscriptionOut]:
    if ctx.member is None:
        return []
    rows = (
        await db.execute(
            select(CpSubscription, CpListing).join(CpListing, CpListing.id == CpSubscription.listing_id)
            .where(CpSubscription.member_id == ctx.member.id).order_by(CpSubscription.requested_at.desc())
        )
    ).all()
    out = []
    for sub, listing in rows:
        d = S.SubscriptionOut.model_validate(sub).model_dump()
        d.update(listing_ref=listing.ref, listing_name=listing.name, entity_name=_entity_name(ctx.member, sub.entity_id))
        out.append(S.SubscriptionOut(**d))
    return out


# ---------------------------------------------------------------------------
# Portfolio, sold, secondary
# ---------------------------------------------------------------------------


async def _holdings(db: AsyncSession, member_id: uuid.UUID, entities: list[CpMemberEntity]) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(CpParticipation, CpListing).join(CpListing, CpListing.id == CpParticipation.listing_id)
            .where(CpParticipation.member_id == member_id).order_by(CpParticipation.funded_at.desc().nullslast())
        )
    ).all()
    names = {e.id: e.name for e in entities}
    out = []
    for part, listing in rows:
        active = await L.active_transfer_for(db, part.id)
        out.append(
            {
                "id": part.id, "listing_id": listing.id, "listing_ref": listing.ref, "listing_name": listing.name,
                "vertical": listing.vertical, "vertical_label": E.VERTICAL_LABELS.get(listing.vertical, listing.vertical),
                "note_rate_pct": listing.note_rate_pct, "entity_id": part.entity_id, "entity_name": names.get(part.entity_id),
                "invested_amount": part.invested_amount, "principal_outstanding": part.principal_outstanding,
                "interest_received_ytd": part.interest_received_ytd, "interest_received_total": part.interest_received_total,
                "next_payment_on": part.next_payment_on, "servicer_status": part.servicer_status, "status_as_of": part.status_as_of,
                "funded_at": part.funded_at, "closed_at": part.closed_at, "maturity_date": listing.maturity_date,
                "transferable": L._d(part.principal_outstanding) > 0 and active is None,
            }
        )
    return out


@router.get("/portfolio", response_model=S.PortfolioOut)
async def portfolio(ctx: ClearedMember, db: Db) -> S.PortfolioOut:
    if ctx.member is None:
        return S.PortfolioOut(stats=S.PortfolioStats(invested=L.ZERO, outstanding=L.ZERO, interest_ytd=L.ZERO, active=0), holdings=[], entities=[], as_of=L.now_utc().date())
    holdings = await _holdings(db, ctx.member.id, ctx.member.entities)
    return S.PortfolioOut(
        stats=S.PortfolioStats(
            invested=sum((L._d(h["invested_amount"]) for h in holdings), L.ZERO),
            outstanding=sum((L._d(h["principal_outstanding"]) for h in holdings), L.ZERO),
            interest_ytd=sum((L._d(h["interest_received_ytd"]) for h in holdings), L.ZERO),
            active=sum(1 for h in holdings if L._d(h["principal_outstanding"]) > 0),
        ),
        holdings=[S.ParticipationOut(**h) for h in holdings],
        entities=[S.EntityOut.model_validate(e) for e in ctx.member.entities],
        as_of=max([h["status_as_of"] for h in holdings if h["status_as_of"]] or [L.now_utc().date()]),
    )


@router.get("/portfolio.csv")
async def portfolio_csv(ctx: ClearedMember, db: Db) -> StreamingResponse:
    holdings = await _holdings(db, ctx.member.id, ctx.member.entities) if ctx.member else []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Loan", "Reference", "Entity", "Invested", "Outstanding", "Rate", "Interest YTD", "Next payment", "Servicer status", "As of"])
    for h in holdings:
        w.writerow([h["listing_name"], h["listing_ref"], h["entity_name"] or "", h["invested_amount"], h["principal_outstanding"], h["note_rate_pct"], h["interest_received_ytd"], h["next_payment_on"] or "", h["servicer_status"], h["status_as_of"] or ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="holdings.csv"'})


@router.get("/sold", response_model=S.SoldOut)
async def sold(ctx: ClearedMember, db: Db) -> S.SoldOut:
    return S.SoldOut(**await L.sold(db))


async def _transfer_rows(db: AsyncSession, member_id: uuid.UUID | None) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(CpTransfer, CpListing).join(CpListing, CpListing.id == CpTransfer.listing_id)
            .where(CpTransfer.status.in_(["listed", "requested", "pending_consent", "approved"]))
            .order_by(CpTransfer.listed_at.desc())
        )
    ).all()
    out = []
    for t, listing in rows:
        is_mine = member_id is not None and t.seller_member_id == member_id
        out.append(
            {
                "id": t.id, "ref": t.ref, "listing_id": listing.id, "listing_ref": listing.ref, "listing_name": listing.name,
                "vertical": listing.vertical, "vertical_label": E.VERTICAL_LABELS.get(listing.vertical, listing.vertical),
                "principal_amount": t.principal_amount, "note_rate_pct": listing.note_rate_pct,
                "remaining_term_months": L.remaining_term_months(listing), "ask_pct": t.ask_pct,
                "implied_yield_pct": L.implied_yield(listing.note_rate_pct, t.ask_pct), "listed_at": t.listed_at, "status": t.status,
                "is_mine": is_mine, "can_request": (not is_mine) and t.status == E.TransferStatus.LISTED and member_id is not None,
            }
        )
    return out


@router.get("/transfers", response_model=list[S.TransferOut])
async def transfers(ctx: ClearedMember, db: Db) -> list[S.TransferOut]:
    return [S.TransferOut(**t) for t in await _transfer_rows(db, ctx.member.id if ctx.member else None)]


@router.post("/transfers", response_model=S.TransferOut, status_code=status.HTTP_201_CREATED)
async def list_participation(payload: S.TransferIn, ctx: SubscribingMember, db: Db) -> S.TransferOut:
    part = await db.get(CpParticipation, payload.participation_id)
    if part is None or part.member_id != ctx.member.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Participation not found.")
    try:
        row = await L.list_participation(db, participation=part, member=ctx.member, principal_amount=payload.principal_amount, ask_pct=payload.ask_pct)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    t = next(t for t in await _transfer_rows(db, ctx.member.id) if t["id"] == row.id)
    return S.TransferOut(**t)


@router.post("/transfers/{transfer_id}/request", response_model=S.TransferOut)
async def request_transfer(transfer_id: uuid.UUID, ctx: SubscribingMember, db: Db) -> S.TransferOut:
    t = await db.get(CpTransfer, transfer_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found.")
    try:
        await L.request_transfer(db, transfer=t, buyer=ctx.member)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    listing = await db.get(CpListing, t.listing_id)
    settings_row = await M.issuer_settings(db)
    recipients = [e for e in (settings_row.notify_emails or []) if e]
    if recipients and listing is not None:
        await emails.transfer_requested_desk(db, recipients=recipients, transfer_ref=t.ref, listing_ref=listing.ref)
    row = next(x for x in await _transfer_rows(db, ctx.member.id) if x["id"] == t.id)
    return S.TransferOut(**row)


@router.post("/transfers/{transfer_id}/withdraw", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_transfer(transfer_id: uuid.UUID, ctx: SubscribingMember, db: Db) -> None:
    t = await db.get(CpTransfer, transfer_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found.")
    try:
        await L.withdraw_transfer(db, transfer=t, member=ctx.member)
    except L.RuleError as exc:
        raise _rule(exc) from exc
    return None


__all__ = ["router", "selectinload"]
