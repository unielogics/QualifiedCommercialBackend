"""Signing a frozen client form (the real-estate loan application first).

The way the room signs documents, aimed at a form: the signer reads the exact
consents the requirement configured (each versioned and hashed), types their
legal name and draws a signature; the signature is stamped onto the frozen
application PDF itself; when every required signer has signed, a certificate
page per signer is appended and the executed copy is filed on the
requirement's checklist row as a generated, signed document (verified by
construction, so no person has to tick it).

One signature can also count as the credit authorization (Franco,
2026-11-16): the room's credit-authorization row gets a cross-referenced
record and certificate, and the soft pull runs from it under the reuse window
(a pull younger than the window is reused, never repeated).

Signing refuses while the file is on a personal-use hold, and in production
until the consent wording is approved (Settings → signed forms).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from functools import partial
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import BucketFile, BucketFileAnalysis, BucketRequestedDocument
from app.models.client_form import ClientForm, ClientFormVersion
from app.models.form_signing import FormSigningSession
from app.services import client_forms, form_kinds
from app.services import re_application_schema as schema

log = logging.getLogger(__name__)

KIND = schema.KIND
ACTIVE = "in_progress"
EXECUTED = "executed"
VOID = "void"
SETTINGS_KEY = "signed_forms"
CREDIT_SATISFIER = "signed_credit_authorization"
COBORROWER_LABEL = client_forms.COBORROWER_LABEL


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- approval of the wording ---------------------------------------------------------


async def _settings_row(db: AsyncSession):
    from app.models.app_settings import AppSettings

    return (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()


async def approval(db: AsyncSession) -> dict[str, Any]:
    row = await _settings_row(db)
    section = ((row.data or {}).get(SETTINGS_KEY) if row is not None and isinstance(row.data, dict) else None) or {}
    return {
        "legal_approved": section.get("legal_approved") is True,
        "approved_by": section.get("approved_by"),
        "approved_at": section.get("approved_at"),
        "note": section.get("note"),
    }


async def set_approval(db: AsyncSession, *, approved: bool, actor: Any, note: str | None = None) -> dict[str, Any]:
    from app.models.app_settings import AppSettings
    from app.services.app_settings_lock import lock_app_settings

    await lock_app_settings(db)
    row = (await db.execute(select(AppSettings).limit(1).with_for_update())).scalar_one_or_none()
    if row is None:
        row = AppSettings(singleton=True, data={})
        db.add(row)
        await db.flush()
    data = dict(row.data or {})
    data[SETTINGS_KEY] = {
        "legal_approved": bool(approved),
        "approved_by": getattr(actor, "name", None) or getattr(actor, "email", None),
        "approved_at": _now().isoformat(),
        "note": (note or "").strip()[:500] or None,
    }
    row.data = data
    await db.flush()
    return await approval(db)


async def signing_open(db: AsyncSession) -> bool:
    from app.config import get_settings

    if str(getattr(get_settings(), "app_env", "") or "").lower() != "production":
        return True
    return (await approval(db))["legal_approved"]


# --- the session ---------------------------------------------------------------------


def _consent_snapshot(signature: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key in signature.get("consents") or []:
        consent = form_kinds.consent(key)
        if consent is not None:
            out.append({"key": consent.key, "label": consent.label, "version": consent.version, "sha256": consent.sha256})
    return out


async def _signature_for(db: AsyncSession, profile: Any) -> dict[str, Any]:
    configured = await client_forms.application_programs_signature(db, profile)
    if configured:
        return configured
    kind = form_kinds.get(KIND)
    return form_kinds.normalize_collection_config({"form_kind": KIND, "signature": dict(kind.default_signature or {})}, strict=False).get("signature") or {
        "required": True, "signers": ["borrower"], "consents": ["esign"], "satisfies": [],
    }


async def ensure_slot(db: AsyncSession, profile: Any, signature: dict[str, Any]) -> BucketRequestedDocument:
    """The checklist row the executed application is filed on (one is made when no requirement made it)."""
    rows = (
        await db.execute(
            select(BucketRequestedDocument).where(
                BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
                BucketRequestedDocument.requirement_key == schema.REQUIREMENT_KEY,
                BucketRequestedDocument.status.notin_(["closed", "superseded"]),
            )
        )
    ).scalars().all()
    if rows:
        return rows[0]
    slot = BucketRequestedDocument(
        bucket_id=profile.primary_bucket_id,
        name="Real estate loan application",
        category="Application",
        description="Complete and sign your real estate loan application.",
        required=True,
        allow_multiple_files=False,
        status="requested",
        is_custom=False,
        requires_signature=True,
        signature_kind="signed_form",
        requirement_key=schema.REQUIREMENT_KEY,
        requirement_source={"kind": "staff_request", "form_kind": KIND, "signature": signature},
    )
    db.add(slot)
    await db.flush()
    return slot


async def active_session(db: AsyncSession, version: ClientFormVersion | None) -> FormSigningSession | None:
    if version is None:
        return None
    return (
        await db.execute(
            select(FormSigningSession).where(FormSigningSession.version_id == version.id, FormSigningSession.status.in_([ACTIVE, EXECUTED]))
        )
    ).scalars().first()


def required_roles(body: dict[str, Any], signature: dict[str, Any]) -> list[str]:
    roles = ["borrower"]
    if body.get("has_coborrower"):
        roles.append("coborrower")
    return roles


async def _start(db: AsyncSession, profile: Any, form: ClientForm, version: ClientFormVersion) -> FormSigningSession:
    signature = await _signature_for(db, profile)
    slot = await ensure_slot(db, profile, signature)
    session = FormSigningSession(
        profile_id=profile.id,
        form_id=form.id,
        version_id=version.id,
        requested_document_id=slot.id,
        kind=KIND,
        status=ACTIVE,
        signature_config={**signature, "consent_snapshot": _consent_snapshot(signature)},
        required_roles=required_roles(version.body, signature),
        signers={},
        unsigned_sha256=version.pdf_sha256 or version.body_sha256,
        current_pdf_key=version.pdf_s3_key,
        current_sha256=version.pdf_sha256,
        chain=[],
    )
    db.add(session)
    await db.flush()
    return session


def role_for_link(link: Any) -> str:
    return "coborrower" if str(getattr(link, "label", "") or "").startswith(COBORROWER_LABEL) else "borrower"


async def view(db: AsyncSession, profile: Any, *, role: str | None = None, link: Any = None) -> dict[str, Any]:
    """What a signer (or the desk) needs to see before signing."""
    form = await client_forms.get_form(db, profile.id)
    version = await client_forms.current_version(db, form)
    session = await active_session(db, version)
    if link is not None and role == "coborrower":
        if session is None or form is None or form.status != "submitted":
            raise HTTPException(status.HTTP_409_CONFLICT, {"code": "link_outdated", "message": "This application is being changed. We will email you a new link when it is ready to sign."})
        require_current_link(link, session, role)
    signature = (session.signature_config if session else None) or await _signature_for(db, profile)
    snapshot = (session.signature_config.get("consent_snapshot") if session else None) or _consent_snapshot(signature)
    consents = []
    for item in snapshot:
        consent = form_kinds.consent(item["key"])
        consents.append({**item, "text": consent.text if consent else "", "draft": bool(consent and consent.draft)})
    roles = list(session.required_roles) if session else (required_roles(version.body, signature) if version else ["borrower"])
    signed = {key: {"name": value.get("name"), "signed_at": value.get("signed_at")} for key, value in ((session.signers if session else {}) or {}).items()}
    intake = await client_forms.intake_for(db, profile)
    from app.services import business_purpose

    person = (schema.get_at(version.body, f"borrowers.{role}") or {}) if version and role else {}
    return {
        "status": session.status if session else ("ready" if form is not None and form.status == "submitted" and version is not None else "not_submitted"),
        "version": {"id": str(version.id), "number": version.number, "body_sha256": version.body_sha256} if version and form and form.status == "submitted" else None,
        "consents": consents,
        "required_roles": roles,
        "signed": signed,
        "role": role,
        "signer_name": " ".join(part for part in (person.get("first_name"), person.get("last_name")) if part) or None,
        "can_sign": bool(
            role and version and form and form.status == "submitted" and role in roles and role not in signed
            and (session is None or session.status == ACTIVE)
        ),
        "held": bool(intake is not None and business_purpose.is_open(intake)),
        "signing_open": await signing_open(db),
        "executed_file_id": str(session.executed_file_id) if session and session.executed_file_id else None,
    }


# --- signing ---------------------------------------------------------------------------


async def sign(
    db: AsyncSession,
    profile: Any,
    *,
    role: str,
    link: Any,
    typed_name: str,
    signature_data_url: str,
    consents: list[dict[str, Any]],
    esign_consent: bool,
    version_id: Any,
    body_sha256: str,
    request: Any,
) -> dict[str, Any]:
    from app.routers.dealer_ai_intake import _bucket_storage_config
    from app.services import business_purpose, document_signature, pdf_stamping
    from app.services.payment_authorization import client_ip, decode_signature_data_url

    await client_forms._lock(db, profile)
    form = await client_forms.get_form(db, profile.id, lock=True)
    version = await client_forms.current_version(db, form)
    if form is None or version is None or form.status != "submitted":
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "not_submitted", "message": "Submit the application before signing it."})
    if str(version.id) != str(version_id) or version.body_sha256 != body_sha256:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "form_changed", "message": "The application changed since you reviewed it. Review the current version and sign again."})
    intake = await client_forms.intake_for(db, profile)
    if intake is not None and business_purpose.is_open(intake):
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "personal_use_review", "message": "Your advisor needs to confirm a few details with you before you sign. They will be in touch."})
    if not await signing_open(db):
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "signing_not_open", "message": "Signing opens once our documents are finalized. Your advisor will let you know."})
    session = await active_session(db, version) or await _start(db, profile, form, version)
    if session.status != ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "already_executed", "message": "This application is already signed."})
    if role not in session.required_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link cannot sign this application")
    require_current_link(link, session, role)
    if role in (session.signers or {}):
        # A second click, or a retry after a dropped reply: the signature is already recorded once.
        # If every signer is on record but the signed copy was never filed, file it now.
        if all(item in session.signers for item in session.required_roles):
            await _execute(db, profile, session, version, document_signature=document_signature)
        return await view(db, profile, role=role)
    if not esign_consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Agree to sign electronically to continue.")
    expected = {item["key"]: item["sha256"] for item in session.signature_config.get("consent_snapshot") or []}
    given = {str(item.get("key")): str(item.get("sha256")) for item in consents or [] if isinstance(item, dict)}
    if given != expected:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "consent_changed", "message": "The terms you agreed to changed. Reload the page, read them again and sign."})
    person = schema.get_at(version.body, f"borrowers.{role}") or {}
    last_name = str(person.get("last_name") or "").strip().casefold()
    typed = " ".join(str(typed_name or "").split())
    if not typed or (last_name and last_name not in typed.casefold()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Type your full legal name as it appears on the application.")
    png, png_hash, content_type = decode_signature_data_url(signature_data_url)
    if not png:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Draw your signature to sign.")

    from app.models.bucket import BucketDocumentSignature

    now = _now()
    identity = schema.signer_identity(version.body, role)
    signature_row = BucketDocumentSignature(
        requested_document_id=session.requested_document_id,
        document_version=f"{schema.SCHEMA_VERSION}:v{version.number}:{role}"[:32],
        document_hash=session.unsigned_sha256,
        typed_name=typed[:160],
        esign_consent=True,
        applicant_data={**identity, "role": role, "form_version_id": str(version.id), "consents": session.signature_config.get("consent_snapshot")},
        ip_address=client_ip(request),
        user_agent=((getattr(request, "headers", None) or {}).get("user-agent") or "")[:512],
        signed_at=now,
    )
    db.add(signature_row)
    await db.flush()
    _, prefix, _ = _bucket_storage_config()
    base = f"{prefix}/client-forms/{profile.id}/{KIND}/v{version.number}/signing/{session.id}"
    # Every object is named for this attempt's signature record, so a signing
    # that is retried after a failed save writes new objects instead of being
    # refused by the ones a rolled-back attempt left behind.
    signature_key = f"{base}/{role}-{signature_row.id}-signature.png"
    current = await asyncio.to_thread(client_forms.fetch, session.current_pdf_key) if session.current_pdf_key else None
    if not current:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The application PDF could not be read. Try again in a minute.")
    if session.current_sha256 and _sha(current) != session.current_sha256:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "document_changed", "message": "The application document changed. Ask your advisor to send it again."})
    try:
        stamped, _counts = await asyncio.to_thread(pdf_stamping.stamp_party, current, party=role, typed_name=typed, signature_png=png, signed_at=now)
        stamped_key = f"{base}/{len(session.chain) + 1:02d}-{role}-{signature_row.id}.pdf"
        await asyncio.to_thread(client_forms.store, signature_key, content_type or "image/png", png)
        await asyncio.to_thread(client_forms.store, stamped_key, "application/pdf", stamped)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Your signature could not be saved. Nothing was recorded; sign again in a minute.") from error
    signature_row.signature_s3_key = signature_key
    signature_row.signature_hash = png_hash
    session.chain = [*session.chain, {"role": role, "signature_id": str(signature_row.id), "input_sha256": _sha(current), "output_sha256": _sha(stamped), "at": now.isoformat()}]
    session.current_pdf_key = stamped_key
    session.current_sha256 = _sha(stamped)
    session.signers = {**(session.signers or {}), role: {
        "name": typed, "email": person.get("email"), "signature_id": str(signature_row.id), "signed_at": now.isoformat(),
        "ip": signature_row.ip_address, "link_id": str(getattr(link, "id", "") or "") or None,
    }}
    await db.flush()

    signature = session.signature_config
    if CREDIT_SATISFIER in (signature.get("satisfies") or []) and "credit_authorization" in (signature.get("consents") or []):
        # The record of the authorization is filed in its own savepoint: the
        # application signature stands whatever this step does.
        recorded = False
        try:
            async with db.begin_nested():
                recorded = await _record_credit_authorization(db, profile, intake, version, role=role, identity=identity, typed=typed, png_hash=png_hash, signature_row=signature_row)
        except Exception:  # noqa: BLE001
            log.exception("credit authorization record from the application failed for profile %s", profile.id)
    else:
        recorded = False

    signed_all = all(item in session.signers for item in session.required_roles)
    if signed_all:
        await _execute(db, profile, session, version, document_signature=document_signature)
    elif role == "borrower" and "coborrower" in session.required_roles and "coborrower" not in session.signers:
        await _invite_coborrower(db, profile, session, version)
    await _event(db, profile, title=f"Real estate application signed by {typed}", body="Every signer has signed; the signed application is filed." if signed_all else "Waiting for the other signer.", target=session.id)

    if recorded:
        # The signing is saved before the bureau is called. The pull keeps its
        # own failed attempts on record by committing, so nothing of the
        # signing may still be unsaved when it runs; and a bureau that is down
        # never erases the signature or the authorization (the desk pulls later).
        await db.commit()
        try:
            await _pull_credit(db, profile, intake, version, role=role, identity=identity, request=request, within_days=signature.get("credit_reuse_days"))
        except Exception:  # noqa: BLE001
            log.exception("soft pull from the application failed for profile %s", profile.id)
            if not db.is_active:
                # A write inside the pull failed: clear it so the reply can be read. The signing is already saved.
                await db.rollback()
                await db.refresh(profile)
    return await view(db, profile, role=role)


async def _event(db: AsyncSession, profile: Any, *, title: str, body: str, target: Any) -> None:
    try:
        from app.models.file_event import VISIBILITY_CLIENT
        from app.services import file_events

        await file_events.emit(db, profile=profile, kind="application.signed", visibility=VISIBILITY_CLIENT, title=title, body=body, actor_label="The client", target_type="form_signing_session", target_id=target)
    except Exception:  # noqa: BLE001
        log.exception("signing event failed for profile %s", profile.id)


def _certificate_rows(session: FormSigningSession, version: ClientFormVersion, role: str) -> list[tuple[str, str]]:
    signer = (session.signers or {}).get(role) or {}
    consents = ", ".join(f"{item['label']} ({item['version']})" for item in session.signature_config.get("consent_snapshot") or [])
    return [
        ("Document", f"{schema.TITLE}, version {version.number}"),
        ("Signer", str(signer.get("name") or "")),
        ("Role", schema.ROLE_LABELS.get(role, role)),
        ("Email", str(signer.get("email") or "")),
        ("Signed at", str(signer.get("signed_at") or "")),
        ("IP address", str(signer.get("ip") or "")),
        ("Agreed to", consents),
        ("Unsigned document SHA-256", session.unsigned_sha256),
        ("Signature record", str(signer.get("signature_id") or "")),
    ]


async def _execute(db: AsyncSession, profile: Any, session: FormSigningSession, version: ClientFormVersion, *, document_signature: Any) -> None:
    from app.dealer_os.services.contract_sign import _certificate_html
    from app.routers.dealer_ai_intake import _bucket_storage_config
    from app.services import pdf_stamping, re_application_pdf
    from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION
    from app.services.production_signing import _append_pages

    def build(current: bytes) -> bytes:
        final = pdf_stamping.redact_remaining_anchors(current)
        for role in session.required_roles:
            page = re_application_pdf.render_pdf(_certificate_html(_certificate_rows(session, version, role), "Electronic signature certificate"))
            final = _append_pages(final, page)
        return final

    try:
        current = await asyncio.to_thread(client_forms.fetch, session.current_pdf_key)
        if not current:
            raise RuntimeError("the signed application could not be read back")
        final = await asyncio.to_thread(build, current)
        digest = _sha(final)
        _, prefix, _ = _bucket_storage_config()
        # Named by its own bytes, so a retry after a failed save is never refused by an earlier attempt's object.
        key = f"{prefix}/client-forms/{profile.id}/{KIND}/v{version.number}/signed-{session.id}-{digest[:20]}.pdf"
        s3_version = await asyncio.to_thread(client_forms.store, key, "application/pdf", final)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The signed application could not be filed. Nothing was recorded; sign again in a minute.") from error
    signer_names = ", ".join(str((session.signers or {}).get(role, {}).get("name") or "") for role in session.required_roles)
    file = BucketFile(
        bucket_id=profile.primary_bucket_id,
        requested_document_id=session.requested_document_id,
        file_name=f"Real Estate Loan Application v{version.number} - Signed.pdf",
        s3_key=key,
        s3_version_id=s3_version,
        content_type="application/pdf",
        size_bytes=len(final),
        uploaded_by_name=signer_names[:160] or "Client",
        uploaded_by_email=str(((session.signers or {}).get("borrower") or {}).get("email") or "")[:320] or None,
        source_kind="generated",
        source_detail="signed_document",
        status="uploaded",
        content_hash=digest,
    )
    db.add(file)
    await db.flush()
    db.add(BucketFileAnalysis(
        bucket_file_id=file.id, bucket_id=profile.primary_bucket_id, content_hash=digest,
        analysis_version=CURRENT_FILE_ANALYSIS_VERSION, provider="signed_form", status="completed",
        classification="re_application", confidence="high",
        summary=f"{schema.TITLE} v{version.number}, signed by {signer_names}",
        analysis={"form_version_id": str(version.id), "signing_session_id": str(session.id), "signers": list(session.required_roles)},
        analyzed_at=_now(),
    ))
    slot = await db.get(BucketRequestedDocument, session.requested_document_id) if session.requested_document_id else None
    if slot is not None:
        slot.status = "uploaded"
    session.status = EXECUTED
    session.executed_file_id = file.id
    session.executed_at = _now()
    await db.flush()
    from app.routers.dealer_ai_intake import _send_signed_document_copy_email

    for role in session.required_roles:
        signer = (session.signers or {}).get(role) or {}
        if signer.get("email"):
            # Sent once the signing is saved: a copy of something that failed to save would be a copy of nothing.
            client_forms.defer(db, partial(
                _send_signed_document_copy_email, to_email=signer["email"], typed_name=str(signer.get("name") or ""),
                document_title=schema.TITLE, pdf_bytes=final,
            ))


def require_current_link(link: Any, session: FormSigningSession | None, role: str) -> None:
    """A co-borrower's link belongs to one signing pass.

    When the application is reopened and submitted again, the co-borrower may
    be someone else; the link emailed for the earlier pass must not sign the
    new one.
    """
    if role != "coborrower" or session is None:
        return
    if str(getattr(link, "label", "") or "") != f"{COBORROWER_LABEL}{session.id}":
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "link_outdated", "message": "This link is for an earlier version of the application. Use the newest link we emailed you, or ask your advisor for it."})


async def coborrower_link(db: AsyncSession, profile: Any, session: FormSigningSession, version: ClientFormVersion, *, created_by: Any = None) -> tuple[Any, str, str] | None:
    """The co-borrower's own address for this signing pass: (link, token, email), or None when no email is on the application."""
    email = schema.get_at(version.body, "borrowers.coborrower.email")
    if not email:
        return None
    link, token = await client_forms.standing_link(db, profile, role="coborrower", session_id=session.id, invitee_email=email, created_by=created_by)
    return link, token, email


