"""Production Package routes.

Operator surface under /production-packages (visibility through the
application profile), the rep surface under /production-packages/shares/{token}
(a signed-in rep plus their unique link), and the client surface under
/public/dealer-ai-intake/{token}/production-package (the intake room).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.dealer_os.models import DealerAuditLog
from app.deps import CurrentUser
from app.models.production_package import ProductionTermSheet
from app.models.referral_partner_company import ReferralPartnerCompany
from app.schemas.production_package import (
    ProductionCapabilitiesRead,
    ProductionComparisonRead,
    ProductionComputeRead,
    ProductionComputeRequest,
    ProductionHistoryRead,
    ProductionLinkResolved,
    ProductionLinkUnlockBody,
    ProductionLinkUnlocked,
    ProductionPackagePatch,
    ProductionPackageRead,
    ProductionPackageResolve,
    ProductionPrefillRequest,
    ProductionReasonBody,
    ProductionSendRequest,
    ProductionSendResult,
    ProductionShareLinkCreate,
    ProductionShareLinkCreated,
    ProductionSmsConsentCapture,
    ProductionSmsConsentRead,
    ProductionTermSheetBody,
    ProductionTermSheetEmailRequest,
    ProductionTermSheetEmailResult,
    ProductionTermSheetResult,
    ProductionTermSheetState,
    SponsorCompanyUpdate,
    SponsorOptionRead,
)
from app.services import application_profiles as profiles
from app.services import file_contacts
from app.services import production_arrangement as pa
from app.services import production_packages as svc
from app.services import production_term_sheets as sheets_svc
from app.services.email.user_mailer import send_as_user
from app.services.production_term_sheet_pdf import filename_for as term_pdf_filename
from app.services.production_term_sheet_pdf import render_term_sheet_pdf

router = APIRouter(prefix="/production-packages", tags=["production-packages"])


# ---- static paths first so they never collide with /{package_id} ----

@router.get("/capabilities", response_model=ProductionCapabilitiesRead)
async def production_capabilities(user: CurrentUser) -> ProductionCapabilitiesRead:
    return ProductionCapabilitiesRead(**svc.probe_capabilities())


@router.get("/sponsors", response_model=list[SponsorOptionRead])
async def list_sponsors(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[SponsorOptionRead]:
    if user.role not in svc.OPERATOR_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team role required")
    return await svc.sponsor_options(db, user=user)


@router.patch("/sponsors/{company_id}", response_model=SponsorOptionRead)
async def update_sponsor(
    company_id: UUID, payload: SponsorCompanyUpdate, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> SponsorOptionRead:
    """Correct the sponsor company. The desk owns it; packages copy from it."""
    out = await svc.update_sponsor_company(
        db, company_id, payload.model_dump(exclude_unset=True), user=user
    )
    await db.commit()
    return out


# ---- term sheet (keyed on the profile: terms are recorded before any final exists) ----

async def _profile_access(db: AsyncSession, profile_id: UUID, user: CurrentUser):
    return await svc.resolve_package(db, profile_id, user)


def _require_sheet_version(sheet: ProductionTermSheet, expected_version: int, action: str) -> None:
    if sheet.version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A newer loan-terms version is available. Reload the file before {action}.",
        )


async def _locked_current_sheet(
    db: AsyncSession,
    access: svc.PackageAccess,
    expected_version: int,
    action: str,
) -> ProductionTermSheet:
    sheet = access.term_sheet
    if sheet is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Record the loan terms before creating the client PDF")
    locked = await db.get(ProductionTermSheet, sheet.id, with_for_update=True)
    if locked is None or locked.status != "current":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A newer loan-terms version is available. Reload the file before {action}.",
        )
    _require_sheet_version(locked, expected_version, action)
    return locked


async def _term_pdf_context(
    db: AsyncSession,
    access: svc.PackageAccess,
) -> tuple[str, str | None, str | None]:
    business_name, _email, _phone = await svc.client_contact(db, access)
    sources = await file_contacts.load_sources(db, access.profile)
    recipient = await file_contacts.client_recipient(db, access.profile, sources)
    sponsor_name = "UrChoice"
    if access.package.sponsor_company_id:
        sponsor = await db.get(ReferralPartnerCompany, access.package.sponsor_company_id)
        if sponsor is not None and sponsor.name.strip():
            sponsor_name = sponsor.name.strip()
    return business_name, recipient.name, sponsor_name


async def _render_client_term_pdf(
    db: AsyncSession,
    access: svc.PackageAccess,
    sheet: ProductionTermSheet,
) -> tuple[bytes, str]:
    business_name, client_name, sponsor_name = await _term_pdf_context(db, access)
    pdf = await asyncio.to_thread(
        render_term_sheet_pdf,
        sheet,
        business_name=business_name,
        client_name=client_name,
        sponsor_name=sponsor_name,
    )
    return pdf, term_pdf_filename(sheet, business_name)


async def _email_delivery_audit(
    db: AsyncSession,
    *,
    dealer_id: UUID,
    delivery_key: UUID,
) -> list[DealerAuditLog]:
    return list(
        (
            await db.execute(
                select(DealerAuditLog)
                .where(
                    DealerAuditLog.dealer_id == dealer_id,
                    DealerAuditLog.entity_kind == "production_term_sheet",
                    DealerAuditLog.action.in_(
                        (
                            "production_term_sheet.email_queued",
                            "production_term_sheet.emailed",
                            "production_term_sheet.email_failed",
                        )
                    ),
                    DealerAuditLog.after["delivery_key"].astext == str(delivery_key),
                )
                .order_by(DealerAuditLog.created_at.desc())
            )
        ).scalars().all()
    )


@router.get("/term-sheets/{profile_id}", response_model=ProductionTermSheetState)
async def read_term_sheet(profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionTermSheetState:
    if user.role not in svc.OPERATOR_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team role required")
    access = await _profile_access(db, profile_id, user)
    await db.commit()
    return ProductionTermSheetState(**await svc.term_sheet_state(db, access))


@router.post("/term-sheets/{profile_id}", response_model=ProductionTermSheetResult)
async def record_term_sheet(
    profile_id: UUID, payload: ProductionTermSheetBody, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionTermSheetResult:
    access = await _profile_access(db, profile_id, user)
    _sheet, reapplied = await sheets_svc.record_sheet(db, profile=access.profile, user=user, body=payload.model_dump(), request=request)
    await db.commit()
    await svc._load_family(db, access)
    state = ProductionTermSheetState(**await svc.term_sheet_state(db, access))
    final_read = None
    if reapplied is not None:
        child_access = await svc.load_package_access(db, reapplied.id, user)
        final_read = await svc.serialize(db, child_access)
    return ProductionTermSheetResult(state=state, final=final_read)


@router.get("/term-sheets/{profile_id}/client.pdf")
async def client_term_sheet_pdf(
    profile_id: UUID,
    expected_version: int,
    user: CurrentUser,
    disposition: Literal["inline", "attachment"] = "inline",
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Preview or download the current dealer loan terms without duplicating them."""
    sheets_svc.require_term_role(user)
    access = await _profile_access(db, profile_id, user)
    sheet = await _locked_current_sheet(db, access, expected_version, "opening the PDF")
    pdf, filename = await _render_client_term_pdf(db, access, sheet)
    digest = hashlib.sha256(pdf).hexdigest()
    action = "production_term_sheet.pdf_previewed" if disposition == "inline" else "production_term_sheet.pdf_downloaded"
    await profiles.log_profile_action(
        db,
        access.profile,
        user,
        action,
        f"{'Previewed' if disposition == 'inline' else 'Downloaded'} client loan terms v{sheet.version}",
        target_type="production_term_sheet",
        target_id=sheet.id,
        metadata={"version": sheet.version, "pdf_sha256": digest, "disposition": disposition},
    )
    await db.commit()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/term-sheets/{profile_id}/client/email",
    response_model=ProductionTermSheetEmailResult,
)
async def email_client_term_sheet(
    profile_id: UUID,
    payload: ProductionTermSheetEmailRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionTermSheetEmailResult:
    """Email one explicit ProductionTermSheet version to operator-confirmed recipients."""
    sheets_svc.require_term_role(user)
    access = await _profile_access(db, profile_id, user)
    if access.profile.dealer_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This dealer file is missing its audit owner")
    sheet = await _locked_current_sheet(db, access, payload.expected_version, "sending the PDF")
    sources = await file_contacts.load_sources(db, access.profile)
    if sources.intake is not None and sources.intake.client_contact_suppressed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Direct client contact is suppressed on this referral-managed file. Download the PDF and route it through the referring professional.",
        )

    previous = await _email_delivery_audit(
        db,
        dealer_id=access.profile.dealer_id,
        delivery_key=payload.delivery_key,
    )
    if previous and any(row.entity_id != sheet.id for row in previous):
        raise HTTPException(status.HTTP_409_CONFLICT, "This delivery key belongs to a different loan-terms version")
    if previous:
        latest = previous[0]
        details = latest.after or {}
        if latest.action == "production_term_sheet.emailed":
            return ProductionTermSheetEmailResult(
                sent=True,
                filename=str(details.get("filename") or "Loan-Terms.pdf"),
                message_id=details.get("message_id"),
                detail=details.get("provider"),
            )
        if latest.action == "production_term_sheet.email_queued":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This delivery is already in progress. Check the audit trail before sending again.",
            )

    pdf, filename = await _render_client_term_pdf(db, access, sheet)
    digest = hashlib.sha256(pdf).hexdigest()
    to_emails = [str(value) for value in payload.to_emails]
    cc_emails = [str(value) for value in payload.cc_emails]
    audit_common = {
        "delivery_key": str(payload.delivery_key),
        "version": sheet.version,
        "filename": filename,
        "pdf_sha256": digest,
        "to": to_emails,
        "cc": cc_emails,
    }
    await profiles.log_profile_action(
        db,
        access.profile,
        user,
        "production_term_sheet.email_queued",
        f"Queued client loan terms v{sheet.version} for email delivery",
        target_type="production_term_sheet",
        target_id=sheet.id,
        metadata=audit_common,
    )
    # Persist the idempotency claim before crossing the email-provider boundary.
    # A crash is intentionally fail-closed: the operator must review the audit
    # trail and use a new key rather than risk a duplicate client email.
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
        await profiles.log_profile_action(
            db,
            access.profile,
            user,
            "production_term_sheet.email_failed",
            f"Client loan terms v{sheet.version} could not be delivered",
            target_type="production_term_sheet",
            target_id=sheet.id,
            metadata={**audit_common, "provider": "transport_exception"},
        )
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The email could not be sent. The audit trail was preserved.") from exc
    if not result.ok:
        await profiles.log_profile_action(
            db,
            access.profile,
            user,
            "production_term_sheet.email_failed",
            f"Client loan terms v{sheet.version} could not be delivered",
            target_type="production_term_sheet",
            target_id=sheet.id,
            metadata={**audit_common, "provider": result.detail},
        )
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The email provider did not accept the message. The audit trail was preserved.")
    await profiles.log_profile_action(
        db,
        access.profile,
        user,
        "production_term_sheet.emailed",
        f"Emailed client loan terms v{sheet.version}",
        target_type="production_term_sheet",
        target_id=sheet.id,
        metadata={**audit_common, "provider": result.detail, "message_id": result.message_id},
    )
    await db.commit()
    return ProductionTermSheetEmailResult(
        sent=True,
        filename=filename,
        message_id=result.message_id,
        detail=result.detail,
    )


