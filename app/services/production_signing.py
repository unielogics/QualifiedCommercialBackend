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
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
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
    ProductionPackageSignature,
)
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.production_package import ProductionSigningGateRead
from app.services import application_profiles as profiles
from app.services import production_arrangement as pa
from app.services import production_packages as pkgs
from app.services import production_presentation as pres
from app.services.payment_authorization import (
    client_ip,
    decode_signature_data_url,
    presign_private_s3_object,
)

logger = logging.getLogger(__name__)

ESIGN_CONSENT_VERSION = "2026-09-03-2"
ESIGN_CONSENT_TEXT = (
    "By typing my name and initials and signing below I agree to sign this Production Commitment and Capital "
    "Engagement Agreement electronically under the U.S. E-SIGN Act and UETA; I adopt my typed initials wherever "
    "the agreement calls for them; I confirm that I have read the agreement and its schedules, that the figures in "
    "them are the ones I am agreeing to, and that my electronic signature has the same effect as a handwritten one. "
    "I can request a paper copy from Qualified Commercial at any time."
)
ESIGN_CONSENT_TEXT_STAGE_TWO = (
    "By typing my name and initials and signing below I agree to sign this Program Activation and Production "
    "Agreement, its Addendum A, its Schedules and the Funding Activation Certificate electronically under the U.S. "
    "E-SIGN Act and UETA; I adopt my typed initials wherever the agreement calls for them; I confirm that I have "
    "reviewed the completed Addendum A and the changes since my Production Commitment, that the figures in this "
    "Agreement are the ones I am agreeing to, and that my electronic signature has the same effect as a handwritten "
    "one. I can request a paper copy from Qualified Commercial at any time."
)
ACKNOWLEDGEMENT_TEXT = {
    1: "I have read the agreement and the figures in it are the ones I am agreeing to.",
    2: "I have reviewed the completed Addendum A and the changes listed above, and the figures in this Agreement are the ones I am agreeing to.",
}
REVIEW_CLAUSE = "Production Commitment §4.7"
REVIEW_NOTE = (
    "Under §4.7 of your Production Commitment you are entitled to review the completed Addendum A before signing. "
    "Where a figure appears in both agreements, this Agreement controls (§4.8 of the Commitment, §1.8 here)."
)
FUNDING_ATTESTATION_VERSION = "2026-09-03-1"
FUNDING_ATTESTATION_TEXT = (
    "I confirm that the Funding Party named on the certificate disbursed the stated amount to the Dealer, that the "
    "funds cleared on the stated date, and that the amount is at or above the Minimum Activation Amount."
)
STAGE_TWO_SEND_AFTER_FUNDING = True
PLACED_PARTIES: tuple[str, ...] = ("qc", "sponsor", "rm")
ATTESTATION_VERSION = "2026-09-03-1"
ATTESTATION_TEXT = (
    "I confirm that I hold or witnessed the original signed copy of this revision for the party named, that the "
    "signer, title and date recorded here are as they appear on that copy, and that I am recording it on behalf of "
    "Qualified Commercial LLC."
)
SIGN_PURPOSE = "sign your Production Commitment and Capital Engagement Agreement"
SIGN_PURPOSE_STAGE_TWO = "sign your Program Activation and Production Agreement"
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
    except PDFUnavailableError as exc:
        raise _pdf_unavailable() from exc


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
    import re

    try:
        from app.dealer_os.services.contract_sign import agreement_text
        text = agreement_text(pdf)
    except Exception:  # noqa: BLE001 - text is a convenience; the hash is the record
        text = re.sub(r"<[^>]+>", " ", html_doc)
    return re.sub(r"\[\[(?:SIG|DATE|INI):[a-z]+:\d+\]\]", "", text)


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
    purpose = SIGN_PURPOSE_STAGE_TWO if int(getattr(access.package, "stage", 1) or 1) == 2 else SIGN_PURPOSE
    result = await consent_delivery.deliver_link_checked(
        db, channel=channel, to_email=email, to_phone=phone if channel == "sms" else None,
        business_name=business_name, purpose=purpose, path=path, rep_name=access.user.name,
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


async def _resolve_placed_signatures(db: AsyncSession, access: pkgs.PackageAccess, arrangement: dict[str, Any]) -> dict[str, Any]:
    """The stored signatures to place on this package: qc, sponsor and the relationship manager.
    Raises 409 signature_on_file_missing with the fix when any is absent."""
    from app.services import stored_signatures as sigs_svc

    status_map = await pkgs.signatures_on_file_status(db, access)
    missing = [p for p in PLACED_PARTIES if not status_map[p]["present"]]
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "signature_on_file_missing", "parties": missing,
                    "how_to_fix": {p: status_map[p]["how_to_fix"] for p in missing},
                    "message": "Every counterparty signature must be on file before the package can be sent."},
        )
    out: dict[str, Any] = {}
    out["qc"] = await sigs_svc.current(db, "qc", None)
    out["sponsor"] = await sigs_svc.current(db, "company", access.package.sponsor_company_id)
    rm_id = UUID(status_map["rm"]["user_id"]) if status_map["rm"].get("user_id") else None
    out["rm"] = await sigs_svc.current(db, "user", rm_id) if rm_id else None
    return out


def _initials_of(name: str) -> str:
    return "".join(w[0] for w in (name or "").replace(".", " ").split() if w[:1].isalpha()).upper()[:4]