def coborrower_invitation(*, to_email: str, borrower_name: str, url: str) -> None:
    from app.services.email.ses_client import send_email

    send_email(
        to_email=to_email,
        subject="Please review and sign the real estate loan application",
        body_text=(
            f"{borrower_name} signed a real estate loan application with Qualified Commercial and listed you as a co-borrower. "
            f"Please review it and sign:\n\n{url}\n\nWe will email you a short code to open it.\n\nQualified Commercial"
        ),
    )


async def _invite_coborrower(db: AsyncSession, profile: Any, session: FormSigningSession, version: ClientFormVersion) -> None:
    try:
        async with db.begin_nested():
            found = await coborrower_link(db, profile, session, version)
    except Exception:  # noqa: BLE001 - the desk can copy or email the co-borrower's link from the file
        log.exception("co-borrower link failed for profile %s", profile.id)
        return
    if found is None:
        return
    _, token, email = found
    borrower = ((session.signers or {}).get("borrower") or {}).get("name") or "Your co-borrower"
    client_forms.defer(db, lambda: coborrower_invitation(to_email=email, borrower_name=borrower, url=client_forms.form_url(token)))


def _applicant_of(identity: dict[str, Any]) -> dict[str, Any] | None:
    applicant = {key: identity.get(key) for key in ("legal_first_name", "legal_last_name", "dob", "street", "city", "state", "zip")}
    return applicant if all(applicant.values()) else None


