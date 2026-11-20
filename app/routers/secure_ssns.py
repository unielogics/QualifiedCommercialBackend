"""Dedicated routes keep full SSNs out of application drafts, exports and credit-pull flows."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.dealer_os.services import client_room
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.routers import buckets, client_forms
from app.schemas.bucket import BucketRequestAccessRequest
from app.services import form_link_codes, payments, secure_ssns


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def private(request: Request):
            try:
                # Production Caddy terminates TLS, overwrites this header and
                # forwards only to the API's loopback listener.
                if request.url.scheme != "https" and request.headers.get("x-forwarded-proto") != "https":
                    raise HTTPException(403, "Open the HTTPS portal to use secure SSN sharing.")
                response = await handle(request)
            except RequestValidationError:
                # FastAPI's standard 422 includes the submitted input, even when
                # a sibling field or the JSON itself is the part that was invalid.
                raise HTTPException(422, "Check the names and nine-digit SSNs.", headers={"Cache-Control": "private, no-store"}) from None
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "private, no-store"}
                raise
            response.headers["Cache-Control"] = "private, no-store"
            return response

        return private


router = APIRouter(prefix="/secure-ssns", tags=["secure-ssns"], route_class=PrivateRoute)
Db = Annotated[AsyncSession, Depends(get_db)]
Pin = Annotated[str, Header(alias="X-Room-Pin", min_length=1, max_length=80)]
Session = Annotated[str | None, Header(alias="X-Form-Session")]


def _require_account_auth():
    # CurrentUser supports simulated users for other screens in a local checkout.
    if not get_settings().clerk_secret_key:
        raise HTTPException(503, "Verified account access is unavailable.")


@router.post("/room/{token}", response_model=list[secure_ssns.Receipt])
async def room_entries(token: str, payload: secure_ssns.Submission, request: Request, pin: Pin, db: Db):
    link = await buckets._load_upload_link_or_404(db, token)
    await buckets._require_request_access_passcode(db, link, BucketRequestAccessRequest(passcode=pin), request)
    client_room.require_passcode_setup(link)
    profile = await db.scalar(select(ApplicationProfile).where(ApplicationProfile.primary_bucket_id == link.bucket_id)
                              .order_by(ApplicationProfile.created_at.desc()).limit(1))
    if profile is None:
        raise HTTPException(404, "Application file not found.")
    await payments.require_client_room_identity(db, link=link, profile=profile)
    return await secure_ssns.client_entries(db, profile, payload)


@router.post("/account/{intake_id}", response_model=list[secure_ssns.Receipt])
async def account_entries(intake_id: UUID, payload: secure_ssns.Submission, user: CurrentUser, db: Db):
    from app.routers.dealer_ai_intake import _load_client_intake

    _require_account_auth()
    if user.role != Role.CLIENT:
        raise HTTPException(403, "Client account required.")
    intake = await _load_client_intake(db, user, intake_id)
    client_room.require_passcode_setup(intake.bucket_upload_link)
    profile = await db.scalar(select(ApplicationProfile).where(ApplicationProfile.intake_id == intake.id))
    if profile is None:
        raise HTTPException(404, "Application file not found.")
    return await secure_ssns.client_entries(db, profile, payload)


@router.post("/application/{token}", response_model=list[secure_ssns.Receipt])
async def application_entries(token: str, payload: secure_ssns.Submission, request: Request, db: Db, session: Session = None):
    link, profile, _ = await client_forms._open_link(db, token, request)
    form_link_codes.require_session(session, link)
    if not client_forms.can_share_ssns(link, session):
        raise HTTPException(403, "Open your client room or verify the code emailed to the borrower.")
    return await secure_ssns.client_entries(db, profile, payload)


@router.get("/files/{profile_id}", response_model=list[secure_ssns.Receipt])
async def file_entries(profile_id: UUID, user: CurrentUser, db: Db):
    _require_account_auth()
    await client_forms._staff_profile(db, profile_id, user)
    return await secure_ssns.receipts(db, profile_id)


@router.post("/files/{profile_id}/{entry_id}/reveal")
async def reveal_entry(profile_id: UUID, entry_id: UUID, user: CurrentUser, db: Db) -> dict[str, str]:
    _require_account_auth()
    await client_forms._staff_profile(db, profile_id, user)
    return {"ssn": await secure_ssns.reveal(db, profile_id, entry_id, user.id)}