def _place_signatures(pdf: bytes, placed: dict[str, Any], now: datetime) -> tuple[bytes, list[dict[str, Any]]]:
    """Stamp QC, sponsor and RM from their stored signatures. Returns (pdf, placement records)."""
    from app.services import pdf_stamping
    from app.services import stored_signatures as sigs_svc

    records: list[dict[str, Any]] = []
    current = pdf
    for party in PLACED_PARTIES:
        sig = placed.get(party)
        if sig is None:
            continue
        png = sigs_svc.signature_png(sig)
        before = hashlib.sha256(current).hexdigest()
        try:
            current, stats = pdf_stamping.stamp_party(
                current, party=party, typed_name=sig.typed_name, signature_png=png, signed_at=now,
                initials=_initials_of(sig.typed_name), scheme=pdf_stamping.STAMP_SCHEME_TEMPLATE,
            )
        except ValueError as exc:
            if party == "rm":
                logger.warning("no RM signature block on the template: %s", exc)
                continue
            raise
        records.append({
            "party": party, "stored_signature_id": str(sig.id), "typed_name": sig.typed_name, "title": sig.title,
            "adopted_at": sig.adopted_at.isoformat() if sig.adopted_at else None, "consent_version": sig.adoption_consent_version,
            "source": sig.source, "signature_sha256": sig.signature_sha256, "placed_at": now.isoformat(),
            "document_sha256_before": before, "document_sha256_after": hashlib.sha256(current).hexdigest(), "stats": stats,
        })
    return current, records


def _agreement_number(package: ProductionPackage, revision_no: int) -> str:
    prefix = "QC-PA" if int(getattr(package, "stage", 1) or 1) == 1 else "QC-AA"
    return f"{prefix}-{str(package.id)[:8].upper()}-R{revision_no}"


async def _stage_two_gates(db: AsyncSession, access: pkgs.PackageAccess, arrangement: dict[str, Any], attestation: dict[str, Any] | None) -> dict[str, Any]:
    """The final may only go out once funding cleared; the sender attests the facts printed on the certificate."""
    parent = access.parent
    if parent is None or parent.status != "executed" or not parent.executed_at:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "stage_one_not_executed", "message": "The production commitment must be executed first."})
    funding = str(arrangement.get("funding_date") or "")[:10]
    docs = str(arrangement.get("funding_docs_executed_date") or "")[:10]
    if docs and docs < parent.executed_at.date().isoformat():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "funding_docs_before_commitment", "message": "The funding documents cannot predate the executed commitment."})
    if STAGE_TWO_SEND_AFTER_FUNDING and (not funding or funding > _now().date().isoformat()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "funding_not_yet_occurred", "message": "The final is sent after actual funding has cleared; the funding date is in the future."})
    if not attestation or not attestation.get("confirm"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": "funding_attestation_required", "message": "Confirm that actual funding cleared before sending the final."})
    a_date = str(attestation.get("actual_funding_date") or "")[:10]
    a_amount = float(attestation.get("amount_funded") or 0)
    a_party = str(attestation.get("funding_party_name") or "").strip()
    mismatches = []
    if a_date != funding:
        mismatches.append("funding date")
    if abs(a_amount - pa._num(arrangement.get("funded_amount"))) > 0.01:
        mismatches.append("amount funded")
    if _normalize_name(a_party) != _normalize_name(str(arrangement.get("funding_party_name") or "")):
        mismatches.append("funding party")
    if mismatches:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "funding_mismatch", "fields": mismatches,
                    "message": "The attested funding must match the certificate: " + ", ".join(mismatches) + ". Edit the final or the term sheet first."},
        )
    return {
        "attested_by_user_id": str(access.user.id), "attested_by_name": access.user.name, "attested_at": _now().isoformat(),
        "actual_funding_date": a_date, "amount_funded": a_amount, "funding_party_name": a_party,
        "funding_reference": attestation.get("funding_reference"), "note": attestation.get("note"),
        "attestation_version": FUNDING_ATTESTATION_VERSION, "text": FUNDING_ATTESTATION_TEXT,
    }


