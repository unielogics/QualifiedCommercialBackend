"""Production Package: presentation generation, freezing and sending,
the dealer's electronic signature, manual QC/sponsor records, execution.

Every state transition stores its evidence before it flips status: a package
is never "out for signature" without a stored, hashed PDF, and never
"executed" without the assembled bundle. Routes own the transaction; this
module flushes.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dealer_os.services import consent_delivery, storage
from app.dealer_os.services.report_pdf import PDFUnavailableError
from app.models.application_profile import ApplicationProfile, ApplicationRoomDelivery
from app.models.production_package import (
    ProductionPackage,
    ProductionPackageRevision,
    ProductionPackageShareLink,
    ProductionPackageSignature,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.production_package import ProductionSigningGateRead
from app.services import application_profiles as profiles
from app.services import production_arrangement as pa
from app.services import production_packages as pkgs
from app.services import production_presentation as pres
from app.services.payment_authorization import client_ip, decode_signature_data_url, presign_private_s3_object

logger = logging.getLogger(__name__)

ESIGN_CONSENT_VERSION = "2026-09-03-1"
ESIGN_CONSENT_TEXT = (
    "By typing my name and signing below I agree to sign this Production Commitment and Capital Engagement "
    "Agreement electronically under the U.S. E-SIGN Act and UETA, I confirm that I have read the schedules "
    "presented to me, that the figures in them are the ones I am agreeing to, and that my electronic signature "
    "has the same effect as a handwritten one. I can request a paper copy from Qualified Commercial at any time."
)
ATTESTATION_VERSION = "2026-09-03-1"
ATTESTATION_TEXT = (
    "I confirm that I hold or witnessed the original signed copy of this revision for the party named, that the "
    "signer, title and date recorded here are as they appear on that copy, and that I am recording it on behalf of "
    "Qualified Commercial LLC."
)
SIGN_PURPOSE = "sign your Production Commitment and Capital Engagement Agreement"
LEDGER_CONTEXT = "production_package_sign"
_REMIND_WINDOW = timedelta(minutes=10)
_REMINDERS: dict[UUID, datetime] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _pdf_unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail={"code": "pdf_unavailable", "message": "PDF rendering is not available in this environment."},
    )


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "storage_unavailable", "message": "Document storage is not configured; nothing was sent."},
    )


def _render(html_doc: str) -> tuple[bytes, str]:
    try:
        return pres.render_pdf(html_doc)
    except PDFUnavailableError:
        raise _pdf_unavailable()


def _meta(access: pkgs.PackageAccess, business_name: str, *, reference: str, revision_no: int | None = None) -> dict[str, Any]:
    now = _now()
    return {
        "business_name": business_name,
        "reference": reference,
        "generated_label": f"Generated {now.strftime('%B %d, %Y')}",
        "snapshot_short": pa.snapshot_hash(access.package.arrangement or {})[:12],
        "revision_no": revision_no,
    }


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

async def generate_presentation(db: AsyncSession, access: pkgs.PackageAccess) -> ProductionPackage:
    package = access.package
    if not access.capabilities().can_generate:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This package cannot be presented right now")
    arrangement = {**pa.empty_arrangement(), **(package.arrangement or {})}
    computed = pa.compute(arrangement)
    blanks = computed["attention_presentation"]
    if blanks:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "attention", "scope": "presentation", "items": blanks,
                    "message": "Fill the presentation fields before generating the PDF."},
        )
    business_name, _email, _phone = await pkgs.client_contact(db, access)
    html_doc = pres.build_presentation_html(
        arrangement, computed, meta=_meta(access, business_name, reference=f"Package {str(package.id)[:8]}")
    )
    pdf, sha = _render(html_doc)
    key = pres.presentation_key(package.profile_id, package.id, sha)
    if not storage.put_bytes(key, pdf, "application/pdf"):
        raise _storage_unavailable()
    package.presentation_s3_key = key
    package.presentation_sha256 = sha
    package.presentation_generated_at = _now()
    package.presentation_snapshot_sha256 = pa.snapshot_hash(arrangement)
    package.computed_cache = pa.jsonable(computed)
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.presented",
        "Production arrangement presentation generated",
        target_type="production_package", target_id=package.id,
        metadata={"sha256": sha, "via": "operator" if access.is_operator else "share_link"},
    )
    return package


# ---------------------------------------------------------------------------
# freeze + send
# ---------------------------------------------------------------------------

def _agreement_text(pdf: bytes, html_doc: str) -> str:
    try:
        from app.dealer_os.services.contract_sign import agreement_text
        return agreement_text(pdf)
    except Exception:  # noqa: BLE001 - text is a convenience; the hash is the record
        import re
        return re.sub(r"<[^>]+>", " ", html_doc)


async def _next_revision_no(db: AsyncSession, package_id: UUID) -> int:
    rows = (
        await db.execute(
            select(ProductionPackageRevision.revision_no).where(ProductionPackageRevision.package_id == package_id)
        )
    ).scalars().all()
    return (max(rows) if rows else 0) + 1


async def _deliver(
    db: AsyncSession, access: pkgs.PackageAccess, *, channel: str, email: str | None, phone: str | None,
    business_name: str, action: str,
) -> dict[str, Any]:
    cfg = get_settings()
    path = "/dealer-ai-underwriter?continue=1"
    result = await consent_delivery.deliver_link_checked(
        db, channel=channel, to_email=email, to_phone=phone if channel == "sms" else None,
        business_name=business_name, purpose=SIGN_PURPOSE, path=path, rep_name=access.user.name,
        origin=cfg.frontend_app_url, ledger_context=LEDGER_CONTEXT,
    )
    entry = {
        "at": _now().isoformat(), "action": action, "channel": channel,
        "recipient_email": email, "recipient_phone": phone if channel == "sms" else None,
        "emailed": bool(result.email_ok), "texted": bool(result.sms_ok), "detail": result.detail,
        "by": access.user.name,
    }
    if access.profile.primary_bucket_id is not None:
        db.add(ApplicationRoomDelivery(
            profile_id=access.profile.id, bucket_id=access.profile.primary_bucket_id, requested_document_id=None,
            action_kind=action, channel=channel, recipient_email=email,
            recipient_phone=phone if channel == "sms" else None,
            status="sent" if result.ok else "failed", detail=result.detail,
            provider_result={"accepted": result.ok, "emailed": result.email_ok, "texted": result.sms_ok},
            created_by_user_id=access.user.id,
        ))
    history = list(access.package.delivery_history or [])
    history.append(entry)
    access.package.delivery_history = history
    await db.flush()
    return {"delivered": bool(result.ok), "emailed": bool(result.email_ok), "texted": bool(result.sms_ok), "detail": result.detail}


async def _training_guard(db: AsyncSession, access: pkgs.PackageAccess, request: Request | None, *, action: str, recipient: str | None) -> None:
    if access.profile.dealer_id is None or request is None:
        return
    from app.routers.application_profiles import _require_training_live_action

    await _require_training_live_action(
        db, profile=access.profile, user=access.user, request=request, action=action,
        provider="ses+sms", recipient=recipient, effect="Emails and texts the client a signing request",
    )


async def send(
    db: AsyncSession, access: pkgs.PackageAccess, *, channel: str, recipient_email: str | None,
    recipient_phone: str | None, request: Request | None,
) -> dict[str, Any]:
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    if package.status == "out_for_signature":
        return {"already_sent": True, "delivered": False, "emailed": False, "texted": False,
                "detail": "This package is already out for signature."}
    if not access.capabilities().can_send:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the desk can request a signature")
    arrangement = {**pa.empty_arrangement(), **(package.arrangement or {})}
    computed = pa.compute(arrangement)
    if computed["attention"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "attention", "scope": "stage_one", "items": computed["attention"],
                    "message": "Clear the open items before requesting a signature — a blank field is not enforceable."},
        )
    business_name, intake_email, intake_phone = await pkgs.client_contact(db, access)
    email = (recipient_email or intake_email or "").strip()
    phone = (recipient_phone or intake_phone or "").strip() or None
    if "@" not in email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The client needs an email address to sign in.")
    await _training_guard(db, access, request, action="production_package.send", recipient=email)
    company, agreement = await pkgs.require_signed_sponsor(db, package)
    sponsor = pkgs.sponsor_snapshot(company, agreement, arrangement)
    parties = {
        "dealer": {"name": arrangement.get("dealer_name"), "signer_name": arrangement.get("dealer_signer_name"),
                   "signer_title": arrangement.get("dealer_signer_title"), "email": email, "phone": phone},
        "qc": {"name": "Qualified Commercial LLC"},
        "relationship_manager": {"name": arrangement.get("rm_name"), "email": arrangement.get("rm_email")},
    }
    revision_no = await _next_revision_no(db, package.id)
    snapshot = pa.canonical_snapshot(arrangement, computed, sponsor=sponsor, parties=parties)
    content_sha = pa.snapshot_hash(arrangement, extra={"sponsor": sponsor, "parties": parties})
    html_doc = pres.build_agreement_html(
        arrangement, computed, sponsor=sponsor,
        meta=_meta(access, business_name, reference=f"QC-PA-{str(package.id)[:8].upper()}-R{revision_no}", revision_no=revision_no),
    )
    pdf, pdf_sha = _render(html_doc)
    key = pres.revision_key(package.profile_id, package.id, revision_no, "unsigned", pdf_sha)
    if not storage.put_bytes(key, pdf, "application/pdf"):
        raise _storage_unavailable()
    now = _now()
    revision = ProductionPackageRevision(
        package_id=package.id, revision_no=revision_no, stage=package.stage, status="out_for_signature",
        document_key=pa.STAGE_ONE_DOCUMENT_KEY, document_title=pa.STAGE_ONE_TITLE, document_version=pa.DOCUMENT_VERSION,
        snapshot=snapshot, content_sha256=content_sha, rendered_text=_agreement_text(pdf, html_doc),
        rendered_pdf_s3_key=key, rendered_pdf_sha256=pdf_sha, current_pdf_s3_key=key, current_pdf_sha256=pdf_sha,
        created_by_user_id=access.user.id, sent_at=now,
    )
    db.add(revision)
    await db.flush()
    dealer_sig = ProductionPackageSignature(
        package_id=package.id, revision_id=revision.id, stage=package.stage, party="dealer", method="electronic",
        status="pending", expected_signer_name=arrangement.get("dealer_signer_name"), sent_at=now,
    )
    db.add(dealer_sig)
    package.frozen_revision_id = revision.id
    package.status = "out_for_signature"
    package.sent_at = now
    package.sent_by_user_id = access.user.id
    package.computed_cache = pa.jsonable(computed)
    package.attention = []
    package.version = (package.version or 1) + 1
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.sent_for_signature",
        f"Production package sent for signature to {email}",
        target_type="production_package", target_id=package.id,
        metadata={"revision_no": revision_no, "content_sha256": content_sha[:16], "pdf_sha256": pdf_sha[:16],
                  "recipient_email": email, "recipient_phone": phone, "channel": channel,
                  "sponsor": sponsor.get("name"), "sponsor_agreement": (sponsor.get("agreement") or {}).get("contract_number")},
    )
    delivery = await _deliver(db, access, channel=channel, email=email, phone=phone, business_name=business_name, action="production_package_sign")
    return {"already_sent": False, **delivery}


async def remind(db: AsyncSession, access: pkgs.PackageAccess, *, channel: str, request: Request | None) -> dict[str, Any]:
    package = access.package
    if package.status != "out_for_signature" or not access.is_operator or access.role not in pkgs.SEND_ROLES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a package that is out for signature can be reminded")
    last = _REMINDERS.get(package.id)
    if last is not None and _now() - last < _REMIND_WINDOW:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "A reminder went out in the last ten minutes.")
    business_name, email, phone = await pkgs.client_contact(db, access)
    if not email or "@" not in email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The client has no email address on file.")
    await _training_guard(db, access, request, action="production_package.remind", recipient=email)
    _REMINDERS[package.id] = _now()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.reminder_sent", f"Signing reminder sent to {email}",
        target_type="production_package", target_id=package.id, metadata={"channel": channel},
    )
    return await _deliver(db, access, channel=channel, email=email, phone=phone, business_name=business_name, action="production_package_reminder")


async def _void_revision(db: AsyncSession, package: ProductionPackage, *, reason: str, user_id: UUID, revision_status: str) -> None:
    if package.frozen_revision_id is None:
        return
    revision = await db.get(ProductionPackageRevision, package.frozen_revision_id)
    now = _now()
    sigs = (
        await db.execute(
            select(ProductionPackageSignature).where(
                ProductionPackageSignature.revision_id == package.frozen_revision_id,
                ProductionPackageSignature.status.in_(("pending", "signed")),
            )
        )
    ).scalars().all()
    for s in sigs:
        s.status = "voided"
        s.voided_at = now
        s.void_reason = reason
    if revision is not None and revision.status != "executed":
        revision.status = revision_status
        revision.voided_at = now
        revision.voided_by_user_id = user_id
        revision.void_reason = reason


async def reopen(db: AsyncSession, access: pkgs.PackageAccess, *, reason: str) -> ProductionPackage:
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    if package.status == "executed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Executed packages are immutable")
    if package.status != "out_for_signature" or not access.capabilities().can_reopen:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a package that is out for signature can be reopened")
    await _void_revision(db, package, reason=f"reopened: {reason}", user_id=access.user.id, revision_status="void")
    package.status = "draft"
    package.frozen_revision_id = None
    package.version = (package.version or 1) + 1
    package.updated_by_user_id = access.user.id
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.reopened", f"Production package reopened: {reason}",
        target_type="production_package", target_id=package.id, metadata={"reason": reason},
    )
    return package


async def void(db: AsyncSession, access: pkgs.PackageAccess, *, reason: str) -> ProductionPackage:
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    if package.status == "executed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Executed packages are immutable")
    if not access.capabilities().can_void:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    await _void_revision(db, package, reason=f"voided: {reason}", user_id=access.user.id, revision_status="void")
    revoked = await pkgs.revoke_all_share_links(db, package, access.user)
    now = _now()
    package.status = "void"
    package.voided_at = now
    package.voided_by_user_id = access.user.id
    package.void_reason = reason
    package.version = (package.version or 1) + 1
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.voided", f"Production package voided: {reason}",
        target_type="production_package", target_id=package.id, metadata={"reason": reason, "share_links_revoked": revoked},
    )
    return package


# ---------------------------------------------------------------------------
# client gate + signature
# ---------------------------------------------------------------------------

async def pending_client_signature(
    db: AsyncSession, intake_id: UUID | None
) -> tuple[ProductionPackage, ProductionPackageRevision, ProductionPackageSignature] | None:
    """Derived, never stored: the dealer still owes a signature on the frozen revision."""
    if intake_id is None:
        return None
    package = (
        await db.execute(
            select(ProductionPackage).where(
                ProductionPackage.intake_id == intake_id,
                ProductionPackage.status == "out_for_signature",
                ProductionPackage.frozen_revision_id.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if package is None:
        return None
    sig = (
        await db.execute(
            select(ProductionPackageSignature).where(
                ProductionPackageSignature.revision_id == package.frozen_revision_id,
                ProductionPackageSignature.party == "dealer",
                ProductionPackageSignature.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if sig is None:
        return None
    revision = await db.get(ProductionPackageRevision, package.frozen_revision_id)
    if revision is None:
        return None
    return package, revision, sig


def gate_read(
    package: ProductionPackage, revision: ProductionPackageRevision, sig: ProductionPackageSignature, *, business_name: str,
) -> ProductionSigningGateRead:
    snap = revision.snapshot or {}
    dealer = (snap.get("parties") or {}).get("dealer") or {}
    return ProductionSigningGateRead(
        package_id=package.id, revision_id=revision.id, revision_no=revision.revision_no,
        title=revision.document_title, document_version=revision.document_version,
        content_sha256=revision.content_sha256, pdf_sha256=revision.current_pdf_sha256,
        pdf_url=presign_private_s3_object(revision.current_pdf_s3_key, ttl_seconds=900, download_filename=f"{revision.document_title}.pdf"),
        signer_name=sig.expected_signer_name or dealer.get("signer_name") or "",
        signer_title=dealer.get("signer_title"), business_name=business_name,
        sent_at=sig.sent_at, esign_consent_text=ESIGN_CONSENT_TEXT, esign_consent_version=ESIGN_CONSENT_VERSION,
        already_signed=sig.status == "signed",
    )


def _normalize_name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _stamp_party(
    pdf_bytes: bytes, *, anchor: str, placeholder: str, date_placeholder: str,
    typed_name: str, signature_png: bytes | None, signed_at: datetime | date,
) -> bytes:
    """Lay a signature (image or /s/ typed adoption) and a date onto the
    party's own signature block, found by its anchor text."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        marks = page.search_for(anchor)
        if not marks:
            continue
        a = marks[0]

        def below(text: str) -> Any:
            cands = [r for r in page.search_for(text) if r.y0 > a.y1 - 2]
            return min(cands, key=lambda r: r.y0) if cands else None

        sig_rect = below(placeholder) or fitz.Rect(a.x0, a.y1 + 40, min(a.x0 + 210, page.rect.width - 260), a.y1 + 82)
        date_rect = below(date_placeholder) or fitz.Rect(page.rect.width - 230, sig_rect.y0, page.rect.width - 40, sig_rect.y1)
        for r in (sig_rect, date_rect):
            page.add_redact_annot(fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2), fill=(1, 1, 1))
        page.apply_redactions()
        sx, sy = sig_rect.x0, sig_rect.y1 - 2
        if signature_png:
            rect = fitz.Rect(sx, sy - 38, min(sx + 190, page.rect.width - 36), sy + 2)
            page.insert_image(rect, stream=signature_png, keep_proportion=True)
        else:
            page.insert_text((sx, sy), f"/s/ {typed_name}", fontname="helv", fontsize=11, color=(0.08, 0.15, 0.36))
        when = signed_at.strftime("%B %d, %Y")
        page.insert_text((date_rect.x0, date_rect.y1 - 2), when, fontname="helv", fontsize=8, color=(0.08, 0.15, 0.36))
        return doc.tobytes(deflate=True)
    raise ValueError(f"Signature block not found: {anchor}")


