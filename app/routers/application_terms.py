from __future__ import annotations

# FastAPI dependencies are intentionally expressed as callable defaults.
# ruff: noqa: B008
import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_terms import ApplicationTermSheet, ApplicationTermSheetDelivery
from app.schemas.application_terms import (
    ClientTermsEmailRequest,
    ClientTermsEmailResult,
    ClientTermsRead,
    ClientTermsWrite,
)
from app.services import application_profiles as profiles
from app.services import application_terms, file_contacts
from app.services.application_terms_pdf import filename_for, render_terms_pdf
from app.services.email.user_mailer import send_as_user

router = APIRouter(prefix="/application-profiles", tags=["application-terms"])


def _require_terms_actor(user: CurrentUser) -> None:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Underwriting role required")


def _require_current_version(row, expected_version: int, action: str) -> None:
    if row.version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A newer terms version is available. Reload the file before {action}.",
        )


def _require_not_expired(row) -> None:
    if row.status == "issued" and row.expires_on and row.expires_on < datetime.now(UTC).date():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "These terms have expired. Save a new version before issuing or sending them again.",
        )


async def _pdf_context(db: AsyncSession, profile) -> tuple[str, str | None]:
    sources = await file_contacts.load_sources(db, profile)
    recipient = await file_contacts.client_recipient(db, profile, sources)
    return file_contacts.business_label(sources), recipient.name


async def _issued_pdf(
    row: ApplicationTermSheet,
    *,
    business_name: str,
    client_name: str | None,
) -> tuple[bytes, str]:
    """Issue once and freeze the exact client artifact for this version."""
    application_terms.issue(row)
    if row.issued_pdf_bytes:
        return bytes(row.issued_pdf_bytes), row.issued_filename or filename_for(row, business_name)
    pdf = await asyncio.to_thread(
        render_terms_pdf,
        row,
        business_name=business_name,
        client_name=client_name,
    )
    row.issued_pdf_bytes = pdf
    row.issued_pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    row.issued_filename = filename_for(row, business_name)
    return pdf, row.issued_filename