async def send(
    db: AsyncSession, access: pkgs.PackageAccess, *, channel: str, recipient_email: str | None,
    recipient_phone: str | None, request: Request | None, attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.services import production_agreements as agreements
    from app.services import production_fields as fields

    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    stage = int(getattr(package, "stage", 1) or 1)
    if package.status == "out_for_signature":
        return {"already_sent": True, "delivered": False, "emailed": False, "texted": False,
                "detail": "This package is already out for signature."}
    if not access.capabilities().can_send:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This package cannot be sent from this account.")
    arrangement = {**pa.empty_arrangement(), **(package.arrangement or {})}
    computed = pa.compute(arrangement, stage=stage)
    if computed["attention"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "attention", "scope": "stage_two" if stage == 2 else "stage_one", "items": computed["attention"],
                    "message": "Clear the open items before requesting a signature — a blank field is not enforceable."},
        )
    business_name, intake_email, intake_phone = await pkgs.client_contact(db, access)
    email = (recipient_email or intake_email or "").strip() if access.is_operator else (intake_email or "").strip()
    phone = ((recipient_phone or intake_phone or "").strip() if access.is_operator else (intake_phone or "").strip()) or None
    if "@" not in email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The client needs an email address to sign in.")
    await _training_guard(db, access, request, action="production_package.send", recipient=email)
    company, agreement = await pkgs.require_signed_sponsor(db, package)
    sponsor = pkgs.sponsor_snapshot(company, agreement, arrangement)
    funding: dict[str, Any] | None = None
    original: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None
    if stage == 2:
        funding = await _stage_two_gates(db, access, arrangement, attestation)
        source = await db.get(ProductionPackageRevision, package.source_revision_id) if package.source_revision_id else None
        if source is not None:
            original = {"package_id": str(package.parent_package_id), "revision_id": str(source.id), "revision_no": source.revision_no,
                        "content_sha256": source.content_sha256, "executed_pdf_sha256": source.current_pdf_sha256,
                        "executed_pdf_s3_key": source.current_pdf_s3_key,
                        "executed_at": access.parent.executed_at.isoformat() if access.parent and access.parent.executed_at else None,
                        "title": source.document_title}
            comparison = pa.arrangement_diff(
                {"arrangement": (source.snapshot or {}).get("arrangement"), "computed": (source.snapshot or {}).get("computed"), "sponsor": (source.snapshot or {}).get("sponsor")},
                {"arrangement": arrangement, "computed": computed, "sponsor": sponsor},
            )
    placed = await _resolve_placed_signatures(db, access, arrangement)
    parties = {
        "dealer": {"name": arrangement.get("dealer_name"), "signer_name": arrangement.get("dealer_signer_name"),
                   "signer_title": arrangement.get("dealer_signer_title"), "email": email, "phone": phone},
        "qc": {"name": "Qualified Commercial LLC", "signer_name": getattr(placed["qc"], "typed_name", None), "signer_title": getattr(placed["qc"], "title", None)},
        "sponsor": {"name": sponsor.get("name"), "signer_name": getattr(placed["sponsor"], "typed_name", None), "signer_title": getattr(placed["sponsor"], "title", None)},
        "relationship_manager": {"name": arrangement.get("rm_name"), "email": arrangement.get("rm_email"),
                                 "signer_name": getattr(placed.get("rm"), "typed_name", None), "title": getattr(placed.get("rm"), "title", None)},
    }
    revision_no = await _next_revision_no(db, package.id)
    agreement_no = package.agreement_no if getattr(package, "agreement_no", None) else _agreement_number(package, revision_no)
    file_ctx = await pkgs.load_file_context(db, access)
    file_ctx["qc"] = {**(file_ctx.get("qc") or {}), "signer_name": (file_ctx.get("qc") or {}).get("signer_name") or getattr(placed["qc"], "typed_name", None),
                      "signer_title": (file_ctx.get("qc") or {}).get("signer_title") or getattr(placed["qc"], "title", None)}
    now = _now()
    meta = {
        "agreement_no": agreement_no, "revision_no": revision_no, "generated_on": now.date().isoformat(),
        "effective_date": "", "written_approval_date": arrangement.get("written_approval_date") or "",
        "outside_funding_date": arrangement.get("outside_funding_date") or "",
        "commitment_agreement_date": (access.parent.executed_at.date().isoformat() if stage == 2 and access.parent and access.parent.executed_at else ""),
    }
    template_key = "commitment_v1" if stage == 1 else "activation_v1"
    if stage == 1:
        values, checks = fields.commitment_values(arrangement, computed, sponsor, parties, file_ctx, meta)
        title, document_key = pa.STAGE_ONE_TITLE, pa.STAGE_ONE_DOCUMENT_KEY
    else:
        values, checks = fields.activation_values(arrangement, computed, sponsor, parties, file_ctx, meta, original=original, funding=funding)
        title, document_key = pa.STAGE_TWO_TITLE, pa.STAGE_TWO_DOCUMENT_KEY
    _template_html, template_sha = agreements.load_template(template_key)
    html_doc = agreements.fill_template(template_key, values, set(checks), footer=title)
    pdf, _unsigned_sha = _render(html_doc)
    pdf, placements = _place_signatures(pdf, placed, now)
    pdf_sha = hashlib.sha256(pdf).hexdigest()
    key = pres.revision_key(package.profile_id, package.id, revision_no, "countersigned", pdf_sha).replace("stage1-", f"stage{stage}-")
    if not storage.put_bytes(key, pdf, "application/pdf"):
        raise _storage_unavailable()
    snapshot = pa.canonical_snapshot(arrangement, computed, sponsor=sponsor, parties=parties)
    snapshot.update(pa.jsonable({
        "template": {"key": template_key, "sha256": template_sha}, "template_values": values, "checks": sorted(checks),
        "file_context": file_ctx, "comparison": comparison, "original": original,
        "stamping": {"scheme": "template_v1"}, "placements": placements, "agreement_no": agreement_no,
    }))
    content_sha = pa.snapshot_hash(arrangement, extra={
        "sponsor": sponsor, "parties": parties, "template_sha256": template_sha, "values": values, "checks": sorted(checks),
        "comparison": comparison, "original": original, "placements": [p_["stored_signature_id"] for p_ in placements],
    })
    revision = ProductionPackageRevision(
        package_id=package.id, revision_no=revision_no, stage=stage, status="out_for_signature",
        document_key=document_key, document_title=title, document_version=pa.DOCUMENT_VERSION,
        snapshot=snapshot, content_sha256=content_sha, rendered_text=_agreement_text(pdf, html_doc),
        rendered_pdf_s3_key=key, rendered_pdf_sha256=pdf_sha, current_pdf_s3_key=key, current_pdf_sha256=pdf_sha,
        funding=funding, created_by_user_id=access.user.id, sent_at=now,
    )
    db.add(revision)
    await db.flush()
    db.add(ProductionPackageSignature(
        package_id=package.id, revision_id=revision.id, stage=stage, party="dealer", method="electronic",
        status="pending", expected_signer_name=arrangement.get("dealer_signer_name"), sent_at=now,
    ))
    for rec in placements:
        db.add(ProductionPackageSignature(
            package_id=package.id, revision_id=revision.id, stage=stage, party=rec["party"], method="stored", status="signed",
            signer_name=rec["typed_name"], signer_title=rec.get("title"), signed_at=now, signed_on=now.date(),
            stored_signature_id=UUID(rec["stored_signature_id"]), placed_at=now, placed_by_user_id=access.user.id,
            initials=_initials_of(rec["typed_name"]), document_sha256=rec["document_sha256_before"], signed_pdf_sha256=rec["document_sha256_after"],
        ))
    package.frozen_revision_id = revision.id
    package.status = "out_for_signature"
    package.sent_at = now
    package.sent_by_user_id = access.user.id
    package.sent_via = "operator" if access.is_operator else ("partner" if access.mode == "partner" else "share_link")
    package.sent_share_link_id = access.link.id if access.link else None
    package.computed_cache = pa.jsonable(computed)
    package.attention = []
    package.execution_pending = False
    package.version = (package.version or 1) + 1
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.sent_for_signature",
        f"{title} sent for signature to {email}",
        target_type="production_package", target_id=package.id,
        metadata={"revision_no": revision_no, "stage": stage, "document_key": document_key, "content_sha256": content_sha[:16],
                  "pdf_sha256": pdf_sha[:16], "recipient_email": email, "recipient_phone": phone, "channel": channel,
                  "sponsor": sponsor.get("name"), "sponsor_agreement": (sponsor.get("agreement") or {}).get("contract_number"),
                  "via": package.sent_via, "share_link_id": str(access.link.id) if access.link else None, "role": str(access.user.role),
                  "placed": [p_["party"] for p_ in placements], "ip": client_ip(request),
                  "user_agent": (request.headers.get("user-agent", "")[:200] if request else None)},
    )
    delivery = await _deliver(db, access, channel=channel, email=email, phone=phone, business_name=business_name, action="production_package_sign")
    return {"already_sent": False, **delivery}