async def _record_credit_authorization(
    db: AsyncSession, profile: Any, intake: Any, version: ClientFormVersion, *, role: str, identity: dict[str, Any], typed: str,
    png_hash: str, signature_row: Any,
) -> bool:
    """File the application's credit-authorization consent as the room's credit authorization.

    Returns whether a pull may follow. The borrower's is a record and a
    certificate on the room's credit row; a co-borrower's authorization is the
    application signature itself (they have no room row), so there is nothing
    more to file.
    """
    applicant = _applicant_of(identity)
    if intake is None or applicant is None:
        return False
    if role != "borrower":
        return True
    from app.models.bucket import BucketDocumentSignature
    from app.routers.dealer_ai_intake import _bucket_storage_config
    from app.services import document_signature

    slot = (
        await db.execute(
            select(BucketRequestedDocument).where(
                BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
                BucketRequestedDocument.signature_kind == "credit_authorization",
                BucketRequestedDocument.status.notin_(["closed", "superseded"]),
            ).order_by(BucketRequestedDocument.created_at)
        )
    ).scalars().first()
    if slot is None:
        slot = BucketRequestedDocument(
            bucket_id=profile.primary_bucket_id, name="Credit Report Authorization", category="Credit",
            description="Signed within the real estate loan application.", required=True, allow_multiple_files=False,
            status="requested", is_custom=False, requires_signature=True, signature_kind="credit_authorization",
            requirement_key=CREDIT_SATISFIER, requirement_source={"kind": "signed_form", "form_kind": KIND},
        )
        db.add(slot)
        await db.flush()
    if slot.status == "uploaded":
        return True
    text = document_signature.credit_authorization_document_text()
    record = BucketDocumentSignature(
        requested_document_id=slot.id,
        document_version=document_signature.CREDIT_AUTHORIZATION_DOCUMENT_VERSION,
        document_hash=document_signature.document_hash(text),
        typed_name=typed[:160], esign_consent=True,
        applicant_data={**applicant, "signed_within": {"form": KIND, "version": version.number, "signature_id": str(signature_row.id)}},
        ip_address=signature_row.ip_address, user_agent=signature_row.user_agent, signed_at=signature_row.signed_at,
        signature_s3_key=signature_row.signature_s3_key, signature_hash=png_hash,
    )
    db.add(record)
    await db.flush()
    pdf = await asyncio.to_thread(
        document_signature.render_signature_certificate_pdf,
        signature=record, title="Credit Report Authorization Certificate",
        document_text=f"{text}\n\nSigned within the {schema.TITLE}, version {version.number} (signature {signature_row.id}).",
        extra_rows=[("Applicant name", f"{applicant['legal_first_name']} {applicant['legal_last_name']}"), ("Date of birth", str(applicant["dob"])), ("Address", ", ".join(str(applicant[key]) for key in ("street", "city", "state", "zip")))],
        qc_signature_png=None,
    )
    _, prefix, _ = _bucket_storage_config()
    key = f"{prefix}/signatures/{profile.primary_bucket_id}/{record.id}/certificate.pdf"
    s3_version = await asyncio.to_thread(client_forms.store, key, "application/pdf", pdf)
    record.certificate_s3_key = key
    record.certificate_hash = _sha(pdf)
    file = BucketFile(
        bucket_id=profile.primary_bucket_id, requested_document_id=slot.id, file_name="Credit Report Authorization - Signed Certificate.pdf",
        s3_key=key, s3_version_id=s3_version, content_type="application/pdf", size_bytes=len(pdf),
        uploaded_by_name=typed[:160], source_kind="generated", source_detail="signed_document", status="uploaded", content_hash=_sha(pdf),
    )
    db.add(file)
    await db.flush()
    record.result_file_id = file.id
    slot.status = "uploaded"
    await db.flush()
    return True