def _append_pages(pdf_bytes: bytes, extra_pdf: bytes) -> bytes:
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    extra = fitz.open(stream=extra_pdf, filetype="pdf")
    doc.insert_pdf(extra)
    return doc.tobytes(deflate=True)


def _certificate(rows: list[tuple[str, str]], title: str) -> bytes:
    from app.dealer_os.services.contract_sign import _certificate_page

    try:
        return _certificate_page(rows, title)
    except ImportError as exc:
        raise PDFUnavailableError(str(exc)) from exc


async def _current_pdf(revision: ProductionPackageRevision) -> bytes:
    raw = storage.get_bytes(revision.current_pdf_s3_key or revision.rendered_pdf_s3_key or "")
    if raw is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The agreement PDF could not be read from storage.")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != (revision.current_pdf_sha256 or revision.rendered_pdf_sha256):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "fingerprint_mismatch",
                    "message": "The stored agreement no longer matches its recorded fingerprint. Ask the desk to regenerate it."},
        )
    return raw


async def sign_dealer(
    db: AsyncSession, intake: PublicUnderwritingIntake, *, revision_id: UUID, typed_name: str, esign_consent: bool,
    acknowledged: bool, signature_data_url: str, document_sha256: str, request: Request | None,
) -> dict[str, Any]:
    pending = await pending_client_signature(db, intake.id)
    if pending is None:
        # Idempotent: a signed row for the frozen revision returns the same answer.
        package = (
            await db.execute(select(ProductionPackage).where(ProductionPackage.intake_id == intake.id))
        ).scalar_one_or_none()
        if package is not None and package.frozen_revision_id == revision_id:
            done = (
                await db.execute(
                    select(ProductionPackageSignature).where(
                        ProductionPackageSignature.revision_id == revision_id,
                        ProductionPackageSignature.party == "dealer",
                        ProductionPackageSignature.status == "signed",
                    )
                )
            ).scalar_one_or_none()
            if done is not None:
                return {"signed": True, "signed_at": done.signed_at, "pdf_sha256": done.signed_pdf_sha256,
                        "download_url": presign_private_s3_object(done.signed_pdf_s3_key, ttl_seconds=3600),
                        "execution_status": "signed"}
        raise HTTPException(status.HTTP_409_CONFLICT, "There is nothing waiting for your signature.")
    package, revision, sig = pending
    package = await db.get(ProductionPackage, package.id, with_for_update=True)
    if revision.id != revision_id or package.frozen_revision_id != revision_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This agreement was updated. Reload to see the current version.")
    if not esign_consent or not acknowledged:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please read the agreement and accept electronic signing first.")
    expected = sig.expected_signer_name or ""
    if _normalize_name(typed_name) != _normalize_name(expected):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "typed_name_mismatch", "expected": expected,
                    "message": f"Type your name exactly as it appears on the agreement: {expected}"},
        )
    png, png_sha, _ct = decode_signature_data_url(signature_data_url)
    if not png:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Draw your signature to continue.")
    raw = await _current_pdf(revision)
    if document_sha256 != (revision.current_pdf_sha256 or revision.rendered_pdf_sha256):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "fingerprint_mismatch", "message": "The agreement you reviewed is not the current copy. Reload and review again."},
        )
    now = _now()
    ip = client_ip(request)
    ua = (request.headers.get("user-agent", "") if request else "")[:400]
    try:
        stamped = _stamp_party(
            raw, anchor=pres.DEALER_ANCHOR, placeholder=pres.ELECTRONIC_PLACEHOLDER,
            date_placeholder=pres.ELECTRONIC_DATE_PLACEHOLDER, typed_name=typed_name, signature_png=png, signed_at=now,
        )
    except ImportError as exc:
        raise _pdf_unavailable() from exc
    signed_body_sha = hashlib.sha256(stamped).hexdigest()
    rows = [
        ("Agreement", revision.document_title),
        ("Revision", f"{revision.revision_no} · version {revision.document_version}"),
        ("Dealer", str((revision.snapshot or {}).get("arrangement", {}).get("dealer_name") or "")),
        ("Signer", typed_name),
        ("Signer title", str((revision.snapshot or {}).get("arrangement", {}).get("dealer_signer_title") or "")),
        ("Signature method", "Drawn on device"),
        ("Signature SHA-256", png_sha or ""),
        ("Content SHA-256 (frozen schedules)", revision.content_sha256),
        ("Document SHA-256 (pre-signing)", revision.current_pdf_sha256 or revision.rendered_pdf_sha256 or ""),
        ("Signed agreement SHA-256 (before certificate)", signed_body_sha),
        ("E-SIGN consent", f"version {ESIGN_CONSENT_VERSION} · {now.isoformat()}"),
        ("Consent text", ESIGN_CONSENT_TEXT),
        ("Signed at", now.isoformat()),
        ("IP address", ip or ""),
        ("Device", ua[:220]),
    ]
    try:
        cert = _certificate(rows, revision.document_title)
    except PDFUnavailableError:
        raise _pdf_unavailable()
    final = _append_pages(stamped, cert)
    final_sha = hashlib.sha256(final).hexdigest()
    key = pres.revision_key(package.profile_id, package.id, revision.revision_no, "dealer-signed", final_sha)
    if not storage.put_bytes(key, final, "application/pdf"):
        raise _storage_unavailable()
    png_key = pres.revision_key(package.profile_id, package.id, revision.revision_no, "dealer-signature", png_sha or final_sha, ext="png")
    storage.put_bytes(png_key, png, "image/png")
    sig.status = "signed"
    sig.typed_name = typed_name
    sig.signature_s3_key = png_key
    sig.signature_sha256 = png_sha
    sig.document_sha256 = revision.current_pdf_sha256 or revision.rendered_pdf_sha256
    sig.esign_consent_version = ESIGN_CONSENT_VERSION
    sig.esign_consent_at = now
    sig.esign_consent_ip = ip
    sig.ip_address = ip
    sig.user_agent = ua
    sig.signed_at = now
    sig.signed_pdf_s3_key = key
    sig.signed_pdf_sha256 = final_sha
    sig.certificate_sha256 = hashlib.sha256(cert).hexdigest()
    revision.current_pdf_s3_key = key
    revision.current_pdf_sha256 = final_sha
    package.version = (package.version or 1) + 1
    await db.flush()
    profile = await db.get(ApplicationProfile, package.profile_id)
    await profiles.log_profile_action(
        db, profile, None, "production_package.dealer_signed",
        f"{typed_name} signed the production commitment electronically",
        target_type="production_package", target_id=package.id,
        metadata={"revision_no": revision.revision_no, "signed_pdf_sha256": final_sha[:16], "ip": ip},
    )
    return {"signed": True, "signed_at": now, "pdf_sha256": final_sha,
            "download_url": presign_private_s3_object(key, ttl_seconds=3600, download_filename=f"{revision.document_title}.pdf"),
            "execution_status": "signed", "pdf": final, "email": (revision.snapshot or {}).get("parties", {}).get("dealer", {}).get("email")}


