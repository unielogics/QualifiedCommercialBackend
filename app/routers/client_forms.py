# ruff: noqa: B008
"""The real-estate loan application: the desk's routes and the client's link.

Staff (underwriting roles) read the application as the file knows it, correct
answers, send the client a link and download any submitted version. The client
opens the link with a one-time code emailed to them (or arrives from a room
they already signed into, which hands over a session), fills only what the
file does not know yet, reviews everything and submits.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.request_context import client_ip
from app.services import application_profiles as profiles
from app.services import client_forms, financial_statements, form_link_codes, link_throttle
from app.services import re_application_schema as schema

router = APIRouter(tags=["client-forms"])

_STAFF = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
KIND = schema.KIND


class FormSave(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    confirm: list[str] = Field(default_factory=list, max_length=400)
    has_coborrower: bool | None = None
    revision: int = Field(ge=0)


class FormSubmit(BaseModel):
    revision: int = Field(ge=0)
    idempotency_key: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.:-]{8,80}$")


class FormLinkCreate(BaseModel):
    email: EmailStr | None = None
    send_email: bool = False


class CodeVerify(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ConsentAccept(BaseModel):
    key: str = Field(max_length=40)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FormSign(BaseModel):
    typed_name: str = Field(min_length=2, max_length=160)
    signature_data_url: str = Field(min_length=20)
    consents: list[ConsentAccept] = Field(max_length=20)
    esign_consent: bool
    version_id: UUID
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FormReopen(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


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
    from app.services import signed_forms

    view = await client_forms.read(db, profile, audience="desk")
    return {**_with_schema(view), "signing": await signed_forms.view(db, profile)}


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
    """A link the client opens with an emailed code. Optionally emails it now."""
    _require_kind(kind)
    profile = await _staff_profile(db, profile_id, user)
    intake = await client_forms.intake_for(db, profile)
    email = str(payload.email) if payload.email else (getattr(intake, "email", None) or None)
    if not email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The link needs the client's email; the code to open it is sent there.")
    link, token = await client_forms.mint_link(db, profile, invitee_email=email, created_by=user.id)
    url = client_forms.form_url(token)
    emailed = False
    if payload.send_email:
        from app.services.email.ses_client import send_email

        result = send_email(
            to_email=email,
            subject="Your real estate loan application",
            body_text=(
                "Most of your application is already filled in from what you told us. Open it to answer what is "
                f"left, review everything and sign:\n\n{url}\n\nWe will email you a short code to open it. "
                "The link works for 30 days.\n\nQualified Commercial"
            ),
        )
        emailed = bool(getattr(result, "ok", False))
    await profiles.log_profile_action(
        db, profile, user, "client_form.link_minted", "Created a link to the real estate application" + (" and emailed it" if emailed else ""),
        target_type="financial_form_link", target_id=link.id,
    )
    await db.commit()
    return {"url": url, "expires_at": link.expires_at, "email": form_link_codes.mask_email(email), "emailed": emailed}


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


@router.get("/public/forms/{token}")
async def open_client_form(token: str, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> dict:
    link, profile, _ = await _open_link(db, token, request)
    if not form_link_codes.session_valid(x_form_session, link):
        return {"kind": KIND, "title": schema.TITLE, "needs_code": True, "email_hint": form_link_codes.mask_email(link.invitee_email)}
    view = await client_forms.read(db, profile, audience="client")
    return {**_with_schema(view), "needs_code": False}


@router.post("/public/forms/{token}/code")
async def send_client_form_code(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    link, profile, key = await _open_link(db, token, request)
    if link_throttle.write_capped(key):
        raise link_throttle.too_many()
    link_throttle.note_write(key)
    email = link.invitee_email or getattr(await client_forms.intake_for(db, profile), "email", None)
    if not email:
        raise HTTPException(status.HTTP_409_CONFLICT, "This link has no email to send a code to. Ask your advisor for a new link.")
    code = form_link_codes.new_code(link)
    await db.commit()
    form_link_codes.send_code_email(to_email=email, code=code, form_title="real estate loan application")
    return {"sent": True, "email_hint": form_link_codes.mask_email(email)}


@router.post("/public/forms/{token}/verify")
async def verify_client_form_code(token: str, payload: CodeVerify, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    link, _, key = await _open_link(db, token, request)
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
    data = await client_forms.preview_pdf(db, profile)
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="real-estate-application.pdf"'})


@router.get("/public/forms/{token}/signing")
async def client_form_signing(token: str, request: Request, x_form_session: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import signed_forms

    link, profile, _ = await _open_link(db, token, request)
    form_link_codes.require_session(x_form_session, link)
    return await signed_forms.view(db, profile, role=signed_forms.role_for_link(link))


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
    return result


async def hand_off(db: AsyncSession, profile: ApplicationProfile, *, email: str | None) -> str:
    """A link opened from a room the client is already signed into: no code to type."""
    link, token = await client_forms.mint_link(db, profile, invitee_email=email, label="From the client's room")
    session, _ = form_link_codes.issue_session(link)
    return f"{client_forms.form_url(token)}#s={session}"