async def remind(db: AsyncSession, access: pkgs.PackageAccess, *, channel: str, request: Request | None) -> dict[str, Any]:
    package = access.package
    if not access.capabilities().can_remind:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a package that is out for signature can be reminded, by the desk or the person who sent it")
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
        target_type="production_package", target_id=package.id, metadata={"channel": channel, "via": access.via},
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
    package.execution_pending = False
    package.version = (package.version or 1) + 1
    package.updated_by_user_id = access.user.id
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.reopened", f"Production package reopened: {reason}",
        target_type="production_package", target_id=package.id, metadata={"reason": reason, "stage": package.stage},
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
    revoked = await pkgs.revoke_all_share_links(db, package, access.user) if int(getattr(package, "stage", 1) or 1) == 1 else 0
    now = _now()
    package.status = "void"
    package.voided_at = now
    package.voided_by_user_id = access.user.id
    package.void_reason = reason
    package.execution_pending = False
    package.version = (package.version or 1) + 1
    if int(getattr(package, "stage", 1) or 1) == 2 and package.term_sheet_id:
        from app.models.production_package import ProductionTermSheet

        sheet = await db.get(ProductionTermSheet, package.term_sheet_id)
        if sheet is not None and sheet.consumed_by_package_id == package.id:
            sheet.consumed_by_package_id = None
            sheet.consumed_at = None
    await db.flush()
    await profiles.log_profile_action(
        db, access.profile, access.user, "production_package.voided", f"Production package voided: {reason}",
        target_type="production_package", target_id=package.id, metadata={"reason": reason, "share_links_revoked": revoked, "stage": package.stage},
    )
    return package


# ---------------------------------------------------------------------------
# client gate + signature
# ---------------------------------------------------------------------------

async def pending_client_signature(
    db: AsyncSession, intake_id: UUID | None
) -> tuple[ProductionPackage, ProductionPackageRevision, ProductionPackageSignature] | None:
    """Derived, never stored: the dealer still owes a signature on the frozen revision.
    At most one package per profile is ever out for signature (partial unique index)."""
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
    stage = int(getattr(revision, "stage", None) or getattr(package, "stage", 1) or 1)
    original = snap.get("original") or None
    if original:
        # The dealer may review the executed commitment beside the final (Commitment §4.7).
        original = {k: v for k, v in original.items() if k != "executed_pdf_s3_key"}
        original["pdf_url"] = presign_private_s3_object(
            (snap.get("original") or {}).get("executed_pdf_s3_key"), ttl_seconds=900,
            download_filename=f"{original.get('title') or 'Production Commitment'}.pdf",
        )
    changes = [r for r in ((snap.get("comparison") or {}).get("rows") or []) if r.get("dealer_visible") and r.get("changed")]
    return ProductionSigningGateRead(
        package_id=package.id, revision_id=revision.id, revision_no=revision.revision_no, stage=stage, document_key=revision.document_key,
        title=revision.document_title, document_version=revision.document_version,
        content_sha256=revision.content_sha256, pdf_sha256=revision.current_pdf_sha256,
        pdf_url=presign_private_s3_object(revision.current_pdf_s3_key, ttl_seconds=900, download_filename=f"{revision.document_title}.pdf"),
        signer_name=sig.expected_signer_name or dealer.get("signer_name") or "",
        signer_title=dealer.get("signer_title"), business_name=business_name,
        sent_at=sig.sent_at, esign_consent_text=ESIGN_CONSENT_TEXT_STAGE_TWO if stage == 2 else ESIGN_CONSENT_TEXT,
        esign_consent_version=ESIGN_CONSENT_VERSION, already_signed=sig.status == "signed",
        initials_expected=True, original=original, changes=changes,
        review_clause=REVIEW_CLAUSE if stage == 2 else None, acknowledgement_text=ACKNOWLEDGEMENT_TEXT[stage],
    )