@router.get("/{profile_id}/client-terms", response_model=ClientTermsRead)
async def get_client_terms(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientTermsRead:
    _require_terms_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    return await application_terms.read_terms(db, profile)


@router.put("/{profile_id}/client-terms", response_model=ClientTermsRead)
async def put_client_terms(
    profile_id: UUID,
    payload: ClientTermsWrite,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientTermsRead:
    _require_terms_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    row = await application_terms.save_terms(db, profile, user, payload)
    await db.commit()
    await db.refresh(row)
    return await application_terms.read_terms(db, profile, row)


@router.get("/{profile_id}/client-terms.pdf")
async def download_client_terms_pdf(
    profile_id: UUID,
    expected_version: int,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_terms_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    row = await application_terms.current_term_sheet(db, profile.id)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Save the client terms before creating the PDF")
    _require_current_version(row, expected_version, "downloading")
    business_name, client_name = await _pdf_context(db, profile)
    if row.status == "issued" and row.issued_pdf_bytes:
        pdf = bytes(row.issued_pdf_bytes)
        filename = row.issued_filename or filename_for(row, business_name)
    else:
        pdf = await asyncio.to_thread(
            render_terms_pdf,
            row,
            business_name=business_name,
            client_name=client_name,
        )
        filename = filename_for(row, business_name)
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "term_sheet.downloaded",
        f"Downloaded client terms version {row.version}",
        target_type="application_term_sheet",
        target_id=row.id,
        metadata={"version": row.version, "pdf_sha256": hashlib.sha256(pdf).hexdigest()},
    )
    await db.commit()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{profile_id}/client-terms/issue.pdf")
async def issue_and_download_client_terms_pdf(
    profile_id: UUID,
    expected_version: int,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Issue the saved version and return its exact client-ready PDF.

    This is the safe delivery path for referral-managed files: staff can route
    the issued artifact through the attorney or broker without contacting the
    protected client directly.
    """
    _require_terms_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await application_terms.lock_profile_terms(db, profile.id)
    row = await application_terms.current_term_sheet(db, profile.id)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Save the client terms before issuing the PDF")
    _require_current_version(row, expected_version, "issuing")
    _require_not_expired(row)
    business_name, client_name = await _pdf_context(db, profile)
    pdf, filename = await _issued_pdf(
        row,
        business_name=business_name,
        client_name=client_name,
    )
    await apply_terms_issued_lifecycle(db, profile, user)
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "term_sheet.issued_downloaded",
        f"Issued and downloaded client terms version {row.version}",
        target_type="application_term_sheet",
        target_id=row.id,
        metadata={"version": row.version, "pdf_sha256": hashlib.sha256(pdf).hexdigest()},
    )
    await db.commit()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def apply_terms_issued_lifecycle(db: AsyncSession, profile, user: CurrentUser) -> None:
    from app.routers.application_profiles import apply_underwriting_changes

    if profile.underwriting_status in {"submitted", "collecting_docs", "in_underwriting"}:
        await apply_underwriting_changes(
            db,
            profile,
            user,
            {"underwriting_status": "term_sheet_provided"},
        )


@router.post("/{profile_id}/client-terms/email", response_model=ClientTermsEmailResult)
async def email_client_terms(
    profile_id: UUID,
    payload: ClientTermsEmailRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientTermsEmailResult:
    _require_terms_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await application_terms.lock_profile_terms(db, profile.id)
    row = await application_terms.current_term_sheet(db, profile.id)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Save the client terms before sending them")
    _require_current_version(row, payload.expected_version, "sending")
    _require_not_expired(row)
    sources = await file_contacts.load_sources(db, profile)
    if sources.intake is not None and sources.intake.client_contact_suppressed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Direct client contact is suppressed on this referral-managed file. Send the terms through the referring professional.",
        )
    business_name, client_name = await _pdf_context(db, profile)
    pdf, filename = await _issued_pdf(
        row,
        business_name=business_name,
        client_name=client_name,
    )
    digest = hashlib.sha256(pdf).hexdigest()
    to_emails = [str(value) for value in payload.to_emails]
    cc_emails = [str(value) for value in payload.cc_emails]
    delivery = (
        await db.execute(
            select(ApplicationTermSheetDelivery).where(
                ApplicationTermSheetDelivery.idempotency_key == payload.delivery_key
            )
        )
    ).scalar_one_or_none()
    if delivery is not None and delivery.term_sheet_id != row.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This delivery key belongs to a different terms version")
    if delivery is not None and delivery.status == "sent":
        return ClientTermsEmailResult(
            sent=True,
            filename=filename,
            message_id=delivery.provider_message_id,
            detail=delivery.provider_detail,
        )
    if delivery is not None and delivery.status == "sending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This delivery is already in progress. Check the audit trail before sending again.",
        )
    if delivery is None:
        delivery = ApplicationTermSheetDelivery(
            idempotency_key=payload.delivery_key,
            term_sheet_id=row.id,
            to_emails=to_emails,
            cc_emails=cc_emails,
            subject=payload.subject.strip(),
            body=payload.body.strip(),
            status="sending",
            pdf_sha256=digest,
            pdf_bytes=pdf,
            sent_by_user_id=user.id,
        )
        db.add(delivery)
    else:
        delivery.to_emails = to_emails
        delivery.cc_emails = cc_emails
        delivery.subject = payload.subject.strip()
        delivery.body = payload.body.strip()
        delivery.status = "sending"
        delivery.provider_detail = None
        delivery.provider_message_id = None
        delivery.pdf_sha256 = digest
        delivery.pdf_bytes = pdf
        delivery.sent_by_user_id = user.id
        delivery.sent_at = None
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "term_sheet.email_queued",
        f"Queued client terms version {row.version} for delivery",
        target_type="application_term_sheet",
        target_id=row.id,
        metadata={"version": row.version, "to": to_emails, "delivery_key": str(payload.delivery_key)},
    )
    # Persist both the exact bytes and the pending delivery before crossing the
    # email-provider boundary. A crash can no longer erase the delivery record
    # or cause an automatic duplicate resend.
    await db.commit()
    try:
        result = await send_as_user(
            db,
            user.id,
            to_emails=to_emails,
            cc_emails=cc_emails or None,
            subject=payload.subject.strip(),
            body_text=payload.body.strip(),
            attachments=[(filename, pdf, "application/pdf")],
        )
    except Exception as exc:  # noqa: BLE001
        result = None
        delivery.status = "failed"
        delivery.provider_detail = "transport_exception"
        await profiles.log_profile_action(
            db,
            profile,
            user,
            "term_sheet.email_failed",
            f"Client terms version {row.version} could not be delivered",
            target_type="application_term_sheet",
            target_id=row.id,
            metadata={"version": row.version, "to": to_emails, "detail": "transport_exception"},
        )
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The email could not be sent. The saved terms were not marked as delivered.") from exc

    delivery.provider_detail = result.detail
    delivery.provider_message_id = result.message_id
    if not result.ok:
        delivery.status = "failed"
        await profiles.log_profile_action(
            db,
            profile,
            user,
            "term_sheet.email_failed",
            f"Client terms version {row.version} could not be delivered",
            target_type="application_term_sheet",
            target_id=row.id,
            metadata={"version": row.version, "to": to_emails, "detail": result.detail},
        )
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The email provider did not accept the message. The saved terms were not marked as delivered.")

    delivery.status = "sent"
    delivery.sent_at = datetime.now(UTC)
    await apply_terms_issued_lifecycle(db, profile, user)
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "term_sheet.emailed",
        f"Emailed client terms version {row.version}",
        target_type="application_term_sheet",
        target_id=row.id,
        metadata={
            "version": row.version,
            "to": to_emails,
            "cc": cc_emails,
            "pdf_sha256": digest,
            "provider": result.detail,
            "message_id": result.message_id,
        },
    )
    await db.commit()
    return ClientTermsEmailResult(
        sent=True,
        filename=filename,
        message_id=result.message_id,
        detail=result.detail,
    )
