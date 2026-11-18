"""A personal financial statement for each owner: the desk's tracker and its actions.

The logic is in `app/services/owner_statements.py`. These routes are the desk's
side of it: whether the file asks for statements at all, who owes one, where
each one stands, a private link per owner, and whose statement an upload is.
The owner's own page is the existing no-login form page; their link simply
opens their statement and nobody else's. Sending a link or asking an owner is
the desk asking the file for statements, so it puts them on the checklist.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.services import application_profiles as profiles
from app.services import owner_statements

router = APIRouter(
    prefix="/application-profiles/{profile_id}/owner-statements", tags=["owner-statements"]
)
Db = Annotated[AsyncSession, Depends(get_db)]


class OwnerRequirementWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: bool
    reason: str | None = Field(default=None, max_length=500)


class StatementOwnersWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_ids: list[UUID] = Field(max_length=5)
    #: With no owners: this is no owner's statement on this file, so stop listing it.
    set_aside: bool = False


class FileOwnerWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: UUID


class MissingLinksWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_ids: list[UUID] | None = Field(default=None, max_length=5)


async def _staff_profile(db: AsyncSession, profile_id: UUID, user):
    from app.routers.application_profiles import _require_statement_staff

    profile = await profiles.load_profile(db, profile_id, user)
    _require_statement_staff(user)
    return profile


async def _underwriting_profile(db: AsyncSession, profile_id: UUID, user):
    from app.routers.application_profiles import _require_underwriting_actor

    _require_underwriting_actor(user)
    return await profiles.load_profile(db, profile_id, user)


async def _refresh_checklist(db: AsyncSession, profile) -> None:
    """Let the checklist row and the room catch up with what the desk just decided.

    Advisory: the decision is already written, and a readiness problem must not
    undo it.
    """
    from app.services import application_programs

    try:
        async with db.begin_nested():
            await application_programs.get_program_readiness(db, profile)
    except Exception:  # noqa: BLE001 - the saved decision stands
        await db.refresh(profile)


@router.get("")
async def read_owner_statements(profile_id: UUID, user: CurrentUser, db: Db) -> dict:
    """Every owner beside their statement: who owes one, where it stands, its versions."""
    profile = await _staff_profile(db, profile_id, user)
    return await owner_statements.read(db, profile)


@router.post("/{owner_id}/open")
async def open_owner_statement(profile_id: UUID, owner_id: UUID, user: CurrentUser, db: Db) -> dict:
    """The owner's statement, started if they have none, for the desk to fill in or review."""
    profile = await _staff_profile(db, profile_id, user)
    owner = await owner_statements._owner_or_404(db, profile, owner_id)
    family = await owner_statements.statement_for(db, profile, owner, actor=user)
    await db.commit()
    return {"family_id": str(family.id), "label": family.label}


@router.post("/{owner_id}/link", status_code=status.HTTP_201_CREATED)
async def mint_owner_statement_link(profile_id: UUID, owner_id: UUID, user: CurrentUser, db: Db) -> dict:
    """A private link to this owner's own statement. Shown once; a lost link is made again."""
    profile = await _staff_profile(db, profile_id, user)
    owner = await owner_statements._owner_or_404(db, profile, owner_id)
    name = owner_statements.owner_name(owner)
    # Sending a link is asking: a file that asked for no statements now does.
    if await owner_statements.ask_for_file(db, profile, user=user):
        await _refresh_checklist(db, profile)
    link, token = await owner_statements.mint_link(
        db, profile, owner, user=user, label=f"Copied for {name}", invitee_email=profiles.normalized_email(owner.email)
    )
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "owner_statement.link_minted",
        f"{owner_statements.LABEL}: created a private link for {name}",
        target_type="owner",
        target_id=owner.id,
        metadata={"link_id": str(link.id)},
    )
    await db.commit()
    return {
        "url": f"{get_settings().frontend_app_url.rstrip('/')}/forms/{owner_statements.KIND}/{token}",
        "expires_at": link.expires_at,
    }


