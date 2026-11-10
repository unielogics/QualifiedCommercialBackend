"""Staff-scoped batch file requests over canonical readiness definitions."""
# ruff: noqa: B008

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.routers.application_profiles import (
    _require_training_live_action,
    _require_underwriting_actor,
)
from app.schemas.file_request import FileRequestBatchCreate, FileRequestRetry
from app.services import application_profiles as profiles
from app.services import file_requests

router = APIRouter(prefix="/application-profiles/{profile_id}/file-requests", tags=["File requests"])


@router.get("")
async def list_requests(profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    # Batch history is already persisted by the request/collection workflows.
    # Reading it must not materialize unrelated program-readiness state or
    # acquire readiness's bucket/profile write locks.
    return await file_requests.list_batches(db, profile, refresh=False)


@router.post("")
async def create_requests(profile_id: UUID, payload: FileRequestBatchCreate, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await _require_training_live_action(db, profile=profile, user=user, request=request,
        action="Request files", provider="email" if payload.notify else "secure room", recipient=None,
        effect=f"Request {len(payload.items)} document item(s) from the client")
    await file_requests.create_batch(db, profile=profile, request_id=payload.request_id, items=payload.items, user=user)
    await db.commit()  # A failed email must never roll back the request itself.
    delivery = None
    if payload.notify:
        delivery = await file_requests.deliver_batch(db, profile=profile, request_id=payload.request_id, user=user)
        await db.commit()
    # create_batch synchronizes the requested destinations and borrower tasks.
    # Do not fail the response after that committed request/email by running an
    # unrelated readiness refresh solely to list the batch we just recorded.
    result = await file_requests.list_batches(db, profile, refresh=False)
    return {**result, "request_id": str(payload.request_id), "notification": file_requests.delivery_read(delivery) if delivery else None}


@router.post("/{request_id}/retry")
async def retry_notification(profile_id: UUID, request_id: UUID, payload: FileRequestRetry, request: Request, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await _require_training_live_action(db, profile=profile, user=user, request=request,
        action="Retry file request", provider="email", recipient=None, effect="Retry a failed document request email")
    row = await file_requests.deliver_batch(db, profile=profile, request_id=request_id, user=user, retry_id=payload.retry_id)
    await db.commit()
    return {"notification": file_requests.delivery_read(row)}


@router.post("/{request_id}/cancel")
async def cancel_request(profile_id: UUID, request_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    await file_requests.cancel_batch(db, profile=profile, request_id=request_id, user=user)
    await db.commit()
    return {"request_id": str(request_id), "status": "cancelled"}