def _normalize_name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _stamp_party(
    pdf_bytes: bytes, *, anchor: str, placeholder: str, date_placeholder: str,
    typed_name: str, signature_png: bytes | None, signed_at: datetime | date,
) -> bytes:
    """Legacy heading-based stamping for revisions rendered before the real templates."""
    from app.services import pdf_stamping

    out, _stats = pdf_stamping.stamp_party(
        pdf_bytes, party="legacy", typed_name=typed_name, signature_png=signature_png, signed_at=signed_at,
        scheme=pdf_stamping.STAMP_SCHEME_LEGACY,
        legacy={"anchor": anchor, "placeholder": placeholder, "date_placeholder": date_placeholder},
    )
    return out


def _stamp_for(revision: ProductionPackageRevision, pdf: bytes, *, party: str, typed_name: str, signature_png: bytes | None,
               signed_at: datetime | date, initials: str | None) -> tuple[bytes, dict[str, Any]]:
    """Stamp every block of a party using the revision's scheme (template anchors, or the legacy headings)."""
    from app.services import pdf_stamping

    scheme = ((revision.snapshot or {}).get("stamping") or {}).get("scheme")
    if scheme == pdf_stamping.STAMP_SCHEME_TEMPLATE or pdf_stamping.has_template_anchors(pdf):
        return pdf_stamping.stamp_party(pdf, party=party, typed_name=typed_name, signature_png=signature_png,
                                        signed_at=signed_at, initials=initials, scheme=pdf_stamping.STAMP_SCHEME_TEMPLATE)
    anchors = {"dealer": (pres.DEALER_ANCHOR, pres.ELECTRONIC_PLACEHOLDER, pres.ELECTRONIC_DATE_PLACEHOLDER),
               "sponsor": (pres.SPONSOR_ANCHOR, pres.RECORDED_PLACEHOLDER, pres.RECORDED_DATE_PLACEHOLDER),
               "qc": (pres.QC_ANCHOR, pres.RECORDED_PLACEHOLDER, pres.RECORDED_DATE_PLACEHOLDER)}
    if party not in anchors:
        raise ValueError(f"No legacy signature block for {party}")
    anchor, placeholder, date_placeholder = anchors[party]
    return _stamp_party(pdf, anchor=anchor, placeholder=placeholder, date_placeholder=date_placeholder,
                        typed_name=typed_name, signature_png=signature_png, signed_at=signed_at), {"blocks": 1}


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


def _initials_match(initials: str, signer_name: str) -> bool:
    given = "".join(ch for ch in (initials or "") if ch.isalpha()).upper()
    if not (2 <= len(given) <= 4):
        return False
    expected = _initials_of(signer_name)
    # Middle names are optional: the first and last letters must match and the given letters must be a subsequence.
    if not expected:
        return True
    if given[0] != expected[0] or given[-1] != expected[-1]:
        return False
    it = iter(expected)
    return all(ch in it for ch in given)