async def _email(db: AsyncSession, profile_id: UUID, profile, owner, user) -> dict:
    from app.routers.application_profiles import _room_delivery_read, request_financial_form

    # On the checklist first. Idempotent, and it commits its own write.
    await request_financial_form(profile_id, owner_statements.KIND, user, db)
    slot = await owner_statements.live_slot(db, profile)
    delivery = await owner_statements.email_link(db, profile, owner, user=user, slot=slot)
    await db.commit()
    receipt = _room_delivery_read(delivery)
    return {
        "owner_id": str(owner.id),
        "owner_name": owner_statements.owner_name(owner),
        "sent": delivery.status == "sent",
        "detail": delivery.detail,
        "receipt": receipt.model_dump(mode="json"),
    }


@router.post("/{owner_id}/email-link")
async def email_owner_statement_link(profile_id: UUID, owner_id: UUID, user: CurrentUser, db: Db) -> dict:
    """Email one owner the link to their own statement, and put the statement on the checklist."""
    profile = await _underwriting_profile(db, profile_id, user)
    owner = await owner_statements._owner_or_404(db, profile, owner_id)
    return await _email(db, profile_id, profile, owner, user)


@router.post("/email-missing")
async def email_missing_owner_statements(
    profile_id: UUID, user: CurrentUser, db: Db, payload: MissingLinksWrite | None = None
) -> dict:
    """Email every owner whose statement is still missing their own link, each to their own address."""
    profile = await _underwriting_profile(db, profile_id, user)
    tracker = await owner_statements.load(db, profile)
    chosen = set(payload.owner_ids) if payload and payload.owner_ids is not None else None
    if chosen is not None and chosen.difference(row.owner.id for row in tracker.rows):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found")
    # Plain values first: each send commits, and nothing after it should need a reload.
    waiting = [
        (row.owner.id, owner_statements.owner_name(row.owner), bool(profiles.normalized_email(row.owner.email)))
        for row in tracker.rows
        if row.required
        and owner_statements.row_status(row) != "received"
        and (chosen is None or row.owner.id in chosen)
    ]
    items: list[dict] = []
    for owner_id, name, has_email in waiting:
        if not has_email:
            items.append({"owner_id": str(owner_id), "owner_name": name, "sent": False, "detail": "No personal email on file for this owner"})
            continue
        owner = await owner_statements._owner_or_404(db, profile, owner_id)
        try:
            items.append(await _email(db, profile_id, profile, owner, user))
        except HTTPException as error:
            # One owner who cannot be emailed does not stop the others. A
            # rollback expires what was loaded, so the file is read again.
            await db.rollback()
            profile = await profiles.load_profile(db, profile_id, user)
            items.append({"owner_id": str(owner_id), "owner_name": name, "sent": False, "detail": str(error.detail)})
    return {"items": items}


@router.put("/{owner_id}/requirement")
async def set_owner_statement_requirement(
    profile_id: UUID, owner_id: UUID, payload: OwnerRequirementWrite, user: CurrentUser, db: Db
) -> dict:
    """Ask an owner under 20% for a statement, or record why one is not needed from this owner."""
    profile = await _underwriting_profile(db, profile_id, user)
    owner = await owner_statements._owner_or_404(db, profile, owner_id)
    await owner_statements.set_requirement(
        db, profile, owner, required=payload.required, reason=payload.reason, user=user
    )
    await _refresh_checklist(db, profile)
    await db.commit()
    return await owner_statements.read(db, profile)


@router.put("/statements/{family_id}/owners")
async def assign_statement_owners(
    profile_id: UUID, family_id: UUID, payload: StatementOwnersWrite, user: CurrentUser, db: Db
) -> dict:
    """Say whose statement this is. Two owners make it joint; none takes it off its owner, or sets it aside."""
    profile = await _underwriting_profile(db, profile_id, user)
    await owner_statements.assign(
        db, profile, family_id, payload.owner_ids, user=user, set_aside=payload.set_aside
    )
    await _refresh_checklist(db, profile)
    await db.commit()
    return await owner_statements.read(db, profile)


@router.post("/files/{file_id}/assign")
async def assign_uploaded_statement(
    profile_id: UUID, file_id: UUID, payload: FileOwnerWrite, user: CurrentUser, db: Db
) -> dict:
    """Put an uploaded statement that belongs to no statement into one owner's history."""
    profile = await _underwriting_profile(db, profile_id, user)
    owner = await owner_statements._owner_or_404(db, profile, payload.owner_id)
    await owner_statements.adopt_file(db, profile, file_id, owner, user=user)
    await _refresh_checklist(db, profile)
    await db.commit()
    return await owner_statements.read(db, profile)
