# ruff: noqa: B008
"""The real-estate loan application: the desk's routes and the client's link.

Staff (underwriting roles) read the application as the file knows it, correct
answers, send the client a link and download any submitted version. The client
opens the link with a one-time code emailed to them (or arrives from a room
they already signed into, which hands over a session), fills only what the
file does not know yet, reviews everything and submits.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.request_context import client_ip
from app.services import application_profiles as profiles
from app.services import (
    client_forms,
    financial_statements,
    form_link_codes,
    link_throttle,
    secure_ssns,
)
from app.services import re_application_schema as schema

router = APIRouter(tags=["client-forms"])

_STAFF = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
KIND = schema.KIND


class FormSave(BaseModel):
    # The whole application has under two hundred fields; anything larger is not a person typing.
    changes: dict[str, Any] = Field(default_factory=dict, max_length=400)
    confirm: list[str] = Field(default_factory=list, max_length=400)
    has_coborrower: bool | None = None
    revision: int = Field(ge=0)


class FormSubmit(BaseModel):
    revision: int = Field(ge=0)
    idempotency_key: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.:-]{8,80}$")


class FormLinkCreate(BaseModel):
    #: Another address than the client's own: a separate, one-off link whose code goes there.
    email: EmailStr | None = None
    send_email: bool = False
    #: "coborrower" returns the co-borrower's signing link while the application waits for them.
    role: Literal["borrower", "coborrower"] = "borrower"
    #: Retire the file's current link and start a new one.
    reset: bool = False


class CodeVerify(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ConsentAccept(BaseModel):
    key: str = Field(max_length=40)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FormSign(BaseModel):
    typed_name: str = Field(min_length=2, max_length=160)
    signature_data_url: str = Field(min_length=20, max_length=2_000_000)
    consents: list[ConsentAccept] = Field(max_length=20)
    esign_consent: bool
    version_id: UUID
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FormReopen(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class FormShare(BaseModel):
    recipient_name: str = Field(min_length=2, max_length=180)
    organization: str | None = Field(default=None, max_length=180)
    recipient_email: EmailStr | None = None


class SigningApproval(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=500)


def _require_kind(kind: str) -> None:
    if kind != KIND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown form")


async def _staff_profile(db: AsyncSession, profile_id: UUID, user: Any) -> ApplicationProfile:
    if user.role not in _STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Underwriter access required")
    return await profiles.load_profile(db, profile_id, user)


def _with_schema(view: dict[str, Any]) -> dict[str, Any]:
    return {**view, "schema": schema.describe()}


async def _desk_view(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    from app.services import application_relay, signed_forms

    view = await client_forms.read(db, profile, audience="desk")
    standing = await client_forms.current_standing_link(db, profile)
    return {
        **_with_schema(view),
        "signing": await signed_forms.view(db, profile),
        "relay": await application_relay.relay_read(db, profile.id),
        # The file's own address for this application, when one has been made (copying makes it).
        "link": {"url": client_forms.form_url(standing[1]), "expires_at": standing[0].expires_at} if standing else None,
    }


# --- the desk -----------------------------------------------------------------


@router.get("/application-profiles/{profile_id}/forms/{kind}")
async def read_client_form(profile_id: UUID, kind: str, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    return await _desk_view(db, profile)


@router.put("/application-profiles/{profile_id}/forms/{kind}")
async def save_client_form(profile_id: UUID, kind: str, payload: FormSave, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    view = await client_forms.save(
        db, profile, changes=payload.changes, confirm=payload.confirm, has_coborrower=payload.has_coborrower,
        revision=payload.revision, actor="desk",
    )
    await profiles.log_profile_action(db, profile, user, "client_form.saved", f"Updated {len(payload.changes)} answers on the real estate application", target_type="client_form", target_id=None)
    await db.commit()
    return _with_schema(view)


@router.post("/application-profiles/{profile_id}/forms/{kind}/submit")
async def submit_client_form(profile_id: UUID, kind: str, payload: FormSubmit, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    view = await client_forms.submit(db, profile, revision=payload.revision, actor="desk", user=user, idempotency_key=payload.idempotency_key)
    await profiles.log_profile_action(db, profile, user, "client_form.submitted", "Submitted the real estate application for signature", target_type="client_form", target_id=None)
    await db.commit()
    return _with_schema(view)


@router.post("/application-profiles/{profile_id}/forms/{kind}/link")
async def create_client_form_link(profile_id: UUID, kind: str, payload: FormLinkCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    """The address that opens this file's application, to copy or to email.

    The client's link is the file's own standing address: the same one every
    time it is copied, opened with a code emailed to the file's current
    address. A co-borrower has their own, for the signing pass that waits on
    them. Another email address gets a separate one-off link.
    """
    from app.services import signed_forms, signed_forms_state

    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    intake = await client_forms.intake_for(db, profile)
    own = (getattr(intake, "email", None) or "").strip() or None
    borrower_name = None
    if payload.role == "coborrower":
        form = await client_forms.get_form(db, profile.id)
        version = await client_forms.current_version(db, form)
        session = await signed_forms_state.live_session(db, version.id) if form is not None and form.status == "submitted" and version is not None else None
        if session is None or session.status != signed_forms.ACTIVE or "coborrower" not in session.required_roles or "coborrower" in (session.signers or {}):
            raise HTTPException(status.HTTP_409_CONFLICT, {"code": "no_coborrower_pending", "message": "The application is not waiting for a co-borrower's signature."})
        found = await signed_forms.coborrower_link(db, profile, session, version, created_by=user.id)
        if found is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The application has no email for the co-borrower.")
        link, token, email = found
        borrower_name = ((session.signers or {}).get("borrower") or {}).get("name") or "Your co-borrower"
    elif payload.email and str(payload.email).lower() != (own or "").lower():
        email = str(payload.email).lower()
        link, token = await client_forms.mint_link(db, profile, invitee_email=email, created_by=user.id)
    else:
        if not own:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The link needs the client's email; the code to open it is sent there.")
        email = own
        link, token = await client_forms.standing_link(db, profile, created_by=user.id, reset=payload.reset)
    url = client_forms.form_url(token)
    await profiles.log_profile_action(
        db, profile, user, "client_form.link_minted",
        ("Reset" if payload.reset and payload.role == "borrower" else "Copied") + f" the {'co-borrower signing ' if payload.role == 'coborrower' else ''}link to the real estate application",
        target_type="financial_form_link", target_id=link.id,
    )
    expires_at = link.expires_at
    await db.commit()
    emailed = False
    if payload.send_email:
        # After the link is saved: an emailed link that failed to save would open nothing.
        def send() -> bool:
            if borrower_name is not None:
                signed_forms.coborrower_invitation(to_email=email, borrower_name=borrower_name, url=url)
                return True
            from app.services.email.ses_client import send_email

            result = send_email(
                to_email=email,
                subject="Your real estate loan application",
                body_text=(
                    "Most of your application is already filled in from what you told us. Open it to answer what is "
                    f"left, review everything and sign:\n\n{url}\n\nWe will email you a short code to open it."
                    "\n\nQualified Commercial"
                ),
            )
            return bool(getattr(result, "ok", False))

        try:
            emailed = await asyncio.to_thread(send)
        except Exception:  # noqa: BLE001 - the link exists; the desk can copy it
            emailed = False
        if emailed:
            await profiles.log_profile_action(db, profile, user, "client_form.link_emailed", f"Emailed the real estate application link to {form_link_codes.mask_email(email)}", target_type="financial_form_link", target_id=link.id)
            await db.commit()
    return {"url": url, "expires_at": expires_at, "email": form_link_codes.mask_email(email), "emailed": emailed, "role": payload.role}


@router.post("/application-profiles/{profile_id}/forms/{kind}/reopen")
async def reopen_client_form(profile_id: UUID, kind: str, payload: FormReopen, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    """Void a signing pass that has not finished so the client can change the application."""
    from app.services import signed_forms

    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    await signed_forms.reopen(db, profile, actor=user, reason=payload.reason)
    await profiles.log_profile_action(db, profile, user, "client_form.reopened", f"Reopened the real estate application: {payload.reason}"[:500], target_type="client_form", target_id=None)
    await db.commit()
    return await _desk_view(db, profile)


@router.post("/application-profiles/{profile_id}/forms/{kind}/share")
async def share_client_form(profile_id: UUID, kind: str, payload: FormShare, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    """A seven-day passcode link to the signed application only, for a partner who is not on the lender roster.

    Lenders on the roster receive the signed application in their submission
    package; this is for anyone else the information-sharing consent covers.
    The passcode is in this reply only.
    """
    from app.services import application_relay

    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    result = await application_relay.share_with_partner(
        db, profile, actor=user, recipient_name=payload.recipient_name, organization=payload.organization,
        recipient_email=str(payload.recipient_email) if payload.recipient_email else None, request=request,
    )
    await profiles.log_profile_action(
        db, profile, user, "client_form.shared", f"Shared the signed real estate application with {result['recipient']}"[:500],
        target_type="bucket_share", target_id=UUID(result["share_id"]),
    )
    await db.commit()
    return result


@router.get("/form-kinds/signing-approval")
async def read_signing_approval(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import signed_forms

    if user.role not in _STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Underwriter access required")
    return {**await signed_forms.approval(db), "signing_open": await signed_forms.signing_open(db)}


@router.put("/form-kinds/signing-approval")
async def write_signing_approval(payload: SigningApproval, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    """The CEO's approval of the consent wording opens signing in production."""
    from app.services import signed_forms

    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    result = await signed_forms.set_approval(db, approved=payload.approved, actor=user, note=payload.note)
    await db.commit()
    return {**result, "signing_open": await signed_forms.signing_open(db)}