async def sign_dealer(
    db: AsyncSession, intake: PublicUnderwritingIntake, *, revision_id: UUID, typed_name: str, initials: str, esign_consent: bool,
    acknowledged: bool, signature_data_url: str, document_sha256: str, request: Request | None,
) -> dict[str, Any]:
    pending = await pending_client_signature(db, intake.id)
    if pending is None:
        package = (
            await db.execute(
                select(ProductionPackage).where(ProductionPackage.intake_id == intake.id, ProductionPackage.frozen_revision_id == revision_id)
            )
        ).scalar_one_or_none()
        if package is not None:
            done = (
                await db.execute(
                    select(ProductionPackageSignature).where(
                        ProductionPackageSignature.revision_id == revision_id,
                        ProductionPackageSignature.party == "dealer",
                        ProductionPackageSignature.status == "signed",
                    )
                )
            ).scalar_one_or_none()
            revision = await db.get(ProductionPackageRevision, revision_id)
            if done is not None:
                key = package.executed_pdf_s3_key if package.status == "executed" else done.signed_pdf_s3_key
                return {"signed": True, "signed_at": done.signed_at, "pdf_sha256": package.executed_pdf_sha256 if package.status == "executed" else done.signed_pdf_sha256,
                        "download_url": presign_private_s3_object(key, ttl_seconds=3600),
                        "execution_status": "executed" if package.status == "executed" else "signed",
                        "title": revision.document_title if revision else None}
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
    if not _initials_match(initials, expected):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "initials_mismatch", "expected": _initials_of(expected),
                    "message": f"Type your initials as they appear in your name ({_initials_of(expected)})."},
        )
    initials = "".join(ch for ch in initials if ch.isalpha()).upper()
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
    stage = int(getattr(revision, "stage", None) or getattr(package, "stage", 1) or 1)
    try:
        stamped, stats = _stamp_for(revision, raw, party="dealer", typed_name=typed_name, signature_png=png, signed_at=now, initials=initials)
    except ImportError as exc:
        raise _pdf_unavailable() from exc
    signed_body_sha = hashlib.sha256(stamped).hexdigest()
    snap = revision.snapshot or {}
    arr = snap.get("arrangement") or {}
    rows = [
        ("Agreement", revision.document_title),
        ("Agreement stage", "Stage two — Program Activation" if stage == 2 else "Stage one — Production Commitment"),
        ("Revision", f"{revision.revision_no} · version {revision.document_version}"),
        ("Template SHA-256", str((snap.get("template") or {}).get("sha256") or "")),
        ("Dealer", str(arr.get("dealer_name") or "")),
        ("Signer", typed_name),
        ("Signer title", str(arr.get("dealer_signer_title") or "")),
        ("Initials adopted", initials),
        ("Blocks stamped", f"{stats.get('blocks', 1)} signature, {stats.get('dates', 0)} date, {stats.get('initials', 0)} initials"),
        ("Signature method", "Drawn on device"),
        ("Signature SHA-256", png_sha or ""),
        ("Content SHA-256 (frozen schedules)", revision.content_sha256),
        ("Document SHA-256 (pre-signing)", revision.current_pdf_sha256 or revision.rendered_pdf_sha256 or ""),
        ("Signed agreement SHA-256 (before certificate)", signed_body_sha),
        ("E-SIGN consent", f"version {ESIGN_CONSENT_VERSION} · {now.isoformat()}"),
        ("Consent text", ESIGN_CONSENT_TEXT_STAGE_TWO if stage == 2 else ESIGN_CONSENT_TEXT),
        ("Signed at", now.isoformat()),
        ("IP address", ip or ""),
        ("Device", ua[:220]),
    ]
    for rec in snap.get("placements") or []:
        rows.append((f"Placed signature — {rec.get('party')}", f"{rec.get('typed_name')} · stored signature {str(rec.get('stored_signature_id'))[:8]} · adopted {rec.get('adopted_at')} · placed {rec.get('placed_at')}"))
    if stage == 2 and snap.get("original"):
        o = snap["original"]
        rows.append(("Supersedes", f"{o.get('title')} R{o.get('revision_no')} · executed {o.get('executed_at')} · content SHA-256 {o.get('content_sha256')}"))
        comp = snap.get("comparison") or {}
        rows.append(("Changes reviewed", f"{comp.get('changed_count', 0)} rows · SHA-256 {hashlib.sha256(pa.canonical_json(comp).encode()).hexdigest()}"))
    if revision.funding:
        f = revision.funding
        rows.append(("Funding attestation", f"{f.get('amount_funded')} cleared {f.get('actual_funding_date')} from {f.get('funding_party_name')} · attested by {f.get('attested_by_name')} {f.get('attested_at')}"))
    try:
        cert = _certificate(rows, revision.document_title)
    except PDFUnavailableError as exc:
        raise _pdf_unavailable() from exc
    final = _append_pages(stamped, cert)
    final_sha = hashlib.sha256(final).hexdigest()
    key = pres.revision_key(package.profile_id, package.id, revision.revision_no, "dealer-signed", final_sha).replace("stage1-", f"stage{stage}-")
    if not storage.put_bytes(key, final, "application/pdf"):
        raise _storage_unavailable()
    png_key = pres.revision_key(package.profile_id, package.id, revision.revision_no, "dealer-signature", png_sha or final_sha, ext="png").replace("stage1-", f"stage{stage}-")
    storage.put_bytes(png_key, png, "image/png")
    sig.status = "signed"
    sig.typed_name = typed_name
    sig.initials = initials
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
        f"{typed_name} signed the {revision.document_title} electronically",
        target_type="production_package", target_id=package.id,
        metadata={"revision_no": revision.revision_no, "stage": stage, "signed_pdf_sha256": final_sha[:16], "ip": ip, "initials": initials},
    )
    # The dealer's signature completes the set: execute in the same transaction.
    execution_status = "signed"
    out_pdf, out_sha, out_key = final, final_sha, key
    try:
        bundle, bundle_sha = await _assemble_execution(db, package, revision, profile, actor=None, request=request)
        execution_status = "executed"
        out_pdf, out_sha, out_key = bundle, bundle_sha, package.executed_pdf_s3_key
    except HTTPException as exc:
        logger.warning("execution assembly deferred for package %s: %s", package.id, exc.detail)
        package.execution_pending = True
        await db.flush()
        await profiles.log_profile_action(
            db, profile, None, "production_package.execution_pending", "Signed, but the executed bundle could not be assembled; the desk can retry",
            target_type="production_package", target_id=package.id, metadata={"reason": str(exc.detail)[:300]},
        )
    email = (snap.get("parties") or {}).get("dealer", {}).get("email")
    return {"signed": True, "signed_at": now, "pdf_sha256": out_sha,
            "download_url": presign_private_s3_object(out_key, ttl_seconds=3600, download_filename=f"{revision.document_title}.pdf"),
            "execution_status": execution_status, "title": revision.document_title, "pdf": out_pdf, "email": email,
            "package_id": str(package.id)}


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


