"""Field Desk CRM, catalog, product discovery, and presentation endpoints."""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.user import User
from app.services.email import ses_client

from .crm_schemas import (
    CompanyContactIn,
    ContactAssignmentIn,
    FinderAnswersIn,
    FundingGoalConfirmIn,
    ProductCatalogUpdate,
    ProductPresentationIn,
)
from .deps import require_super_admin, require_team_or_rep
from .models import (
    DealerApplicationContact,
    DealerBusiness,
    DealerProductCatalog,
    DealerProductFinderSession,
    DealerProductPresentation,
    DealerProductScreeningSnapshot,
    DealerRepCompany,
    DealerRepContact,
    DealerRepContactAssignment,
    DealerRepInboxMessage,
    DealerRepInboxThread,
    DealerRepLead,
    DealerSourceConnection,
)
from .services import buckets_link
from .services.product_finder import QUESTIONS, screen_products
from .services.targets import propose_targets

router = APIRouter(prefix="/dealer-os", tags=["dealer-os-crm"])

_CATALOG_SUMMARIES = {
    "term_loan_3_5_year": {"en": "Structured working capital or debt refinance with predictable monthly payments.", "es": "Capital de trabajo o refinanciamiento de deuda con pagos mensuales predecibles."},
    "term_loan_10_year": {"en": "Long-term, unsecured working capital for qualified small businesses.", "es": "Capital de trabajo a largo plazo y sin garantía para pequeños negocios calificados."},
    "line_of_credit": {"en": "Reusable working-capital access for seasonal and operating needs.", "es": "Acceso reutilizable a capital de trabajo para necesidades estacionales y operativas."},
    "term_loan_loc_hybrid": {"en": "A structured term facility combined with flexible revolving access.", "es": "Un préstamo estructurado combinado con acceso rotativo flexible."},
    "equipment_financing": {"en": "Asset-backed financing for vehicles, machinery, and business equipment.", "es": "Financiamiento respaldado por activos para vehículos, maquinaria y equipo comercial."},
    "jumbo_term_loan": {"en": "Larger structured capital for established businesses and complex transactions.", "es": "Capital estructurado de mayor tamaño para negocios establecidos y transacciones complejas."},
    "transportation_finance": {"en": "Equipment and operating capital tailored to transportation businesses.", "es": "Equipo y capital operativo adaptado a empresas de transporte."},
    "sba": {"en": "Government-backed financing for acquisitions, expansion, real estate, and working capital.", "es": "Financiamiento respaldado por el gobierno para adquisiciones, expansión, bienes raíces y capital de trabajo."},
    "sba_grocery": {"en": "SBA-oriented financing for grocery and food-market operators.", "es": "Financiamiento SBA para operadores de supermercados y mercados de alimentos."},
    "sba_made_in_america": {"en": "SBA-oriented capital supporting eligible domestic manufacturing investment.", "es": "Capital SBA para apoyar inversiones elegibles de manufactura nacional."},
}


def _can_view_contact(user: User, contact: DealerRepContact) -> bool:
    return user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC} or contact.owner_user_id == user.id


async def _contact_access_filter(user: User):
    if user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        return True
    return or_(
        DealerRepContact.owner_user_id == user.id,
        exists(select(DealerRepContactAssignment.id).where(
            DealerRepContactAssignment.contact_id == DealerRepContact.id,
            DealerRepContactAssignment.user_id == user.id,
        )),
    )