async def _pull_credit(
    db: AsyncSession, profile: Any, intake: Any, version: ClientFormVersion, *, role: str, identity: dict[str, Any], request: Any, within_days: int | None,
) -> None:
    """The soft pull the signature authorized, under the reuse window (a recent pull is used, never repeated)."""
    from app.services import credit_pull_core

    applicant = _applicant_of(identity)
    if applicant is None:
        return
    if role == "borrower":
        await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=applicant, request=request, within_days=within_days)
        return
    # A co-borrower's own pull, linked to their owner row (a guarantor with no ownership share).
    from datetime import date

    from sqlalchemy import func

    from app.models.application_profile import ApplicationOwner
    from app.models.client import Client
    from app.services import credit_links

    client = await db.get(Client, profile.client_id) if getattr(profile, "client_id", None) else None
    if client is None:
        return
    first, last = str(applicant["legal_first_name"]).strip(), str(applicant["legal_last_name"]).strip()
    owner = (
        await db.execute(
            select(ApplicationOwner).where(
                ApplicationOwner.profile_id == profile.id,
                ApplicationOwner.archived_at.is_(None),
                # Equality, not a pattern: a name is never a wildcard.
                func.lower(func.trim(ApplicationOwner.last_name)) == last.lower(),
                func.lower(func.trim(ApplicationOwner.first_name)) == first.lower(),
            )
        )
    ).scalars().first()
    if owner is None:
        owner = ApplicationOwner(
            profile_id=profile.id, first_name=first, last_name=last,
            email=schema.get_at(version.body, "borrowers.coborrower.email"), is_primary=False, is_guarantor=True,
            dob=date.fromisoformat(str(applicant["dob"])), street=applicant["street"], city=applicant["city"], state=applicant["state"], zip=applicant["zip"],
        )
        db.add(owner)
        await db.flush()
    pull, reused = await credit_pull_core.pull_or_reuse(
        db, client=client,
        applicant=credit_pull_core.SoftPullApplicant(
            legal_first_name=first, legal_last_name=last, dob=date.fromisoformat(str(applicant["dob"])),
            street=str(applicant["street"]), city=str(applicant["city"]), state=str(applicant["state"]), zip=str(applicant["zip"]),
        ),
        email=owner.email, within_days=within_days,
    )
    await credit_links.link_pull(db, profile, owner, pull, how=credit_links.HOW_REUSED if reused else credit_links.HOW_SIGNATURE)


