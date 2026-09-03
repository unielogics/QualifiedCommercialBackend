"""Production Package routes.

Operator surface under /production-packages (visibility through the
application profile), the rep surface under /production-packages/shares/{token}
(a signed-in rep plus their unique link), and the client surface under
/public/dealer-ai-intake/{token}/production-package (the intake room).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.schemas.production_package import (
    ProductionCapabilitiesRead,
    ProductionComputeRead,
    ProductionComputeRequest,
    ProductionHistoryRead,
    ProductionPackagePatch,
    ProductionPackageRead,
    ProductionPackageResolve,
    ProductionPrefillRequest,
    ProductionShareLinkCreate,
    ProductionShareLinkCreated,
    ProductionSmsConsentCapture,
    ProductionSmsConsentRead,
    SponsorOptionRead,
)
from app.services import production_arrangement as pa
from app.services import production_packages as svc

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


@router.post("/resolve", response_model=ProductionPackageRead)
async def resolve_production_package(
    payload: ProductionPackageResolve, user: CurrentUser, db: AsyncSession = Depends(get_db)
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
    token: str, payload: ProductionComputeRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ProductionComputeRead:
    await svc.resolve_rep_share(db, user, token)
    await db.commit()
    return _compute(payload.arrangement)


@router.post("/shares/{token}/prefill")
async def rep_share_prefill(
    token: str, payload: ProductionPrefillRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    access = await svc.resolve_rep_share(db, user, token)
    out = await svc.run_prefill(db, access, force=payload.force, fields=payload.fields, apply=payload.apply)
    await db.commit()
    return out


# ---- operator surface ----

def _compute(arrangement: dict[str, Any]) -> ProductionComputeRead:
    merged = pa.merge_changes(pa.empty_arrangement(), arrangement or {})
    computed = pa.jsonable(pa.compute(merged))
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
    package_id: UUID, payload: ProductionPrefillRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    access = await svc.load_operator_access(db, package_id, user)
    out = await svc.run_prefill(db, access, force=payload.force, fields=payload.fields, apply=payload.apply)
    await db.commit()
    return out


@router.post("/{package_id}/compute", response_model=ProductionComputeRead)
async def compute_production_package(
    package_id: UUID, payload: ProductionComputeRequest, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ProductionComputeRead:
    await svc.load_operator_access(db, package_id, user)
    return _compute(payload.arrangement)


@router.post("/{package_id}/share-links", response_model=ProductionShareLinkCreated, status_code=201)
async def create_share_link(
    package_id: UUID, payload: ProductionShareLinkCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ProductionShareLinkCreated:
    access = await svc.load_operator_access(db, package_id, user)
    link, token = await svc.mint_share_link(
        db, access, rep_user_id=payload.rep_user_id, label=payload.label,
        expires_in_days=payload.expires_in_days, outside_book=payload.outside_book,
    )
    await db.commit()
    read = await svc.serialize(db, access)
    row = next(l for l in read.share_links if l.id == link.id)
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

from datetime import date as _date  # noqa: E402

from fastapi import Response  # noqa: E402

from app.schemas.production_package import (  # noqa: E402
    ProductionClientSignBody,
    ProductionClientSignResult,
    ProductionManualSignatureBody,
    ProductionManualSignatureResult,
    ProductionPresentationRead,
    ProductionReasonBody,
    ProductionScanCompleteBody,
    ProductionSendRequest,
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
    package_id: UUID, payload: ProductionReasonBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ProductionPackageRead:
    access = await svc.load_operator_access(db, package_id, user)
    await signing.reopen(db, access, reason=payload.reason)
    await db.commit()
    return await svc.serialize(db, access)


@router.post("/{package_id}/void", response_model=ProductionPackageRead)
async def void_package(
    package_id: UUID, payload: ProductionReasonBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
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
        scan_content_type=payload.scan_content_type, request=request,
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
    access = await svc.load_operator_access(db, package_id, user)
    package, final_pdf = await signing.execute(db, access, request=request)
    await db.commit()
    if final_pdf:
        _business, email, _phone = await svc.client_contact(db, access)
        signing.email_signed_copy(email, pa.STAGE_ONE_TITLE, final_pdf, final=True)
    return await svc.serialize(db, access)


@router.get("/{package_id}/revisions/{revision_id}/document")
async def revision_document(
    package_id: UUID, revision_id: UUID, user: CurrentUser, phase: str = "current", db: AsyncSession = Depends(get_db)
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
    token: str, payload: ProductionClientSignBody, request: Request, db: AsyncSession = Depends(get_db)
) -> ProductionClientSignResult:
    intake = await _room_intake(db, token)
    result = await signing.sign_dealer(
        db, intake, revision_id=payload.revision_id, typed_name=payload.typed_name, esign_consent=payload.esign_consent,
        acknowledged=payload.acknowledged, signature_data_url=payload.signature_data_url,
        document_sha256=payload.document_sha256, request=request,
    )
    await db.commit()
    pdf = result.pop("pdf", None)
    email = result.pop("email", None) or intake.email
    if pdf:
        signing.email_signed_copy(email, pa.STAGE_ONE_TITLE, pdf)
    return ProductionClientSignResult(**result)