def email_signed_copy(to: str | None, title: str, pdf: bytes, *, final: bool = False) -> bool:
    if not to or "@" not in to:
        return False
    from app.services.email.ses_client import send_raw_email

    try:
        sent = send_raw_email(
            to_emails=[to],
            subject=f"Your {'executed' if final else 'signed'} {title} — Qualified Commercial",
            body_text=(
                f"Attached is your {'fully executed' if final else 'signed'} {title}. The package includes a certificate "
                "with the document fingerprints. Keep this copy for your records.\n\nQualified Commercial"
            ),
            attachments=[(f"{title.replace(' ', '-')}-{'executed' if final else 'signed'}.pdf", pdf, "application/pdf")],
        )
        return bool(getattr(sent, "ok", False))
    except Exception:  # noqa: BLE001 - the signature stands; delivery is retryable
        logger.exception("production package signed-copy email failed")
        return False


# ---------------------------------------------------------------------------
# manual QC / sponsor records and execution
# ---------------------------------------------------------------------------

async def _frozen_revision(db: AsyncSession, package: ProductionPackage) -> ProductionPackageRevision:
    revision = await db.get(ProductionPackageRevision, package.frozen_revision_id) if package.frozen_revision_id else None
    if revision is None or package.status != "out_for_signature":
        raise HTTPException(status.HTTP_409_CONFLICT, "This package is not out for signature.")
    return revision