async def notify_executed(db: AsyncSession, access: pkgs.PackageAccess, title: str, pdf: bytes) -> None:
    """The executed bundle goes to the dealer, the sponsor's notice email and the relationship manager."""
    _business, email, _phone = await pkgs.client_contact(db, access)
    email_signed_copy(email, title, pdf, final=True)
    arrangement = access.package.arrangement or {}
    for to in (arrangement.get("sponsor_email"), arrangement.get("rm_email")):
        if to and to != email:
            email_signed_copy(str(to), title, pdf, final=True)


async def notify_executed_by_id(db: AsyncSession, package_id: str, title: str, pdf: bytes) -> None:
    package = await db.get(ProductionPackage, UUID(str(package_id)))
    if package is None:
        return
    arrangement = package.arrangement or {}
    for to in (arrangement.get("sponsor_email"), arrangement.get("rm_email")):
        if to:
            email_signed_copy(str(to), title, pdf, final=True)


# ---------------------------------------------------------------------------
# fallback manual records, execution
# ---------------------------------------------------------------------------

async def _frozen_revision(db: AsyncSession, package: ProductionPackage) -> ProductionPackageRevision:
    revision = await db.get(ProductionPackageRevision, package.frozen_revision_id) if package.frozen_revision_id else None
    if revision is None or package.status != "out_for_signature":
        raise HTTPException(status.HTTP_409_CONFLICT, "This package is not out for signature.")
    return revision