async def reopen(db: AsyncSession, profile: Any, *, actor: Any, reason: str) -> dict[str, Any]:
    """Send the application back to the client for changes.

    An unfinished signing pass is voided (never deleted), so signatures already
    given on that version no longer count. A fully signed version is never
    voided: it stays on file as signed, and the client's changes become a new
    version that is signed afresh.
    """
    await client_forms._lock(db, profile)
    form = await client_forms.get_form(db, profile.id, lock=True)
    version = await client_forms.current_version(db, form)
    session = await active_session(db, version)
    why = (reason or "").strip()[:500] or "Reopened by the desk"
    if form is None or form.status != "submitted":
        return await client_forms.read(db, profile, audience="desk")
    if session is not None and session.status == ACTIVE:
        session.status = VOID
        session.void_reason = why
        session.voided_at = _now()
        session.voided_by_user_id = getattr(actor, "id", None)
        await _retire_coborrower_links(db, profile, session)
    form.status = "draft"
    form.revision = int(form.revision) + 1
    await db.flush()
    signed = session is not None and session.status == EXECUTED
    await _desk_event(
        db, profile,
        title=f"Real estate application version {version.number} reopened" + (" for a new version" if signed else ""),
        body=why + (" The signed version stays on file; the client's changes are signed again as a new version." if signed else ""),
        actor=actor, target=form.id,
    )
    return await client_forms.read(db, profile, audience="desk")


async def _retire_coborrower_links(db: AsyncSession, profile: Any, session: FormSigningSession) -> None:
    """A voided signing pass takes its co-borrower's link with it."""
    from app.models.financial_form_link import FinancialFormLink

    if not isinstance(db, AsyncSession):
        return
    rows = (
        await db.execute(
            select(FinancialFormLink).where(
                FinancialFormLink.profile_id == profile.id, FinancialFormLink.kind == KIND,
                FinancialFormLink.label == f"{COBORROWER_LABEL}{session.id}", FinancialFormLink.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    for row in rows:
        row.revoked_at = _now()


async def _desk_event(db: AsyncSession, profile: Any, *, title: str, body: str, actor: Any, target: Any) -> None:
    try:
        from app.models.file_event import VISIBILITY_DESK
        from app.services import file_events

        await file_events.emit(db, profile=profile, kind="application.reopened", visibility=VISIBILITY_DESK, title=title, body=body, actor_label=getattr(actor, "name", None) or "The desk", target_type="client_form", target_id=target)
    except Exception:  # noqa: BLE001 - the timeline never undoes the reopen
        log.exception("reopen event failed for profile %s", profile.id)