async def record_manual_signature(
    db: AsyncSession, access: pkgs.PackageAccess, *, party: str, signer_name: str, signer_title: str,
    signed_on: date, attestation: bool, note: str | None, override_reason: str | None,
    scan_file_name: str | None, scan_content_type: str | None, request: Request | None,
) -> tuple[ProductionPackageSignature, dict[str, Any] | None]:
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    if not access.capabilities().can_record:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required to record a signature")
    if party not in ("qc", "sponsor"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The dealer signs electronically; only QC and sponsor signatures are recorded.")
    revision = await _frozen_revision(db, package)
    if not attestation:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Confirm the attestation to record a signature.")
    if signed_on < revision.created_at.date() or signed_on > _now().date():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "signed_on_out_of_range", "message": "A signature cannot predate the document it signs or be in the future."},
        )
    live = (
        await db.execute(
            select(ProductionPackageSignature).where(
                ProductionPackageSignature.revision_id == revision.id,
                ProductionPackageSignature.party == party,
                ProductionPackageSignature.status.in_(("pending", "signed")),
            )
        )
    ).scalar_one_or_none()
    if live is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {party} signature is already recorded on this revision.")
    sponsor_snap = (revision.snapshot or {}).get("sponsor") or {}
    if party == "sponsor":
        expected = sponsor_snap.get("signer_name") or ""
        if expected and _normalize_name(expected) != _normalize_name(signer_name) and not (override_reason or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "sponsor_signer_mismatch", "expected": expected,
                        "message": f"The sponsor's agreement names {expected}. Give a reason to record a different signer."},
            )
    raw = await _current_pdf(revision)
    anchor = pres.SPONSOR_ANCHOR if party == "sponsor" else pres.QC_ANCHOR
    try:
        stamped = _stamp_party(
            raw, anchor=anchor, placeholder=pres.RECORDED_PLACEHOLDER, date_placeholder=pres.RECORDED_DATE_PLACEHOLDER,
            typed_name=f"{signer_name}, {signer_title}", signature_png=None, signed_at=signed_on,
        )
    except ImportError as exc:
        raise _pdf_unavailable() from exc
    sha = hashlib.sha256(stamped).hexdigest()
    key = pres.revision_key(package.profile_id, package.id, revision.revision_no, f"{party}-recorded", sha)
    if not storage.put_bytes(key, stamped, "application/pdf"):
        raise _storage_unavailable()
    now = _now()
    sig = ProductionPackageSignature(
        package_id=package.id, revision_id=revision.id, stage=package.stage, party=party, method="manual", status="signed",
        signer_name=signer_name.strip(), signer_title=signer_title.strip(), signed_on=signed_on,
        attestation_version=ATTESTATION_VERSION, recorded_by_user_id=access.user.id, recorded_at=now,
        recorded_ip=client_ip(request), recorded_user_agent=(request.headers.get("user-agent", "")[:400] if request else None),
        note=(note or "").strip() or None, signed_at=now, document_sha256=revision.current_pdf_sha256,
        signed_pdf_s3_key=key, signed_pdf_sha256=sha,
    )
    db.add(sig)
    revision.current_pdf_s3_key = key
    revision.current_pdf_sha256 = sha
    package.version = (package.version or 1) + 1
    await db.flush()
    upload: dict[str, Any] | None = None
    if scan_file_name:
        ext = (scan_file_name.rsplit(".", 1)[-1].lower() if "." in scan_file_name else "pdf")[:5]
        scan_key = pres.scan_key(package.profile_id, package.id, revision.revision_no, party, sha, ext)
        contract = storage.presign_put(scan_key, content_type=scan_content_type or "application/pdf")
        if contract is None:
            raise _storage_unavailable()
        sig.scan_s3_key = scan_key
        await db.flush()
        upload = {"signature_id": str(sig.id), "key": scan_key, **contract}
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.manual_signature_recorded",
        f"{party.upper() if party == 'qc' else 'Sponsor'} signature recorded: {signer_name}",
        target_type="production_package", target_id=package.id,
        metadata={"party": party, "signer_name": signer_name, "signer_title": signer_title, "signed_on": signed_on.isoformat(),
                  "override_reason": override_reason, "revision_no": revision.revision_no, "pdf_sha256": sha[:16]},
    )
    return sig, upload