async def _load_contact(db: AsyncSession, user: User, contact_id: UUID) -> DealerRepContact:
    require_team_or_rep(user)
    row = (await db.execute(select(DealerRepContact).where(
        DealerRepContact.id == contact_id, await _contact_access_filter(user)
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return row


async def _next_case_ref(db: AsyncSession) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"QC-{year}-"
    top = (await db.execute(select(func.max(DealerBusiness.case_ref)).where(
        DealerBusiness.case_ref.like(f"{prefix}%")
    ))).scalar_one_or_none()
    try:
        number = int(str(top).rsplit("-", 1)[1]) if top else 0
    except (IndexError, ValueError):
        number = 0
    return f"{prefix}{number + 1:05d}"


def _catalog_item(row: DealerProductCatalog, locale: str) -> dict:
    language = "es" if locale == "es" else "en"
    copy = (row.copy or {}).get(language) or (row.copy or {}).get("en") or {}
    disclosure = (row.disclosures or {}).get(language) or (row.disclosures or {}).get("en")
    pricing = (row.pricing or {}).get(language) or (row.pricing or {}).get("en")
    return {
        "id": str(row.id), "program_key": row.program_key, "version": row.version,
        "category": row.category, "name": copy.get("name", row.program_key.replace("_", " ").title()),
        "summary": copy.get("summary") or _CATALOG_SUMMARIES.get(row.program_key, {}).get(language), "highlights": copy.get("highlights", []),
        "pricing": pricing, "disclosure": disclosure,
        "amount_min": float(row.amount_min) if row.amount_min is not None else None,
        "amount_max": float(row.amount_max) if row.amount_max is not None else None,
        "term_min_months": row.term_min_months, "term_max_months": row.term_max_months,
        "effective_at": row.effective_at, "active": row.active,
    }


def _render_catalog_pdf(rows: list[DealerProductCatalog], locale: str) -> bytes:
    from weasyprint import HTML

    cards = "".join(
        f"<section><h2>{_catalog_item(row, locale)['name']}</h2>"
        f"<p>{_catalog_item(row, locale).get('summary') or ''}</p>"
        f"<b>${float(row.amount_min or 0):,.0f}–${float(row.amount_max or 0):,.0f}</b>"
        f"<p>{_catalog_item(row, locale).get('pricing') or ''}</p></section>"
        for row in rows
    )
    html = (
        "<style>@page{size:Letter;margin:.65in}body{font:14px Arial;color:#101828}"
        "h1{color:#174b84}section{border:1px solid #d8e0ea;padding:18px;margin:14px 0;border-radius:8px}"
        "small{color:#667085}</style>"
        f"<h1>{'Catálogo de financiamiento' if locale == 'es' else 'Funding program catalog'}</h1>"
        f"{cards}<small>{'Evaluación preliminar; no es un compromiso de préstamo.' if locale == 'es' else 'Preliminary fit only; not a commitment to lend.'}</small>"
    )
    return HTML(string=html).write_pdf()


@router.get("/products")
async def list_products(
    user: CurrentUser, db: AsyncSession = Depends(get_db), locale: str = Query("en", pattern="^(en|es)$"),
    q: str = Query("", max_length=120), category: str = Query("all", max_length=48),
) -> dict:
    require_team_or_rep(user)
    stmt = select(DealerProductCatalog).where(DealerProductCatalog.active.is_(True))
    if category != "all": stmt = stmt.where(DealerProductCatalog.category == category)
    rows = (await db.execute(stmt.order_by(DealerProductCatalog.sort_order, DealerProductCatalog.program_key))).scalars().all()
    items = [_catalog_item(row, locale) for row in rows]
    needle = q.strip().lower()
    if needle:
        items = [item for item in items if needle in f"{item['name']} {item['category']} {item.get('summary') or ''}".lower()]
    return {"items": items, "questions": [{**question, "label": question[locale]} for question in QUESTIONS]}


@router.patch("/products/{program_key}")
async def update_product(program_key: str, payload: ProductCatalogUpdate, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    require_super_admin(user)
    row = (await db.execute(select(DealerProductCatalog).where(
        DealerProductCatalog.program_key == program_key,
        DealerProductCatalog.active.is_(True),
    ).order_by(DealerProductCatalog.version.desc()))).scalars().first()
    if row is None: raise HTTPException(status.HTTP_404_NOT_FOUND, "Program not found")
    for key, value in payload.model_dump(exclude_unset=True).items(): setattr(row, key, value)
    row.updated_by_user_id = user.id
    await db.commit(); await db.refresh(row)
    return _catalog_item(row, "en")


async def _find_or_create_company_contact(db: AsyncSession, user: User, payload: CompanyContactIn) -> tuple[DealerRepCompany, DealerRepContact]:
    email = payload.email.strip().lower() if payload.email else None
    contact = None
    if email:
        contact = (await db.execute(select(DealerRepContact).where(
            DealerRepContact.owner_user_id == user.id, DealerRepContact.email == email
        ).order_by(DealerRepContact.updated_at.desc()))).scalars().first()
    company = await db.get(DealerRepCompany, contact.company_id) if contact and contact.company_id else None
    if company is None:
        company = (await db.execute(select(DealerRepCompany).where(
            DealerRepCompany.owner_user_id == user.id,
            func.lower(DealerRepCompany.name) == payload.company_name.strip().lower(),
        ).order_by(DealerRepCompany.updated_at.desc()))).scalars().first()
    if company is None:
        company = DealerRepCompany(owner_user_id=user.id, name=payload.company_name.strip(), industry=payload.industry,
            address=payload.address, city=payload.city, state=payload.state, zip=payload.zip)
        db.add(company); await db.flush()
    if contact is None:
        contact = DealerRepContact(owner_user_id=user.id, company_id=company.id, full_name=payload.contact_name.strip(),
            company=company.name, email=email, phone_e164=payload.phone, source="product_finder", last_activity_at=datetime.now(timezone.utc))
        db.add(contact); await db.flush()
    else:
        contact.company_id = contact.company_id or company.id
        contact.company = company.name; contact.phone_e164 = payload.phone or contact.phone_e164
        contact.last_activity_at = datetime.now(timezone.utc)
    return company, contact


@router.post("/product-finder/sessions", status_code=status.HTTP_201_CREATED)
async def create_finder_session(payload: CompanyContactIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    require_team_or_rep(user)
    company, contact = await _find_or_create_company_contact(db, user, payload)
    existing = (await db.execute(select(DealerProductFinderSession).where(
        DealerProductFinderSession.owner_user_id == user.id,
        DealerProductFinderSession.contact_id == contact.id,
        DealerProductFinderSession.status.in_(["screening", "draft"]),
    ).order_by(DealerProductFinderSession.updated_at.desc()))).scalars().first()
    if existing:
        return {"id": str(existing.id), "dealer_id": str(existing.dealer_id), "contact_id": str(contact.id),
            "company_id": str(company.id), "answers": existing.answers, "result": existing.current_result,
            "client_requested_amount": float(existing.client_requested_amount or 0), "reused": True}
    dealer = DealerBusiness(name=company.name, legal_name=company.name, email=contact.email, phone=contact.phone_e164,
        address=company.address, city=company.city, state=company.state, zip=company.zip, industry=payload.industry or "other",
        entity_type="unknown", funding_goal=Decimal(str(payload.requested_amount)),
        client_requested_amount=Decimal(str(payload.requested_amount)), funding_purpose="other",
        use_of_proceeds_note=payload.use_of_funds, owner_user_id=user.id, case_ref=await _next_case_ref(db),
        application_lifecycle="draft", status="draft")
    db.add(dealer); await db.flush()
    contact.dealer_id = contact.dealer_id or dealer.id
    db.add(DealerApplicationContact(dealer_id=dealer.id, contact_id=contact.id, relationship="prospect", is_primary=True))
    answers = {"requested_amount": payload.requested_amount, "use_of_funds": payload.use_of_funds, "industry": payload.industry}
    session = DealerProductFinderSession(owner_user_id=user.id, company_id=company.id, contact_id=contact.id,
        dealer_id=dealer.id, locale=payload.locale, answers=answers, client_requested_amount=Decimal(str(payload.requested_amount)))
    db.add(session); await db.commit(); await db.refresh(session)
    return {"id": str(session.id), "dealer_id": str(dealer.id), "contact_id": str(contact.id), "company_id": str(company.id),
        "answers": answers, "result": None, "client_requested_amount": payload.requested_amount, "reused": False}


async def _load_session(db: AsyncSession, user: User, session_id: UUID) -> DealerProductFinderSession:
    row = await db.get(DealerProductFinderSession, session_id)
    if row is None or (user.role == Role.FIELD_REP and row.owner_user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Screening session not found")
    require_team_or_rep(user); return row


@router.post("/product-finder/sessions/{session_id}/screen")
async def screen_session(session_id: UUID, payload: FinderAnswersIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    session = await _load_session(db, user, session_id)
    answers = {**(session.answers or {}), **payload.answers}
    result = screen_products(answers, session.locale)
    session.answers = answers; session.current_result = result
    session.recommended_amount = Decimal(str(result["recommended_amount"])) if result.get("recommended_amount") else None
    db.add(DealerProductScreeningSnapshot(session_id=session.id, source="self_reported", inputs=answers, result=result, created_by_user_id=user.id))
    await db.commit()
    return {"id": str(session.id), "answers": answers, "result": result}


@router.post("/product-finder/sessions/{session_id}/confirm-funding-goal")
async def confirm_funding_goal(session_id: UUID, payload: FundingGoalConfirmIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    session = await _load_session(db, user, session_id); dealer = await db.get(DealerBusiness, session.dealer_id)
    if dealer is None: raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft file not found")
    maximum = float(session.recommended_amount or 0)
    if maximum and payload.amount > maximum: raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Confirmed amount exceeds the screened maximum")
    dealer.funding_goal = Decimal(str(payload.amount)); session.funding_goal_confirmed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"client_requested_amount": float(dealer.client_requested_amount or 0), "funding_goal": float(dealer.funding_goal or 0)}


@router.post("/product-finder/sessions/{session_id}/start-application")
async def start_application(session_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    session = await _load_session(db, user, session_id); dealer = await db.get(DealerBusiness, session.dealer_id)
    if dealer is None: raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft file not found")
    if dealer.application_lifecycle == "draft":
        dealer.application_lifecycle = "active"; dealer.status = "active"; session.status = "promoted"
        db.add(DealerSourceConnection(dealer_id=dealer.id, kind="uploads", status="active"))
        await propose_targets(db, dealer)
        try: await buckets_link.ensure_bucket(db, dealer)
        except Exception: pass
        if user.role == Role.FIELD_REP:
            db.add(DealerRepLead(dealer_id=dealer.id, rep_user_id=user.id, status="draft", status_history=[]))
        await db.commit()
    return {"dealer_id": str(dealer.id), "route": f"/applications/{dealer.id}?step=1"}


@router.get("/contacts")
async def list_contacts(user: CurrentUser, db: AsyncSession = Depends(get_db), q: str = Query("", max_length=160), limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)) -> dict:
    require_team_or_rep(user); filters = [await _contact_access_filter(user)]
    if q.strip():
        like = f"%{q.strip().lower()}%"; filters.append(or_(func.lower(DealerRepContact.full_name).like(like), func.lower(func.coalesce(DealerRepContact.company, "")).like(like), func.lower(func.coalesce(DealerRepContact.email, "")).like(like), func.lower(func.coalesce(DealerRepContact.phone_e164, "")).like(like)))
    total = int((await db.execute(select(func.count()).select_from(DealerRepContact).where(*filters))).scalar_one())
    rows = (await db.execute(select(DealerRepContact).where(*filters).order_by(DealerRepContact.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": str(row.id), "company_id": str(row.company_id) if row.company_id else None, "name": row.full_name, "company": row.company, "email": row.email, "phone": row.phone_e164, "source": row.source, "updated_at": row.updated_at} for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/companies")
async def list_companies(user: CurrentUser, db: AsyncSession = Depends(get_db), q: str = Query("", max_length=160), limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)) -> dict:
    require_team_or_rep(user)
    filters = [] if user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC} else [or_(
        DealerRepCompany.owner_user_id == user.id,
        exists(select(DealerRepContactAssignment.id).join(
            DealerRepContact, DealerRepContact.id == DealerRepContactAssignment.contact_id
        ).where(DealerRepContact.company_id == DealerRepCompany.id, DealerRepContactAssignment.user_id == user.id)),
    )]
    if q.strip():
        like = f"%{q.strip().lower()}%"
        filters.append(or_(func.lower(DealerRepCompany.name).like(like), func.lower(func.coalesce(DealerRepCompany.address, "")).like(like), func.lower(func.coalesce(DealerRepCompany.city, "")).like(like)))
    total = int((await db.execute(select(func.count()).select_from(DealerRepCompany).where(*filters))).scalar_one())
    rows = (await db.execute(select(DealerRepCompany).where(*filters).order_by(DealerRepCompany.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": str(row.id), "name": row.name, "industry": row.industry, "address": row.address, "city": row.city, "state": row.state, "status": row.status, "updated_at": row.updated_at} for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/companies/{company_id}")
async def company_detail(company_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    require_team_or_rep(user)
    company = await db.get(DealerRepCompany, company_id)
    shared = False
    if company is not None and user.role == Role.FIELD_REP:
        shared = bool((await db.execute(select(exists(select(DealerRepContactAssignment.id).join(
            DealerRepContact, DealerRepContact.id == DealerRepContactAssignment.contact_id
        ).where(DealerRepContact.company_id == company.id, DealerRepContactAssignment.user_id == user.id))))).scalar_one())
    if company is None or (user.role == Role.FIELD_REP and company.owner_user_id != user.id and not shared):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    contacts = (await db.execute(select(DealerRepContact).where(DealerRepContact.company_id == company.id).order_by(DealerRepContact.updated_at.desc()))).scalars().all()
    return {"id": str(company.id), "name": company.name, "industry": company.industry, "address": company.address, "city": company.city, "state": company.state, "zip": company.zip, "contacts": [{"id": str(row.id), "name": row.full_name, "email": row.email, "phone": row.phone_e164} for row in contacts]}


@router.get("/contacts/{contact_id}")
async def contact_detail(contact_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    contact = await _load_contact(db, user, contact_id)
    applications = (await db.execute(select(DealerBusiness).join(DealerApplicationContact, DealerApplicationContact.dealer_id == DealerBusiness.id).where(DealerApplicationContact.contact_id == contact.id).order_by(DealerBusiness.updated_at.desc()))).scalars().all()
    sessions = (await db.execute(select(DealerProductFinderSession).where(DealerProductFinderSession.contact_id == contact.id).order_by(DealerProductFinderSession.updated_at.desc()))).scalars().all()
    presentations = (await db.execute(select(DealerProductPresentation).where(DealerProductPresentation.contact_id == contact.id).order_by(DealerProductPresentation.created_at.desc()))).scalars().all()
    threads = (await db.execute(select(DealerRepInboxThread).where(DealerRepInboxThread.contact_id == contact.id).order_by(DealerRepInboxThread.updated_at.desc()))).scalars().all()
    return {"id": str(contact.id), "name": contact.full_name, "company": contact.company, "email": contact.email, "phone": contact.phone_e164,
        "applications": [{"id": str(row.id), "name": row.name, "case_ref": row.case_ref, "lifecycle": row.application_lifecycle, "status": row.status, "funding_goal": float(row.funding_goal or 0), "updated_at": row.updated_at} for row in applications],
        "sessions": [{"id": str(row.id), "status": row.status, "result": row.current_result, "updated_at": row.updated_at} for row in sessions],
        "presentations": [{"id": str(row.id), "program_keys": row.program_keys, "locale": row.locale, "channel": row.channel, "status": row.delivery_status, "created_at": row.created_at} for row in presentations],
        "threads": [{"id": str(row.id), "subject": row.subject, "channel": row.channel, "unread_count": row.unread_count, "updated_at": row.updated_at} for row in threads]}


@router.post("/contacts/{contact_id}/assignments", status_code=status.HTTP_201_CREATED)
async def assign_contact(contact_id: UUID, payload: ContactAssignmentIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    contact = await _load_contact(db, user, contact_id)
    if user.role == Role.FIELD_REP and contact.owner_user_id != user.id: raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owning rep can share this contact")
    existing = (await db.execute(select(DealerRepContactAssignment).where(DealerRepContactAssignment.contact_id == contact.id, DealerRepContactAssignment.user_id == payload.user_id))).scalar_one_or_none()
    if existing is None: db.add(DealerRepContactAssignment(contact_id=contact.id, user_id=payload.user_id, assigned_by_user_id=user.id)); await db.commit()
    return {"assigned": True}


def _normalize_subject(subject: str) -> str:
    value = subject.strip().lower()
    while re.match(r"^(re|fw|fwd)\s*:", value): value = re.sub(r"^(re|fw|fwd)\s*:\s*", "", value)
    return " ".join(value.split())[:200]


@router.post("/product-presentations", status_code=status.HTTP_201_CREATED)
async def present_products(payload: ProductPresentationIn, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    contact = await _load_contact(db, user, payload.contact_id)
    subject = payload.subject or ("Opciones de financiamiento" if payload.locale == "es" else "Funding options")
    thread = None; delivery = "presented"
    if payload.channel in {"email", "sms"}:
        if payload.channel == "email" and not contact.email: raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Contact email is required")
        if payload.channel == "sms" and not contact.phone_e164: raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Contact phone is required")
        filters = [DealerRepInboxThread.contact_id == contact.id, DealerRepInboxThread.channel == payload.channel, DealerRepInboxThread.status == "open"]
        if payload.channel == "email": filters.append(DealerRepInboxThread.subject_key == _normalize_subject(subject))
        thread = (await db.execute(select(DealerRepInboxThread).where(*filters).order_by(DealerRepInboxThread.updated_at.desc()))).scalars().first()
        if thread is None:
            thread = DealerRepInboxThread(owner_user_id=contact.owner_user_id, contact_id=contact.id, dealer_id=contact.dealer_id, subject=subject, subject_key=_normalize_subject(subject) if payload.channel == "email" else None, channel=payload.channel, source="product_presentation", last_message_at=datetime.now(timezone.utc)); db.add(thread); await db.flush()
        body = payload.message or ("Attached are the funding options we discussed." if payload.locale == "en" else "Adjuntamos las opciones de financiamiento que conversamos.")
        if payload.channel == "email":
            catalog_rows = (await db.execute(select(DealerProductCatalog).where(
                DealerProductCatalog.program_key.in_(payload.program_keys),
                DealerProductCatalog.active.is_(True),
            ).order_by(DealerProductCatalog.sort_order))).scalars().all()
            pdf = await run_in_threadpool(_render_catalog_pdf, list(catalog_rows), payload.locale)
            result = await run_in_threadpool(
                ses_client.send_raw_email,
                to_emails=[contact.email], subject=subject, body_text=body,
                attachments=[(f"qc-funding-options-{payload.locale}.pdf", pdf, "application/pdf")],
            )
            delivery = "sent" if result.ok else "failed"
        else: delivery = "queued"
        db.add(DealerRepInboxMessage(thread_id=thread.id, owner_user_id=contact.owner_user_id, contact_id=contact.id, dealer_id=contact.dealer_id, direction="outbound", channel=payload.channel, subject=subject if payload.channel == "email" else None, body=body, delivery_status=delivery, sender=user.email, recipient=contact.email if payload.channel == "email" else contact.phone_e164, read_at=datetime.now(timezone.utc)))
    row = DealerProductPresentation(owner_user_id=user.id, company_id=contact.company_id, contact_id=contact.id, dealer_id=contact.dealer_id, session_id=payload.session_id, program_keys=payload.program_keys, locale=payload.locale, channel=payload.channel, delivery_status=delivery, inbox_thread_id=thread.id if thread else None)
    db.add(row); await db.commit(); await db.refresh(row)
    return {"id": str(row.id), "delivery_status": delivery, "thread_id": str(thread.id) if thread else None}


@router.get("/products/pdf")
async def products_pdf(user: CurrentUser, db: AsyncSession = Depends(get_db), keys: str = Query(...), locale: str = Query("en", pattern="^(en|es)$")):
    require_team_or_rep(user); wanted = [key for key in keys.split(",") if key][:8]
    rows = (await db.execute(select(DealerProductCatalog).where(DealerProductCatalog.program_key.in_(wanted), DealerProductCatalog.active.is_(True)).order_by(DealerProductCatalog.sort_order))).scalars().all()
    try:
        content = await run_in_threadpool(_render_catalog_pdf, list(rows), locale)
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PDF generation unavailable: {exc}") from exc
    return StreamingResponse(io.BytesIO(content), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="qc-product-catalog-{locale}.pdf"'})
