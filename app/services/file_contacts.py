"""Who the client is on a file, for the timeline's notices.

One resolver, reused instead of the three rules that already exist: the
login when there is one (`Client.user_id`, `DealerBusiness.dealer_user_id`),
else an email in the order the room requests already use — the intake's own
address, the dealer's primary owner else the business, the client record.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerBusiness, DealerOwner
from app.models.application_profile import ApplicationProfile
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake


@dataclass
class Sources:
    intake: PublicUnderwritingIntake | None
    dealer: DealerBusiness | None
    client: Client | None


@dataclass
class ClientRecipient:
    user_id: UUID | None
    email: str | None
    name: str | None


async def load_sources(db: AsyncSession, profile: ApplicationProfile) -> Sources:
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    dealer = await db.get(DealerBusiness, profile.dealer_id) if profile.dealer_id else None
    client_id = profile.client_id or (intake.client_id if intake is not None else None)
    client = await db.get(Client, client_id) if client_id else None
    return Sources(intake=intake, dealer=dealer, client=client)


async def _primary_owner(db: AsyncSession, dealer: DealerBusiness) -> DealerOwner | None:
    return (
        await db.execute(
            select(DealerOwner)
            .where(DealerOwner.dealer_id == dealer.id)
            .order_by(DealerOwner.is_primary.desc(), DealerOwner.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def client_recipient(db: AsyncSession, profile: ApplicationProfile, sources: Sources | None = None) -> ClientRecipient:
    src = sources or await load_sources(db, profile)
    user_id: UUID | None = None
    if src.client is not None and src.client.user_id:
        user_id = src.client.user_id
    elif src.dealer is not None and src.dealer.dealer_user_id:
        user_id = src.dealer.dealer_user_id
    email: str | None = None
    name: str | None = None
    if src.intake is not None:
        email, name = src.intake.email, src.intake.full_name
    if not email and src.dealer is not None:
        owner = await _primary_owner(db, src.dealer)
        if owner is not None and owner.email:
            email = owner.email
            name = f"{owner.first_name or ''} {owner.last_name or ''}".strip() or None
        elif src.dealer.email:
            email = src.dealer.email
    if not email and src.client is not None:
        email, name = src.client.email, name or src.client.name
    return ClientRecipient(user_id=user_id, email=email, name=name)


def business_label(sources: Sources) -> str:
    if sources.intake is not None and sources.intake.business_name:
        return sources.intake.business_name
    if sources.dealer is not None:
        return sources.dealer.legal_name or sources.dealer.name or "your application"
    if sources.client is not None and sources.client.name:
        return sources.client.name
    return "your application"