async def record_manual_signature(
    db: AsyncSession, access: pkgs.PackageAccess, *, party: str, signer_name: str, signer_title: str,
    signed_on: date, attestation: bool, note: str | None, override_reason: str | None,
    scan_file_name: str | None, scan_content_type: str | None, request: Request | None, initials: str | None = None,
) -> tuple[ProductionPackageSignature, dict[str, Any] | None]:
    """Fallback for a party with no signature on file: a super admin records a signature captured outside the system."""
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    if not access.capabilities().can_record:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required to record a signature")
    if party not in ("qc", "sponsor", "rm"):
        raise HTTPException(status.HTTP_409_CONFLICT, "The dealer signs electronically; only QC, sponsor and relationship-manager signatures are recorded.")
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
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {party} signature is already on this revision.")
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
    ini = "".join(ch for ch in (initials or _initials_of(signer_name)) if ch.isalpha()).upper()[:4]
    try:
        stamped, _stats = _stamp_for(revision, raw, party=party, typed_name=f"{signer_name}, {signer_title}", signature_png=None, signed_at=signed_on, initials=ini)
    except ImportError as exc:
        raise _pdf_unavailable() from exc
    sha = hashlib.sha256(stamped).hexdigest()
    stage = int(getattr(revision, "stage", None) or getattr(package, "stage", 1) or 1)
    key = pres.revision_key(package.profile_id, package.id, revision.revision_no, f"{party}-recorded", sha).replace("stage1-", f"stage{stage}-")
    if not storage.put_bytes(key, stamped, "application/pdf"):
        raise _storage_unavailable()
    now = _now()
    sig = ProductionPackageSignature(
        package_id=package.id, revision_id=revision.id, stage=stage, party=party, method="manual", status="signed",
        signer_name=signer_name.strip(), signer_title=signer_title.strip(), signed_on=signed_on, initials=ini,
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
        f"{party.upper() if party == 'qc' else party.capitalize()} signature recorded: {signer_name}",
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


async def _assemble_execution(
    db: AsyncSession, package: ProductionPackage, revision: ProductionPackageRevision, profile: ApplicationProfile, *,
    actor: User | None, request: Request | None,
) -> tuple[bytes, str]:
    """All parties signed the same revision: append the execution record, store the bundle, flip the status."""
    from app.services import pdf_stamping

    sigs = (
        await db.execute(
            select(ProductionPackageSignature).where(
                ProductionPackageSignature.revision_id == revision.id, ProductionPackageSignature.status == "signed",
            )
        )
    ).scalars().all()
    by_party = {s.party: s for s in sigs}
    missing = [p for p in ("dealer", "qc", "sponsor") if p not in by_party]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "signatures_missing", "missing": missing, "message": "Every party must sign the same revision before execution."},
        )
    raw = await _current_pdf(revision)
    now = _now()
    stage = int(getattr(revision, "stage", None) or getattr(package, "stage", 1) or 1)
    dealer_sig = by_party["dealer"]
    rows: list[tuple[str, str]] = [
        ("Agreement", revision.document_title),
        ("Agreement stage", "Stage two — Program Activation" if stage == 2 else "Stage one — Production Commitment"),
        ("Revision", f"{revision.revision_no} · version {revision.document_version}"),
        ("Content SHA-256 (frozen schedules)", revision.content_sha256),
        ("Countersigned document SHA-256", revision.rendered_pdf_sha256 or ""),
        ("Dealer signature", f"{dealer_sig.typed_name} · electronic · initials {dealer_sig.initials or ''} · {dealer_sig.signed_at.isoformat() if dealer_sig.signed_at else ''}"),
        ("Dealer-signed document SHA-256", dealer_sig.signed_pdf_sha256 or ""),
    ]
    for party, label in (("qc", "Qualified Commercial signature"), ("sponsor", "Sponsor signature"), ("rm", "Relationship manager signature")):
        s = by_party.get(party)
        if s is None:
            continue
        if s.method == "stored":
            rows.append((label, f"{s.signer_name}{', ' + s.signer_title if s.signer_title else ''} · placed from file {str(s.stored_signature_id)[:8]} at {s.placed_at.isoformat() if s.placed_at else ''}"))
        else:
            rows.append((label, f"{s.signer_name}, {s.signer_title} · signed {s.signed_on.isoformat() if s.signed_on else ''} · recorded {s.recorded_at.isoformat() if s.recorded_at else ''} · "
                                f"scan {s.scan_sha256[:16] if s.scan_sha256 else 'no scan on file'} · attestation {s.attestation_version}"))
    if stage == 2 and (revision.snapshot or {}).get("original"):
        o = revision.snapshot["original"]
        rows.append(("Supersedes", f"{o.get('title')} R{o.get('revision_no')} · executed {o.get('executed_at')} · content SHA-256 {o.get('content_sha256')}"))
    if revision.funding:
        f = revision.funding
        rows.append(("Funding attestation", f"{f.get('amount_funded')} cleared {f.get('actual_funding_date')} from {f.get('funding_party_name')} · attested by {f.get('attested_by_name')} {f.get('attested_at')} · v{f.get('attestation_version')}"))
        rows.append(("Attestation text", str(f.get("text") or FUNDING_ATTESTATION_TEXT)))
    rows.append(("Current document SHA-256 (before execution record)", revision.current_pdf_sha256 or ""))
    rows.append(("Executed", f"{(actor.name if actor else 'automatically on the dealer signature')} · {now.isoformat()} · {client_ip(request) or ''}"))
    try:
        record = _certificate(rows, f"Execution Record — {revision.document_title}")
    except PDFUnavailableError as exc:
        raise _pdf_unavailable() from exc
    clean = pdf_stamping.redact_remaining_anchors(raw)
    final = _append_pages(clean, record)
    final_sha = hashlib.sha256(final).hexdigest()
    key = pres.revision_key(package.profile_id, package.id, revision.revision_no, "executed", final_sha).replace("stage1-", f"stage{stage}-")
    if not storage.put_bytes(key, final, "application/pdf"):
        raise _storage_unavailable()
    package.status = "executed"
    package.executed_at = now
    package.executed_by_user_id = actor.id if actor else None
    package.executed_pdf_s3_key = key
    package.executed_pdf_sha256 = final_sha
    package.execution_pending = False
    package.version = (package.version or 1) + 1
    revision.status = "executed"
    revision.completed_at = now
    revision.current_pdf_s3_key = key
    revision.current_pdf_sha256 = final_sha
    if stage == 1:
        await pkgs.revoke_all_share_links(db, package, actor) if actor else None
    await db.flush()
    await profiles.log_profile_action(
        db, profile, actor, "production_package.executed" if stage == 1 else "production_package.final_executed",
        f"{revision.document_title} fully executed",
        target_type="production_package", target_id=package.id,
        metadata={"revision_no": revision.revision_no, "stage": stage, "executed_sha256": final_sha[:16], "automatic": actor is None},
    )
    if stage == 2:
        try:
            from app.routers.application_profiles import apply_underwriting_changes

            if profile.underwriting_status not in {"closed_won", "closed_lost", "denied"}:
                await apply_underwriting_changes(db, profile, actor or SimpleNamespace(id=package.sent_by_user_id), {"underwriting_status": "closed_won"})
        except Exception:  # noqa: BLE001 - the execution stands; the status write-through is a convenience
            logger.exception("closed_won write-through failed for package %s", package.id)
    return final, final_sha


async def execute(db: AsyncSession, access: pkgs.PackageAccess, *, request: Request | None) -> tuple[ProductionPackage, bytes | None, str]:
    """Retry the execution bundle after the dealer signed but the assembly failed (or finish a manually recorded set)."""
    package = await db.get(ProductionPackage, access.package.id, with_for_update=True)
    access.package = package
    revision = await db.get(ProductionPackageRevision, package.frozen_revision_id) if package.frozen_revision_id else None
    title = revision.document_title if revision else pa.STAGE_ONE_TITLE
    if package.status == "executed":
        return package, None, title
    caps = access.capabilities()
    if not (caps.can_execute or caps.can_record):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin or underwriter role required to execute")
    if revision is None or package.status != "out_for_signature":
        raise HTTPException(status.HTTP_409_CONFLICT, "This package is not out for signature.")
    final, _sha = await _assemble_execution(db, package, revision, access.profile, actor=access.user, request=request)
    return package, final, title


async def delete_guard(db: AsyncSession, profile_id: UUID) -> None:
    """Two-step lead deletion guard: refuse while any package is out for signature or executed."""
    packages = (
        await db.execute(select(ProductionPackage).where(ProductionPackage.profile_id == profile_id))
    ).scalars().all()
    for package in packages:
        if package.status == "out_for_signature":
            raise HTTPException(status.HTTP_409_CONFLICT, "Void the outstanding Production Package before deleting this file.")
        if package.status == "executed":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This file holds an executed Production Package, which is a retained record. Archive it instead of deleting it.",
            )