@router.post("/term-sheets/{profile_id}/withdraw", response_model=ProductionTermSheetState)
async def withdraw_term_sheet(
    profile_id: UUID, payload: ProductionReasonBody, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionTermSheetState:
    access = await _profile_access(db, profile_id, user)
    await sheets_svc.withdraw_sheet(db, profile=access.profile, user=user, reason=payload.reason)
    await db.commit()
    await svc._load_family(db, access)
    return ProductionTermSheetState(**await svc.term_sheet_state(db, access))


@router.post("/resolve", response_model=ProductionPackageRead)
async def resolve_production_package(
    payload: ProductionPackageResolve, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access = await svc.resolve_package(db, payload.profile_id, user)
    await db.commit()
    return await svc.serialize(db, access)


# ---- rep surface (signed-in rep + unique link) ----

@router.get("/shares/{token}", response_model=ProductionPackageRead)
async def rep_share_read(token: str, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionPackageRead:
    access = await svc.resolve_rep_share(db, user, token)
    await db.commit()
    return await svc.serialize(db, access)


@router.patch("/shares/{token}", response_model=ProductionPackageRead)
async def rep_share_patch(
    token: str, payload: ProductionPackagePatch, request: Request, user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access = await svc.resolve_rep_share(db, user, token)
    await svc.apply_changes(
        db, access, changes=payload.changes, version=payload.version, confirm=payload.confirm, request=request
    )
    await db.commit()
    return await svc.serialize(db, access)


@router.post("/shares/{token}/compute", response_model=ProductionComputeRead)
async def rep_share_compute(
    token: str, payload: ProductionComputeRequest, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionComputeRead:
    await svc.resolve_rep_share(db, user, token)
    await db.commit()
    return _compute(payload.arrangement, 1)


@router.post("/shares/{token}/send", response_model=ProductionSendResult)
async def rep_share_send(
    token: str, payload: ProductionSendRequest, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionSendResult:
    from app.services import production_signing as signing_svc

    access = await svc.resolve_rep_share(db, user, token)
    result = await signing_svc.send(
        db, access, channel=payload.channel, recipient_email=None, recipient_phone=None, request=request, attestation=None,
    )
    await db.commit()
    return ProductionSendResult(
        package=await svc.serialize(db, access), delivered=result["delivered"], emailed=result["emailed"],
        texted=result["texted"], detail=result["detail"], already_sent=result.get("already_sent", False),
    )


@router.post("/shares/{token}/remind", response_model=ProductionSendResult)
async def rep_share_remind(
    token: str, payload: ProductionSendRequest, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionSendResult:
    from app.services import production_signing as signing_svc

    access = await svc.resolve_rep_share(db, user, token)
    result = await signing_svc.remind(db, access, channel=payload.channel, request=request)
    await db.commit()
    return ProductionSendResult(
        package=await svc.serialize(db, access), delivered=result["delivered"], emailed=result["emailed"],
        texted=result["texted"], detail=result["detail"],
    )


@router.post("/shares/{token}/prefill")
async def rep_share_prefill(
    token: str, payload: ProductionPrefillRequest, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    access = await svc.resolve_rep_share(db, user, token)
    out = await svc.run_prefill(db, access, force=payload.force, fields=payload.fields, apply=payload.apply)
    await db.commit()
    return out


@router.get("/shares/{token}/resolve", response_model=ProductionLinkResolved)
async def share_resolve(token: str, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionLinkResolved:
    """A signed-in person opened a forwarded link: where do they belong?"""
    out = await svc.resolve_share_for_user(db, user, token)
    await db.commit()
    return ProductionLinkResolved(**out)


# ---- operator surface ----

def _compute(arrangement: dict[str, Any], stage: int = 1) -> ProductionComputeRead:
    merged = pa.merge_changes(pa.empty_arrangement(), arrangement or {})
    computed = pa.jsonable(pa.compute(merged, stage=stage))
    return ProductionComputeRead(
        computed=computed, attention=computed["attention"], attention_presentation=computed["attention_presentation"]
    )


@router.get("/{package_id}", response_model=ProductionPackageRead)
async def read_production_package(package_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    return await svc.serialize(db, access)


@router.patch("/{package_id}", response_model=ProductionPackageRead)
async def patch_production_package(
    package_id: UUID, payload: ProductionPackagePatch, request: Request, user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    await svc.apply_changes(
        db, access, changes=payload.changes, version=payload.version, confirm=payload.confirm, request=request
    )
    await db.commit()
    return await svc.serialize(db, access)


@router.post("/{package_id}/prefill")
async def prefill_production_package(
    package_id: UUID, payload: ProductionPrefillRequest, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    access = await svc.load_operator_access(db, package_id, user)
    out = await svc.run_prefill(db, access, force=payload.force, fields=payload.fields, apply=payload.apply)
    await db.commit()
    return out


@router.post("/{package_id}/compute", response_model=ProductionComputeRead)
async def compute_production_package(
    package_id: UUID, payload: ProductionComputeRequest, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionComputeRead:
    access = await svc.load_operator_access(db, package_id, user)
    return _compute(payload.arrangement, payload.stage or int(access.package.stage or 1))


@router.post("/{package_id}/share-links", response_model=ProductionShareLinkCreated, status_code=201)
async def create_share_link(
    package_id: UUID, payload: ProductionShareLinkCreate, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionShareLinkCreated:
    access = await svc.load_operator_access(db, package_id, user)
    if payload.kind == "public":
        link, token, pin = await svc.mint_public_link(
            db, access, label=payload.label, recipient_name=payload.recipient_name,
            recipient_email=payload.recipient_email, expires_in_days=payload.expires_in_days,
        )
        await db.commit()
        read = await svc.serialize(db, access)
        row = next(item for item in read.share_links if item.id == link.id)
        return ProductionShareLinkCreated(link=row, url=svc.share_link_url(token, "public"), expires_at=link.expires_at, pin=pin)
    if payload.rep_user_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a field representative")
    link, token = await svc.mint_share_link(
        db, access, rep_user_id=payload.rep_user_id, label=payload.label,
        expires_in_days=payload.expires_in_days, outside_book=payload.outside_book,
    )
    await db.commit()
    read = await svc.serialize(db, access)
    row = next(item for item in read.share_links if item.id == link.id)
    return ProductionShareLinkCreated(link=row, url=svc.share_link_url(token), expires_at=link.expires_at)


@router.delete("/{package_id}/share-links/{link_id}", status_code=204)
async def delete_share_link(package_id: UUID, link_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> None:
    access = await svc.load_operator_access(db, package_id, user)
    await svc.revoke_share_link(db, access, link_id)
    await db.commit()


@router.get("/{package_id}/history", response_model=ProductionHistoryRead)
async def production_package_history(package_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionHistoryRead:
    access = await svc.load_operator_access(db, package_id, user)
    return ProductionHistoryRead(events=await svc.history(db, access))


@router.post("/{package_id}/sms-consent", response_model=ProductionSmsConsentRead)
async def capture_production_sms_consent(
    package_id: UUID, payload: ProductionSmsConsentCapture, request: Request, user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionSmsConsentRead:
    access = await svc.load_operator_access(db, package_id, user)
    out = await svc.capture_sms_consent(
        db, access, phone=payload.phone, consenter_name=payload.consenter_name, method=payload.method, request=request
    )
    await db.commit()
    return out


# ---------------------------------------------------------------------------
# presentation, send, signatures, execution
# ---------------------------------------------------------------------------



from app.schemas.production_package import (  # noqa: E402
    ProductionClientSignBody,
    ProductionClientSignResult,
    ProductionManualSignatureBody,
    ProductionManualSignatureResult,
    ProductionPresentationRead,
    ProductionScanCompleteBody,
    ProductionSendResult,
    ProductionSigningGateRead,
)
from app.services import production_signing as signing  # noqa: E402
from app.services.payment_authorization import presign_private_s3_object  # noqa: E402


@router.post("/{package_id}/presentation", response_model=ProductionPackageRead)
async def generate_presentation(package_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    await signing.generate_presentation(db, access)
    await db.commit()
    return await svc.serialize(db, access)


@router.post("/shares/{token}/presentation", response_model=ProductionPackageRead)
async def rep_generate_presentation(token: str, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionPackageRead:
    access = await svc.resolve_rep_share(db, user, token)
    await signing.generate_presentation(db, access)
    await db.commit()
    return await svc.serialize(db, access)


@router.get("/{package_id}/presentation", response_model=ProductionPresentationRead)
async def read_presentation(package_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionPresentationRead:
    access = await svc.load_operator_access(db, package_id, user)
    read = await svc.serialize(db, access)
    return read.presentation


@router.post("/{package_id}/send", response_model=ProductionSendResult)
async def request_signature(
    package_id: UUID, payload: ProductionSendRequest, request: Request, user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionSendResult:
    access = await svc.load_operator_access(db, package_id, user)
    result = await signing.send(
        db, access, channel=payload.channel, recipient_email=payload.recipient_email,
        recipient_phone=payload.recipient_phone, request=request,
        attestation=payload.funding_attestation.model_dump() if payload.funding_attestation else None,
    )
    await db.commit()
    return ProductionSendResult(
        package=await svc.serialize(db, access), delivered=result["delivered"], emailed=result["emailed"],
        texted=result["texted"], detail=result["detail"], already_sent=result.get("already_sent", False),
    )


@router.post("/{package_id}/remind", response_model=ProductionSendResult)
async def remind_signature(
    package_id: UUID, payload: ProductionSendRequest, request: Request, user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionSendResult:
    access = await svc.load_operator_access(db, package_id, user)
    result = await signing.remind(db, access, channel=payload.channel, request=request)
    await db.commit()
    return ProductionSendResult(
        package=await svc.serialize(db, access), delivered=result["delivered"], emailed=result["emailed"],
        texted=result["texted"], detail=result["detail"],
    )


@router.post("/{package_id}/reopen", response_model=ProductionPackageRead)
async def reopen_package(
    package_id: UUID, payload: ProductionReasonBody, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    await signing.reopen(db, access, reason=payload.reason)
    await db.commit()
    return await svc.serialize(db, access)


@router.post("/{package_id}/void", response_model=ProductionPackageRead)
async def void_package(
    package_id: UUID, payload: ProductionReasonBody, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    await signing.void(db, access, reason=payload.reason)
    await db.commit()
    return await svc.serialize(db, access)


@router.post("/{package_id}/signatures/manual", response_model=ProductionManualSignatureResult, status_code=201)
async def record_manual_signature(
    package_id: UUID, payload: ProductionManualSignatureBody, request: Request, user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionManualSignatureResult:
    access = await svc.load_operator_access(db, package_id, user)
    sig, upload = await signing.record_manual_signature(
        db, access, party=payload.party, signer_name=payload.signer_name, signer_title=payload.signer_title,
        signed_on=payload.signed_on, attestation=payload.attestation, note=payload.note,
        override_reason=payload.override_reason, scan_file_name=payload.scan_file_name,
        scan_content_type=payload.scan_content_type, request=request, initials=payload.initials,
    )
    await db.commit()
    read = await svc.serialize(db, access)
    row = next(s for r in read.revisions for s in r.signatures if s.id == sig.id)
    return ProductionManualSignatureResult(signature=row, package=read, scan_upload=upload)


@router.post("/{package_id}/signatures/{signature_id}/scan-complete", response_model=ProductionPackageRead)
async def complete_signature_scan(
    package_id: UUID, signature_id: UUID, payload: ProductionScanCompleteBody, user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    await signing.complete_scan(db, access, signature_id=signature_id, sha256=payload.sha256)
    await db.commit()
    return await svc.serialize(db, access)


@router.post("/{package_id}/execute", response_model=ProductionPackageRead)
async def execute_package(package_id: UUID, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionPackageRead:
    """Retry the execution bundle after the dealer signed but the assembly failed."""
    access = await svc.load_operator_access(db, package_id, user)
    package, final_pdf, title = await signing.execute(db, access, request=request)
    await db.commit()
    if final_pdf:
        await signing.notify_executed(db, access, title, final_pdf)
    return await svc.serialize(db, access)


@router.post("/{package_id}/final", response_model=ProductionPackageRead)
async def draft_final_package(package_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    child = await svc.draft_final(db, access)
    await db.commit()
    return await svc.serialize(db, child)


@router.get("/{package_id}/comparison", response_model=ProductionComparisonRead)
async def read_comparison(package_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ProductionComparisonRead:
    access = await svc.load_operator_access(db, package_id, user)
    target = access if int(access.package.stage or 1) == 2 else (await svc.load_operator_access(db, access.child.id, user) if access.child else None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No final package to compare yet")
    read = await svc.serialize(db, target)
    if read.comparison is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No comparison available")
    return read.comparison


@router.post("/{package_id}/sponsor-signature/adopt")
async def adopt_sponsor_signature(
    package_id: UUID, payload: ProductionReasonBody, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    access = await svc.load_operator_access(db, package_id, user)
    out = await svc.adopt_sponsor_signature(db, access, authorization_note=payload.reason, request=request)
    await db.commit()
    return out


@router.get("/{package_id}/revisions/{revision_id}/document")
async def revision_document(
    package_id: UUID, revision_id: UUID, user: CurrentUser, phase: str = "current", db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.models.production_package import ProductionPackageRevision

    access = await svc.load_operator_access(db, package_id, user)
    revision = await db.get(ProductionPackageRevision, revision_id)
    if revision is None or revision.package_id != access.package.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    if phase == "unsigned":
        key, sha = revision.rendered_pdf_s3_key, revision.rendered_pdf_sha256
    elif phase == "executed":
        key, sha = access.package.executed_pdf_s3_key, access.package.executed_pdf_sha256
    else:
        key, sha = revision.current_pdf_s3_key, revision.current_pdf_sha256
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No document for that phase yet")
    return {"url": presign_private_s3_object(key, ttl_seconds=900, download_filename=f"{revision.document_title}.pdf"),
            "sha256": sha, "phase": phase}


# ---------------------------------------------------------------------------
# the forwarded link: no account, a PIN, then a signed session
# ---------------------------------------------------------------------------
# Its own router, included before the package router: a non-UUID segment under
# /production-packages/{package_id} is a 422, not a fall-through.

link_router = APIRouter(prefix="/production-packages/link", tags=["production-packages-link"])
_LINK_SESSION_HEADER = "X-Link-Session"


def _link_ip(request: Request) -> str | None:
    from app.request_context import client_ip

    return client_ip(request)


@link_router.get("/{token}", response_model=ProductionPackageRead)
async def link_read(
    token: str, request: Request, session: str | None = Header(default=None, alias=_LINK_SESSION_HEADER),
    db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access, _s, _e = await svc.resolve_public_share(db, token, session=session, client_ip=_link_ip(request))
    await db.commit()
    return await svc.serialize(db, access)


@link_router.post("/{token}/unlock", response_model=ProductionLinkUnlocked)
async def link_unlock(
    token: str, payload: ProductionLinkUnlockBody, request: Request, db: AsyncSession = Depends(get_db),
) -> ProductionLinkUnlocked:
    access, session, expires = await svc.resolve_public_share(db, token, pin=payload.pin, client_ip=_link_ip(request))
    await db.commit()
    return ProductionLinkUnlocked(session=session or "", expires_at=expires, package=await svc.serialize(db, access))


@link_router.patch("/{token}", response_model=ProductionPackageRead)
async def link_patch(
    token: str, payload: ProductionPackagePatch, request: Request,
    session: str | None = Header(default=None, alias=_LINK_SESSION_HEADER), db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access, _s, _e = await svc.resolve_public_share(db, token, session=session, client_ip=_link_ip(request))
    await svc.apply_changes(
        db, access, changes=payload.changes, version=payload.version, confirm=payload.confirm, request=request
    )
    await db.commit()
    return await svc.serialize(db, access)


@link_router.post("/{token}/compute", response_model=ProductionComputeRead)
async def link_compute(
    token: str, payload: ProductionComputeRequest, request: Request,
    session: str | None = Header(default=None, alias=_LINK_SESSION_HEADER), db: AsyncSession = Depends(get_db),
) -> ProductionComputeRead:
    await svc.resolve_public_share(db, token, session=session, client_ip=_link_ip(request))
    await db.commit()
    return _compute(payload.arrangement, 1)


@link_router.post("/{token}/presentation", response_model=ProductionPackageRead)
async def link_generate_presentation(
    token: str, request: Request, session: str | None = Header(default=None, alias=_LINK_SESSION_HEADER),
    db: AsyncSession = Depends(get_db),
) -> ProductionPackageRead:
    access, _s, _e = await svc.resolve_public_share(db, token, session=session, client_ip=_link_ip(request))
    await signing.generate_presentation(db, access)
    await db.commit()
    return await svc.serialize(db, access)


# ---------------------------------------------------------------------------
# client surface: the intake room
# ---------------------------------------------------------------------------

public_router = APIRouter(prefix="/public/dealer-ai-intake", tags=["dealer-ai-intake-production"])


async def _room_intake(db: AsyncSession, token: str):
    from app.routers.dealer_ai_intake import _load_public_intake, _require_dealer_intake

    intake = await _load_public_intake(db, token, allow_pending_signing=True)
    _require_dealer_intake(intake)
    return intake


async def _room_business_name(db: AsyncSession, intake) -> str:
    return (intake.business_name or intake.full_name or "your business").strip()


@public_router.get("/{token}/production-package", response_model=ProductionSigningGateRead | None)
async def room_production_gate(token: str, db: AsyncSession = Depends(get_db)) -> ProductionSigningGateRead | None:
    intake = await _room_intake(db, token)
    pending = await signing.pending_client_signature(db, intake.id)
    if pending is None:
        return None
    package, revision, sig = pending
    if sig.viewed_at is None:
        sig.viewed_at = signing._now()
        await db.commit()
    return signing.gate_read(package, revision, sig, business_name=await _room_business_name(db, intake))


@public_router.get("/{token}/production-package/pdf")
async def room_production_pdf(token: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    intake = await _room_intake(db, token)
    pending = await signing.pending_client_signature(db, intake.id)
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing is waiting for your signature.")
    _package, revision, _sig = pending
    return {
        "url": presign_private_s3_object(revision.current_pdf_s3_key, ttl_seconds=900, download_filename=f"{revision.document_title}.pdf"),
        "sha256": revision.current_pdf_sha256,
        "text": revision.rendered_text,
    }


@public_router.post("/{token}/production-package/sign", response_model=ProductionClientSignResult)
async def room_production_sign(
    token: str, payload: ProductionClientSignBody, request: Request, db: AsyncSession = Depends(get_db),
) -> ProductionClientSignResult:
    intake = await _room_intake(db, token)
    result = await signing.sign_dealer(
        db, intake, revision_id=payload.revision_id, typed_name=payload.typed_name, initials=payload.initials,
        esign_consent=payload.esign_consent, acknowledged=payload.acknowledged, signature_data_url=payload.signature_data_url,
        document_sha256=payload.document_sha256, request=request,
    )
    await db.commit()
    pdf = result.pop("pdf", None)
    email = result.pop("email", None) or intake.email
    title = result.get("title") or pa.STAGE_ONE_TITLE
    executed = result.get("execution_status") == "executed"
    if pdf:
        signing.email_signed_copy(email, title, pdf, final=executed)
        if executed and result.get("package_id"):
            await signing.notify_executed_by_id(db, result["package_id"], title, pdf)
    result.pop("package_id", None)
    return ProductionClientSignResult(**result)
