"""Record a contact's documented off-platform permission without starting a sequence."""
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.email_marketing import MarketingEnrollment, MarketingSubscriber
from app.models.user import User
from app.services.email_marketing import CONSENT_VERSION
from app.services.email_marketing_directory import contact_hold_reason, directory_contains
from app.services.prospect_outreach import _lock_suppression_addresses, normalize_email, valid_email

router = APIRouter(prefix="/admin/email-marketing", tags=["email-marketing"])


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(max_length=320)
    source: Literal["written_request", "signed_form", "subscription_record"]
    reference: str = Field(min_length=5, max_length=300)
    granted_on: date
    locale: Literal["en", "es"]
    confirmed: StrictBool

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        if not valid_email(value):
            raise ValueError("Enter a valid email address.")
        return normalize_email(value)


@router.post("/consent-evidence")
async def record_evidence(payload: Evidence, db: Annotated[AsyncSession, Depends(get_db)],
                          user: Annotated[User, Depends(require_role(Role.SUPER_ADMIN))]):
    if not payload.confirmed or payload.granted_on > datetime.now(UTC).date():
        raise HTTPException(422, "Confirm the documented permission and enter the date it was granted.")
    address = payload.email
    if not await directory_contains(db, address):
        raise HTTPException(404, "This contact is no longer in the directory.")
    await _lock_suppression_addresses(db, [address])
    if await db.scalar(select(contact_hold_reason(literal(address)))):
        raise HTTPException(409, "This contact has an active contact restriction.")
    subscriber = await db.scalar(insert(MarketingSubscriber).values(id=uuid4(), email=address, locale=payload.locale)
                                 .on_conflict_do_nothing(index_elements=["email"]).returning(MarketingSubscriber))
    if subscriber is None:
        subscriber = await db.scalar(select(MarketingSubscriber).where(
            MarketingSubscriber.email == address).with_for_update())
    if subscriber.unsubscribed_at is not None:
        raise HTTPException(409, "This contact previously unsubscribed.")
    enrollment_id = uuid5(subscriber.id, "documented-email-marketing-consent")
    inserted_id = await db.scalar(insert(MarketingEnrollment).values(
        id=enrollment_id, subscriber_id=subscriber.id, source="documented_opt_in", consent_version=CONSENT_VERSION,
    ).on_conflict_do_nothing(index_elements=["id"]).returning(MarketingEnrollment.id))
    if inserted_id:
        db.add(Activity(actor_id=user.id, actor_label="super_admin", kind="email_marketing.consent_recorded",
                        summary="Documented permission for educational and lending-update emails",
                        payload={"subscriber_id": str(subscriber.id), "enrollment_id": str(enrollment_id),
                                 "source": payload.source, "reference": payload.reference,
                                 "granted_on": payload.granted_on.isoformat(), "consent_version": CONSENT_VERSION}))
    await db.commit()
    return {"recorded": True}