async def complete_scan(db: AsyncSession, access: pkgs.PackageAccess, *, signature_id: UUID, sha256: str) -> ProductionPackageSignature:
    if not access.is_operator or access.role not in pkgs.RECORD_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    sig = await db.get(ProductionPackageSignature, signature_id)
    if sig is None or sig.package_id != access.package.id or not sig.scan_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signature not found")
    raw = storage.get_bytes(sig.scan_s3_key)
    if raw is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The scan has not been uploaded yet.")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != sha256.lower():
        raise HTTPException(status.HTTP_409_CONFLICT, "The uploaded scan does not match the fingerprint you sent.")
    sig.scan_sha256 = actual
    await db.flush()
    return sig


async def execute(db: AsyncSession, access: pkgs.PackageAccess, *, request: Request | None) -> tuple[ProductionPackage, bytes | None]:
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    if package.status == "executed":
        return package, None
    if not access.capabilities().can_execute:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required to execute")
    revision = await _frozen_revision(db, package)
    sigs = (
        await db.execute(
            select(ProductionPackageSignature).where(
                ProductionPackageSignature.revision_id == revision.id,
                ProductionPackageSignature.status == "signed",
            )
        )
    ).scalars().all()
    by_party = {s.party: s for s in sigs}
    missing = [p for p in ("dealer", "qc", "sponsor") if p not in by_party]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "signatures_missing", "missing": missing,
                    "message": "Every party must sign the same revision before execution."},
        )
    raw = await _current_pdf(revision)
    now = _now()
    dealer_sig = by_party["dealer"]
    rows: list[tuple[str, str]] = [
        ("Agreement", revision.document_title),
        ("Revision", f"{revision.revision_no} · version {revision.document_version}"),
        ("Content SHA-256 (frozen schedules)", revision.content_sha256),
        ("Unsigned document SHA-256", revision.rendered_pdf_sha256 or ""),
        ("Dealer signature", f"{dealer_sig.typed_name} · electronic · {dealer_sig.signed_at.isoformat() if dealer_sig.signed_at else ''}"),
        ("Dealer-signed document SHA-256", dealer_sig.signed_pdf_sha256 or ""),
    ]
    for party, label in (("sponsor", "Sponsor signature"), ("qc", "Qualified Commercial signature")):
        s = by_party[party]
        rows.append((label, f"{s.signer_name}, {s.signer_title} · signed {s.signed_on.isoformat() if s.signed_on else ''} · "
                            f"recorded {s.recorded_at.isoformat() if s.recorded_at else ''} · scan {s.scan_sha256[:16] if s.scan_sha256 else 'no scan on file'} · "
                            f"attestation {s.attestation_version}"))
    rows.append(("Current document SHA-256 (before execution record)", revision.current_pdf_sha256 or ""))
    rows.append(("Executed by", f"{access.user.name} · {now.isoformat()} · {client_ip(request) or ''}"))
    rows.append(("Attestation text", ATTESTATION_TEXT))
    try:
        record = _certificate(rows, f"Execution Record — {revision.document_title}")
    except PDFUnavailableError:
        raise _pdf_unavailable()
    final = _append_pages(raw, record)
    final_sha = hashlib.sha256(final).hexdigest()
    key = pres.revision_key(package.profile_id, package.id, revision.revision_no, "executed", final_sha)
    if not storage.put_bytes(key, final, "application/pdf"):
        raise _storage_unavailable()
    package.status = "executed"
    package.executed_at = now
    package.executed_by_user_id = access.user.id
    package.executed_pdf_s3_key = key
    package.executed_pdf_sha256 = final_sha
    package.version = (package.version or 1) + 1
    revision.status = "executed"
    revision.completed_at = now
    revision.current_pdf_s3_key = key
    revision.current_pdf_sha256 = final_sha
    await pkgs.revoke_all_share_links(db, package, access.user)
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.executed",
        "Production commitment fully executed",
        target_type="production_package", target_id=package.id,
        metadata={"revision_no": revision.revision_no, "executed_sha256": final_sha[:16]},
    )
    return package, final


async def delete_guard(db: AsyncSession, profile_id: UUID) -> None:
    """Two-step lead deletion guard: refuse while a package is out for signature or executed."""
    package = (
        await db.execute(select(ProductionPackage).where(ProductionPackage.profile_id == profile_id))
    ).scalar_one_or_none()
    if package is None:
        return
    if package.status == "out_for_signature":
        raise HTTPException(status.HTTP_409_CONFLICT, "Void the outstanding Production Package before deleting this file.")
    if package.status == "executed":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This file holds an executed Production Package, which is a retained record. Archive it instead of deleting it.",
        )
