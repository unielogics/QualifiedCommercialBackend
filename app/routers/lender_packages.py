"""Secure multi-lender package delivery and lender terms.

Operator routes are loan-scoped. Lender routes are scoped to the
authenticated lender user's linked lender roster rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import DocStatus, EmailDraftStatus, LoanStage, Role
from app.models.activity import Activity
from app.models.document import Document
from app.models.email_draft import EmailDraft
from app.models.lender import Lender
from app.models.lender_package import (
    LenderPackage,
    LenderPackageDocument,
    LenderPackageEvent,
    LenderPackageRecipient,
    LenderTerm,
    LenderUser,
)
from app.models.loan import Loan
from app.models.user import User
from app.schemas.lender_package import (
    LenderDownloadResponse,
    LenderPackageCreate,
    LenderPackageDocumentRead,
    LenderPackageEventRead,
    LenderPackageRead,
    LenderPackageRecipientRead,
    LenderPackageRevoke,
    LenderPortalPackageListItem,
    LenderTermFields,
    LenderTermManualCreate,
    LenderTermRead,
    LenderTermSelect,
    LenderTermUpdate,
)
from app.services.activity_log import mark_loan_dirty
from app.services.clerk import invite_user
from app.services.lender_connect import LenderConnectError, connect_lender

router = APIRouter(tags=["lender-packages"])

DOWNLOAD_URL_TTL_SECONDS = 60
SENDABLE_DOC_STATUSES = {DocStatus.RECEIVED, DocStatus.VERIFIED}
TERM_VALUE_FIELDS = set(LenderTermFields.model_fields)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_internal(user: User) -> None:
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal funding role required")


def _require_lender(user: User) -> None:
    if user.role != Role.LENDER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Lender portal role required")


def _frontend_url(path: str) -> str:
    base = get_settings().frontend_app_url.rstrip("/")
    return f"{base}{path if path.startswith('/') else '/' + path}"


def _s3():
    settings = get_settings()
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        region_name=settings.aws_region,
    )


def _effective_recipient_status(
    package: LenderPackage,
    recipient: LenderPackageRecipient,
) -> str:
    if package.revoked_at is not None or package.status == "revoked":
        return "revoked"
    if package.expires_at <= _now():
        return "expired"
    return recipient.status


def _term_value_fields(payload: LenderTermFields | LenderTermUpdate) -> dict:
    return {
        k: v
        for k, v in payload.model_dump(exclude_unset=True).items()
        if k in TERM_VALUE_FIELDS
    }


def _term_update_fields(payload: LenderTermUpdate) -> dict:
    return payload.model_dump(exclude_unset=True)


def _term_read(term: LenderTerm, lender_name: str | None) -> LenderTermRead:
    row = LenderTermRead.model_validate(term)
    row.lender_name = lender_name
    return row


async def _log_package_event(
    db: AsyncSession,
    *,
    package_id: UUID,
    event: str,
    actor_user_id: UUID | None = None,
    recipient_id: UUID | None = None,
    lender_id: UUID | None = None,
    detail: dict | None = None,
) -> None:
    db.add(
        LenderPackageEvent(
            package_id=package_id,
            recipient_id=recipient_id,
            lender_id=lender_id,
            actor_user_id=actor_user_id,
            event=event,
            detail=detail,
        )
    )


async def _ensure_lender_user(
    db: AsyncSession,
    *,
    lender: Lender,
    email: str,
    portal_url: str,
) -> User:
    user = (
        await db.execute(
            select(User).where(func.lower(User.email) == email.lower())
        )
    ).scalar_one_or_none()
    created = False
    if user is None:
        user = User(
            clerk_id=None,
            email=email,
            name=lender.contact_name or lender.name,
            role=Role.LENDER,
        )
        db.add(user)
        await db.flush()
        created = True

    link = (
        await db.execute(
            select(LenderUser).where(
                LenderUser.user_id == user.id,
                LenderUser.lender_id == lender.id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        link = LenderUser(
            user_id=user.id,
            lender_id=lender.id,
            email=email,
            is_active=True,
        )
        db.add(link)
    else:
        link.email = email
        link.is_active = True
    await db.flush()

    if created or user.clerk_id is None:
        await invite_user(
            email=email,
            name=user.name or lender.name,
            role=Role.LENDER,
            redirect_url=portal_url,
        )
    return user


async def _read_package(
    db: AsyncSession,
    package: LenderPackage,
    *,
    include_events: bool = False,
) -> LenderPackageRead:
    loan = (
        await db.execute(select(Loan).where(Loan.id == package.loan_id))
    ).scalar_one()

    doc_rows = (
        await db.execute(
            select(LenderPackageDocument, Document)
            .join(Document, Document.id == LenderPackageDocument.document_id)
            .where(LenderPackageDocument.package_id == package.id)
            .order_by(LenderPackageDocument.position.asc(), LenderPackageDocument.created_at.asc())
        )
    ).all()
    docs = [
        LenderPackageDocumentRead(
            id=pd.id,
            document_id=doc.id,
            display_name=pd.display_name,
            category=doc.category,
            status=str(doc.status.value if hasattr(doc.status, "value") else doc.status),
            received_on=doc.received_on,
            verified_at=doc.verified_at,
        )
        for pd, doc in doc_rows
    ]

    term_rows = (
        await db.execute(
            select(LenderTerm, Lender.name)
            .join(Lender, Lender.id == LenderTerm.lender_id)
            .where(LenderTerm.package_id == package.id)
            .order_by(LenderTerm.updated_at.desc())
        )
    ).all()
    term_by_recipient: dict[UUID, LenderTermRead] = {}
    for term, lender_name in term_rows:
        if term.package_recipient_id is not None and term.package_recipient_id not in term_by_recipient:
            term_by_recipient[term.package_recipient_id] = _term_read(term, lender_name)

    rec_rows = (
        await db.execute(
            select(LenderPackageRecipient, Lender.name)
            .join(Lender, Lender.id == LenderPackageRecipient.lender_id)
            .where(LenderPackageRecipient.package_id == package.id)
            .order_by(Lender.name.asc())
        )
    ).all()
    recipients = [
        LenderPackageRecipientRead(
            id=rec.id,
            package_id=rec.package_id,
            lender_id=rec.lender_id,
            lender_name=lender_name,
            email=rec.email,
            status=_effective_recipient_status(package, rec),
            email_draft_id=rec.email_draft_id,
            viewed_at=rec.viewed_at,
            downloaded_at=rec.downloaded_at,
            terms_submitted_at=rec.terms_submitted_at,
            no_quote_at=rec.no_quote_at,
            last_event_at=rec.last_event_at,
            term=term_by_recipient.get(rec.id),
            created_at=rec.created_at,
            updated_at=rec.updated_at,
        )
        for rec, lender_name in rec_rows
    ]

    events: list[LenderPackageEventRead] = []
    if include_events:
        event_rows = (
            await db.execute(
                select(LenderPackageEvent)
                .where(LenderPackageEvent.package_id == package.id)
                .order_by(LenderPackageEvent.occurred_at.desc())
                .limit(100)
            )
        ).scalars().all()
        events = [LenderPackageEventRead.model_validate(e) for e in event_rows]

    return LenderPackageRead(
        id=package.id,
        loan_id=package.loan_id,
        deal_id=loan.deal_id,
        address=loan.address,
        subject=package.subject,
        message=package.message,
        status=package.status,
        expires_at=package.expires_at,
        revoked_at=package.revoked_at,
        documents=docs,
        recipients=recipients,
        events=events,
        created_at=package.created_at,
        updated_at=package.updated_at,
    )


async def _lender_membership_ids(db: AsyncSession, user: User) -> set[UUID]:
    links = (
        await db.execute(
            select(LenderUser.lender_id).where(
                LenderUser.user_id == user.id,
                LenderUser.is_active.is_(True),
            )
        )
    ).scalars().all()
    return set(links)


async def _get_lender_recipient(
    db: AsyncSession,
    *,
    package_id: UUID,
    user: User,
) -> tuple[LenderPackage, LenderPackageRecipient]:
    _require_lender(user)
    lender_ids = await _lender_membership_ids(db, user)
    if not lender_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No active lender account link")

    row = (
        await db.execute(
            select(LenderPackage, LenderPackageRecipient)
            .join(LenderPackageRecipient, LenderPackageRecipient.package_id == LenderPackage.id)
            .where(
                LenderPackage.id == package_id,
                LenderPackageRecipient.lender_id.in_(lender_ids),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Package not found")
    package, recipient = row
    return package, recipient


def _assert_package_open(package: LenderPackage) -> None:
    if package.revoked_at is not None or package.status == "revoked":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Package access has been revoked")
    if package.expires_at <= _now():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Package access has expired")


async def _build_lender_portal_list_item(
    db: AsyncSession,
    *,
    package: LenderPackage,
    recipient: LenderPackageRecipient,
) -> LenderPortalPackageListItem:
    loan = (
        await db.execute(select(Loan).where(Loan.id == package.loan_id))
    ).scalar_one()
    return LenderPortalPackageListItem(
        id=package.id,
        loan_id=loan.id,
        deal_id=loan.deal_id,
        address=loan.address,
        subject=package.subject,
        status=package.status,
        recipient_status=_effective_recipient_status(package, recipient),
        expires_at=package.expires_at,
        viewed_at=recipient.viewed_at,
        terms_submitted_at=recipient.terms_submitted_at,
        created_at=package.created_at,
    )


@router.post("/loans/{loan_id}/lender-packages", response_model=LenderPackageRead, status_code=status.HTTP_201_CREATED)
async def create_lender_package(
    loan_id: UUID,
    payload: LenderPackageCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderPackageRead:
    _require_internal(user)
    loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    lenders = (
        await db.execute(select(Lender).where(Lender.id.in_(payload.lender_ids)))
    ).scalars().all()
    lender_by_id = {l.id: l for l in lenders}
    missing = [str(lid) for lid in payload.lender_ids if lid not in lender_by_id]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Lender not found: {', '.join(missing)}")

    docs = (
        await db.execute(
            select(Document).where(Document.loan_id == loan_id, Document.id.in_(payload.document_ids))
        )
    ).scalars().all()
    docs_by_id = {d.id: d for d in docs}
    selected_docs: list[Document] = []
    for did in payload.document_ids:
        doc = docs_by_id.get(did)
        if doc is None:
            continue
        if doc.status in SENDABLE_DOC_STATUSES:
            selected_docs.append(doc)
    if not selected_docs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick at least one received/verified document")

    subject = payload.subject or f"[QC-{loan.deal_id}] Secure lender package - {loan.address}"
    expires_at = _now() + timedelta(days=payload.expires_in_days)
    package = LenderPackage(
        loan_id=loan.id,
        created_by_id=user.id,
        subject=subject,
        message=payload.message,
        expires_at=expires_at,
    )
    db.add(package)
    await db.flush()

    for idx, doc in enumerate(selected_docs):
        db.add(
            LenderPackageDocument(
                package_id=package.id,
                document_id=doc.id,
                display_name=doc.name,
                position=idx,
            )
        )

    await _log_package_event(
        db,
        package_id=package.id,
        actor_user_id=user.id,
        event="package.created",
        detail={"document_count": len(selected_docs), "recipient_count": len(lenders)},
    )

    portal_url = _frontend_url(f"/lender/packages/{package.id}")
    for lender_id in payload.lender_ids:
        lender = lender_by_id[lender_id]
        if not lender.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Lender '{lender.name}' is inactive")
        email = lender.submission_email or lender.contact_email
        if not email:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Lender '{lender.name}' has no email")

        invited_user = await _ensure_lender_user(db, lender=lender, email=email, portal_url=portal_url)
        recipient = LenderPackageRecipient(
            package_id=package.id,
            lender_id=lender.id,
            email=email,
            status="sent",
            invited_user_id=invited_user.id,
        )
        db.add(recipient)
        await db.flush()

        draft = EmailDraft(
            loan_id=loan.id,
            to_email=email,
            subject=subject,
            body=(
                f"Hello,\n\n"
                f"Qualified Commercial has shared a secure lender review package for "
                f"{loan.deal_id} - {loan.address}.\n\n"
                f"For security, borrower documents are not attached and file links are not included in this email. "
                f"Please sign in to the lender portal to review the package and, if you choose, submit proposed terms:\n"
                f"{portal_url}\n\n"
                f"This package expires on {expires_at:%Y-%m-%d %H:%M %Z}.\n\n"
                f"{payload.message or ''}"
            ),
            status=EmailDraftStatus.PENDING,
            # Send the secure-package portal link FROM the acting admin's connected
            # Gmail (send_as_user in send_approved_draft) rather than the firm SA;
            # mirrors lender_send.draft_lender_send.
            sender_user_id=user.id,
            triggered_by_kind="lender_secure_package",
            triggered_by_payload={
                "package_id": str(package.id),
                "recipient_id": str(recipient.id),
                "lender_id": str(lender.id),
                "document_ids": [str(d.id) for d in selected_docs],
                "portal_url": portal_url,
            },
        )
        db.add(draft)
        await db.flush()
        recipient.email_draft_id = draft.id
        await _log_package_event(
            db,
            package_id=package.id,
            recipient_id=recipient.id,
            lender_id=lender.id,
            actor_user_id=user.id,
            event="recipient.invited",
            detail={"email": email, "draft_id": str(draft.id)},
        )

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role.value,
            kind="lender.package_created",
            summary=f"Created secure lender package for {len(lenders)} lender(s)",
            payload={
                "package_id": str(package.id),
                "lender_ids": [str(l.id) for l in lenders],
                "document_ids": [str(d.id) for d in selected_docs],
                "expires_at": expires_at.isoformat(),
            },
        )
    )
    await mark_loan_dirty(db, loan.id)
    await db.commit()
    package = (await db.execute(select(LenderPackage).where(LenderPackage.id == package.id))).scalar_one()
    return await _read_package(db, package, include_events=True)


@router.get("/loans/{loan_id}/lender-packages", response_model=list[LenderPackageRead])
async def list_loan_lender_packages(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[LenderPackageRead]:
    _require_internal(user)
    packages = (
        await db.execute(
            select(LenderPackage)
            .where(LenderPackage.loan_id == loan_id)
            .order_by(LenderPackage.created_at.desc())
        )
    ).scalars().all()
    return [await _read_package(db, p) for p in packages]


@router.post("/loans/{loan_id}/lender-packages/{package_id}/revoke", response_model=LenderPackageRead)
async def revoke_lender_package(
    loan_id: UUID,
    package_id: UUID,
    payload: LenderPackageRevoke,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderPackageRead:
    _require_internal(user)
    package = (
        await db.execute(
            select(LenderPackage).where(LenderPackage.id == package_id, LenderPackage.loan_id == loan_id)
        )
    ).scalar_one_or_none()
    if package is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Package not found")
    package.status = "revoked"
    package.revoked_at = _now()
    package.revoked_by_id = user.id
    recipients = (
        await db.execute(
            select(LenderPackageRecipient).where(LenderPackageRecipient.package_id == package.id)
        )
    ).scalars().all()
    for recipient in recipients:
        recipient.status = "revoked"
        recipient.last_event_at = package.revoked_at
    await _log_package_event(
        db,
        package_id=package.id,
        actor_user_id=user.id,
        event="package.revoked",
        detail={"reason": payload.reason},
    )
    db.add(
        Activity(
            loan_id=loan_id,
            actor_id=user.id,
            actor_label=user.role.value,
            kind="lender.package_revoked",
            summary="Revoked secure lender package",
            payload={"package_id": str(package.id), "reason": payload.reason},
        )
    )
    await db.commit()
    await db.refresh(package)
    return await _read_package(db, package, include_events=True)


@router.post("/loans/{loan_id}/lender-terms", response_model=LenderTermRead, status_code=status.HTTP_201_CREATED)
async def create_manual_lender_terms(
    loan_id: UUID,
    payload: LenderTermManualCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderTermRead:
    _require_internal(user)
    loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    lender = (await db.execute(select(Lender).where(Lender.id == payload.lender_id))).scalar_one_or_none()
    if lender is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lender not found")

    package_id: UUID | None = None
    if payload.package_recipient_id is not None:
        recipient = (
            await db.execute(
                select(LenderPackageRecipient, LenderPackage)
                .join(LenderPackage, LenderPackage.id == LenderPackageRecipient.package_id)
                .where(
                    LenderPackageRecipient.id == payload.package_recipient_id,
                    LenderPackage.loan_id == loan_id,
                )
            )
        ).first()
        if recipient is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Package recipient not found for this loan")
        rec, package = recipient
        if rec.lender_id != payload.lender_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Recipient lender does not match terms lender")
        package_id = package.id

    term = LenderTerm(
        loan_id=loan_id,
        lender_id=payload.lender_id,
        package_id=package_id,
        package_recipient_id=payload.package_recipient_id,
        created_by_id=user.id,
        submitted_by_id=user.id,
        source=payload.source,
        status=payload.status,
        **_term_value_fields(payload),
    )
    db.add(term)
    db.add(
        Activity(
            loan_id=loan_id,
            actor_id=user.id,
            actor_label=user.role.value,
            kind="lender.terms_recorded",
            summary=f"Recorded lender terms from {lender.name}",
            payload={"lender_id": str(lender.id), "source": payload.source},
        )
    )
    if package_id:
        await _log_package_event(
            db,
            package_id=package_id,
            recipient_id=payload.package_recipient_id,
            lender_id=lender.id,
            actor_user_id=user.id,
            event="terms.manual_recorded",
            detail={"source": payload.source},
        )
    await db.commit()
    await db.refresh(term)
    return _term_read(term, lender.name)


@router.patch("/loans/{loan_id}/lender-terms/{term_id}", response_model=LenderTermRead)
async def update_lender_terms(
    loan_id: UUID,
    term_id: UUID,
    payload: LenderTermUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderTermRead:
    _require_internal(user)
    row = (
        await db.execute(
            select(LenderTerm, Lender.name)
            .join(Lender, Lender.id == LenderTerm.lender_id)
            .where(LenderTerm.id == term_id, LenderTerm.loan_id == loan_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terms not found")
    term, lender_name = row
    patch = _term_update_fields(payload)
    for key, value in patch.items():
        setattr(term, key, value)
    db.add(
        Activity(
            loan_id=loan_id,
            actor_id=user.id,
            actor_label=user.role.value,
            kind="lender.terms_updated",
            summary=f"Updated lender terms from {lender_name}",
            payload={"term_id": str(term.id), "lender_id": str(term.lender_id)},
        )
    )
    await db.commit()
    await db.refresh(term)
    return _term_read(term, lender_name)


@router.post("/loans/{loan_id}/lender-terms/{term_id}/select", response_model=LenderTermRead)
async def select_lender_terms(
    loan_id: UUID,
    term_id: UUID,
    payload: LenderTermSelect,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderTermRead:
    _require_internal(user)
    row = (
        await db.execute(
            select(LenderTerm, Lender.name)
            .join(Lender, Lender.id == LenderTerm.lender_id)
            .where(LenderTerm.id == term_id, LenderTerm.loan_id == loan_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terms not found")
    selected, lender_name = row

    all_terms = (
        await db.execute(select(LenderTerm).where(LenderTerm.loan_id == loan_id))
    ).scalars().all()
    now = _now()
    for term in all_terms:
        if term.id == selected.id:
            term.status = "selected"
            term.selected_at = now
            term.selected_by_id = user.id
        elif term.status in ("pending", "received", "selected"):
            term.status = "not_selected"
            term.selected_at = None
            term.selected_by_id = None

    try:
        await connect_lender(
            db,
            loan_id=loan_id,
            lender_id=selected.lender_id,
            notify_toggles=[],
            actor_user_id=user.id,
            actor_label=user.role.value,
        )
    except LenderConnectError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one()
    if payload.apply_to_loan:
        for attr in (
            "base_rate",
            "final_rate",
            "discount_points",
            "origination_pct",
            "lender_fees",
            "term_months",
            "amortization_style",
            "prepay_penalty",
            "ltv",
            "ltc",
            "dscr",
            "reserves_required",
            "construction_holdback_pct",
            "draw_count",
            "exit_strategy",
        ):
            value = getattr(selected, attr)
            if value is not None:
                setattr(loan, attr, value)
        if selected.approved_amount is not None:
            loan.amount = selected.approved_amount
        if loan.stage in (LoanStage.PREQUALIFIED, LoanStage.COLLECTING_DOCS):
            loan.stage = LoanStage.LENDER_CONNECTED

    if selected.package_id is not None:
        await _log_package_event(
            db,
            package_id=selected.package_id,
            recipient_id=selected.package_recipient_id,
            lender_id=selected.lender_id,
            actor_user_id=user.id,
            event="terms.selected",
            detail={"apply_to_loan": payload.apply_to_loan},
        )

    db.add(
        Activity(
            loan_id=loan_id,
            actor_id=user.id,
            actor_label=user.role.value,
            kind="lender.terms_selected",
            summary=f"Selected lender terms from {lender_name}",
            payload={
                "term_id": str(selected.id),
                "lender_id": str(selected.lender_id),
                "apply_to_loan": payload.apply_to_loan,
            },
        )
    )
    await mark_loan_dirty(db, loan_id)
    await db.commit()
    await db.refresh(selected)
    return _term_read(selected, lender_name)


@router.get("/lender/packages", response_model=list[LenderPortalPackageListItem])
async def list_lender_portal_packages(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[LenderPortalPackageListItem]:
    _require_lender(user)
    lender_ids = await _lender_membership_ids(db, user)
    if not lender_ids:
        return []
    rows = (
        await db.execute(
            select(LenderPackage, LenderPackageRecipient)
            .join(LenderPackageRecipient, LenderPackageRecipient.package_id == LenderPackage.id)
            .where(LenderPackageRecipient.lender_id.in_(lender_ids))
            .order_by(LenderPackage.created_at.desc())
        )
    ).all()
    return [
        await _build_lender_portal_list_item(db, package=package, recipient=recipient)
        for package, recipient in rows
    ]


@router.get("/lender/packages/{package_id}", response_model=LenderPackageRead)
async def get_lender_portal_package(
    package_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderPackageRead:
    package, recipient = await _get_lender_recipient(db, package_id=package_id, user=user)
    _assert_package_open(package)
    now = _now()
    if recipient.viewed_at is None:
        recipient.viewed_at = now
    recipient.status = "viewed" if recipient.status == "sent" else recipient.status
    recipient.last_event_at = now
    await _log_package_event(
        db,
        package_id=package.id,
        recipient_id=recipient.id,
        lender_id=recipient.lender_id,
        actor_user_id=user.id,
        event="package.viewed",
    )
    await db.commit()
    package = (await db.execute(select(LenderPackage).where(LenderPackage.id == package.id))).scalar_one()
    read = await _read_package(db, package)
    read.recipients = [r for r in read.recipients if r.id == recipient.id]
    return read


@router.get("/lender/packages/{package_id}/documents/{document_id}/download", response_model=LenderDownloadResponse)
async def lender_package_document_download(
    package_id: UUID,
    document_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderDownloadResponse:
    package, recipient = await _get_lender_recipient(db, package_id=package_id, user=user)
    _assert_package_open(package)
    row = (
        await db.execute(
            select(LenderPackageDocument, Document)
            .join(Document, Document.id == LenderPackageDocument.document_id)
            .where(
                LenderPackageDocument.package_id == package.id,
                LenderPackageDocument.document_id == document_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found in package")
    _package_doc, doc = row
    if not doc.s3_key:
        raise HTTPException(status.HTTP_409_CONFLICT, "Document file is not available")
    settings = get_settings()
    if not settings.s3_bucket:
        raise HTTPException(status.HTTP_409_CONFLICT, "Document storage is not configured")
    try:
        url = _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": doc.s3_key},
            ExpiresIn=DOWNLOAD_URL_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not create download URL: {exc}") from exc

    now = _now()
    recipient.downloaded_at = recipient.downloaded_at or now
    recipient.status = "downloaded" if recipient.status in ("sent", "viewed") else recipient.status
    recipient.last_event_at = now
    await _log_package_event(
        db,
        package_id=package.id,
        recipient_id=recipient.id,
        lender_id=recipient.lender_id,
        actor_user_id=user.id,
        event="document.downloaded",
        detail={"document_id": str(doc.id), "document_name": doc.name},
    )
    await db.commit()
    return LenderDownloadResponse(download_url=url, expires_in_seconds=DOWNLOAD_URL_TTL_SECONDS)


@router.put("/lender/packages/{package_id}/terms", response_model=LenderTermRead)
async def submit_lender_portal_terms(
    package_id: UUID,
    payload: LenderTermFields,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderTermRead:
    package, recipient = await _get_lender_recipient(db, package_id=package_id, user=user)
    _assert_package_open(package)
    lender = (await db.execute(select(Lender).where(Lender.id == recipient.lender_id))).scalar_one()

    existing = (
        await db.execute(
            select(LenderTerm).where(
                LenderTerm.package_recipient_id == recipient.id,
                LenderTerm.source == "portal",
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        term = LenderTerm(
            loan_id=package.loan_id,
            lender_id=recipient.lender_id,
            package_id=package.id,
            package_recipient_id=recipient.id,
            submitted_by_id=user.id,
            source="portal",
            status="received",
            **_term_value_fields(payload),
        )
        db.add(term)
    else:
        term = existing
        for key, value in _term_value_fields(payload).items():
            setattr(term, key, value)
        term.status = "received"
        term.submitted_by_id = user.id

    now = _now()
    recipient.status = "terms_submitted"
    recipient.terms_submitted_at = now
    recipient.last_event_at = now
    await _log_package_event(
        db,
        package_id=package.id,
        recipient_id=recipient.id,
        lender_id=recipient.lender_id,
        actor_user_id=user.id,
        event="terms.submitted",
    )
    db.add(
        Activity(
            loan_id=package.loan_id,
            actor_id=user.id,
            actor_label="lender",
            kind="lender.terms_submitted",
            summary=f"Lender submitted terms: {lender.name}",
            payload={"package_id": str(package.id), "lender_id": str(lender.id)},
        )
    )
    await mark_loan_dirty(db, package.loan_id)
    await db.commit()
    await db.refresh(term)
    return _term_read(term, lender.name)


@router.post("/lender/packages/{package_id}/no-quote", response_model=LenderPackageRead)
async def mark_lender_no_quote(
    package_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderPackageRead:
    package, recipient = await _get_lender_recipient(db, package_id=package_id, user=user)
    _assert_package_open(package)
    now = _now()
    recipient.status = "no_quote"
    recipient.no_quote_at = now
    recipient.last_event_at = now
    await _log_package_event(
        db,
        package_id=package.id,
        recipient_id=recipient.id,
        lender_id=recipient.lender_id,
        actor_user_id=user.id,
        event="recipient.no_quote",
    )
    await db.commit()
    package = (await db.execute(select(LenderPackage).where(LenderPackage.id == package.id))).scalar_one()
    read = await _read_package(db, package)
    read.recipients = [r for r in read.recipients if r.id == recipient.id]
    return read