@router.get("/application-profiles/{profile_id}/forms/{kind}/versions/{version_id}/pdf")
async def client_form_version_pdf(profile_id: UUID, kind: str, version_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> Response:
    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    data = await client_forms.version_pdf(db, profile, version_id)
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="real-estate-application.pdf"'})


@router.get("/application-profiles/{profile_id}/forms/{kind}/preview.pdf")
async def client_form_preview_pdf(profile_id: UUID, kind: str, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> Response:
    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    data = await client_forms.preview_pdf(db, profile)
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="real-estate-application-preview.pdf"'})


# --- the client's link ------------------------------------------------------------


async def _open_link(db: AsyncSession, token: str, request: Request):
    key = link_throttle.miss_key(token, client_ip(request), prefix="client-form")
    if link_throttle.locked(key):
        raise link_throttle.too_many()
    link = await financial_statements.link_for_token(db, token)
    if link is None or link.kind != KIND:
        link_throttle.note_miss(key)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is no longer available")
    profile = await db.get(ApplicationProfile, link.profile_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is no longer available")
    return link, profile, key


async def _coborrower_guard(db: AsyncSession, profile: ApplicationProfile, link: Any) -> None:
    """A co-borrower's link reads and signs the submitted application of its own signing pass, nothing else."""
    from app.services import signed_forms

    if signed_forms.role_for_link(link) == "coborrower":
        await signed_forms.view(db, profile, role="coborrower", link=link)


async def _lock_link(db: AsyncSession, link: Any) -> None:
    """Serialize code requests and checks on one link, so parallel guesses share one count."""
    from app.models.financial_form_link import FinancialFormLink

    await db.execute(select(FinancialFormLink.id).where(FinancialFormLink.id == link.id).with_for_update())
    await db.refresh(link)
    if not client_forms.link_open(link):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is no longer available")


def can_share_ssns(link: Any, session: str | None) -> bool:
    return bool(secure_ssns.configured() and get_settings().clerk_secret_key
                and link.label == client_forms.STANDING_LABEL and not link.invitee_email
                and link.permission == "edit"
                and form_link_codes.session_valid(session, link, require_email=True))


@router.get("/public/forms/{token}")
async def open_client_form(token: str, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> dict:
    link, profile, _ = await _open_link(db, token, request)
    if not form_link_codes.session_valid(x_form_session, link):
        hint = link.invitee_email or getattr(await client_forms.intake_for(db, profile), "email", None)
        return {"kind": KIND, "title": schema.TITLE, "needs_code": True, "email_hint": form_link_codes.mask_email(hint)}
    await _coborrower_guard(db, profile, link)
    view = await client_forms.read(db, profile, audience="client")
    return {**_with_schema(view), "needs_code": False, "can_share_ssns": can_share_ssns(link, x_form_session)}


@router.post("/public/forms/{token}/code")
async def send_client_form_code(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    link, profile, key = await _open_link(db, token, request)
    if link_throttle.write_capped(key):
        raise link_throttle.too_many()
    link_throttle.note_write(key)
    email = link.invitee_email or getattr(await client_forms.intake_for(db, profile), "email", None)
    if not email:
        raise HTTPException(status.HTTP_409_CONFLICT, "This link has no email to send a code to. Ask your advisor for a new link.")
    await _lock_link(db, link)
    code = form_link_codes.new_code(link)
    await db.commit()
    form_link_codes.send_code_email(to_email=email, code=code, form_title="real estate loan application")
    return {"sent": True, "email_hint": form_link_codes.mask_email(email)}


@router.post("/public/forms/{token}/verify")
async def verify_client_form_code(token: str, payload: CodeVerify, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    link, _, key = await _open_link(db, token, request)
    await _lock_link(db, link)
    try:
        session, expires = form_link_codes.verify_code(link, payload.code)
    except HTTPException:
        link_throttle.note_miss(key)
        await db.commit()
        raise
    await db.commit()
    return {"session": session, "expires_at": expires}


@router.put("/public/forms/{token}/draft")
async def save_client_form_draft(token: str, payload: FormSave, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import signed_forms

    link, profile, _ = await _open_link(db, token, request)
    form_link_codes.require_session(x_form_session, link)
    if signed_forms.role_for_link(link) == "coborrower":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the borrower can change the application. Review it and sign, or ask them to make the change.")
    view = await client_forms.save(
        db, profile, changes=payload.changes, confirm=payload.confirm, has_coborrower=payload.has_coborrower,
        revision=payload.revision, actor="client",
    )
    await db.commit()
    return {**_with_schema(view), "needs_code": False}


@router.post("/public/forms/{token}/submit")
async def submit_client_form_public(token: str, payload: FormSubmit, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> dict:
    link, profile, _ = await _open_link(db, token, request)
    form_link_codes.require_session(x_form_session, link)
    view = await client_forms.submit(db, profile, revision=payload.revision, actor="client", idempotency_key=payload.idempotency_key)
    link.completed_at = link.completed_at or client_forms._now()
    await db.commit()
    return {**_with_schema(view), "needs_code": False}


@router.get("/public/forms/{token}/preview.pdf")
async def client_form_public_preview(token: str, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> Response:
    link, profile, _ = await _open_link(db, token, request)
    form_link_codes.require_session(x_form_session, link)
    await _coborrower_guard(db, profile, link)
    data = await client_forms.preview_pdf(db, profile)
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="real-estate-application.pdf"'})


@router.get("/public/forms/{token}/signing")
async def client_form_signing(token: str, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import signed_forms

    link, profile, _ = await _open_link(db, token, request)
    form_link_codes.require_session(x_form_session, link)
    return await signed_forms.view(db, profile, role=signed_forms.role_for_link(link), link=link)


@router.post("/public/forms/{token}/sign")
async def sign_client_form(token: str, payload: FormSign, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import signed_forms

    link, profile, _ = await _open_link(db, token, request)
    form_link_codes.require_session(x_form_session, link)
    result = await signed_forms.sign(
        db, profile, role=signed_forms.role_for_link(link), link=link, typed_name=payload.typed_name,
        signature_data_url=payload.signature_data_url, consents=[item.model_dump() for item in payload.consents],
        esign_consent=payload.esign_consent, version_id=payload.version_id, body_sha256=payload.body_sha256, request=request,
    )
    await db.commit()
    # Copies and the co-borrower's invitation go out only now that the signature is saved.
    await client_forms.run_deferred(db)
    return result


async def hand_off(db: AsyncSession, profile: ApplicationProfile, *, email: str | None) -> str:
    """A link opened from a room the client is already signed into: no code to type."""
    # The room opens the file's own standing address: one link per file, however often the client comes through.
    link, token = await client_forms.standing_link(db, profile)
    session, _ = form_link_codes.issue_session(link)
    return f"{client_forms.form_url(token)}#s={session}"
