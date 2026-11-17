"""Determine whether a file's client has ever authenticated."""
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketActivityLog, BucketUploadLink
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User


async def client_has_signed_in(
    db: AsyncSession, profile: ApplicationProfile, link: BucketUploadLink,
) -> bool:
    if link.passcode_set_by_client_at is not None:
        return True
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    email = ((intake.email if intake else link.recipient_email) or "").strip().lower()
    room_identity = [BucketActivityLog.target_id == str(link.id)]
    if email:
        # A replacement invitation must not erase this client's access history.
        room_identity.append(func.lower(BucketActivityLog.actor_email) == email)
    accessed = (await db.execute(select(BucketActivityLog.id).where(
        BucketActivityLog.bucket_id == profile.primary_bucket_id,
        BucketActivityLog.target_type == "upload_link",
        or_(*room_identity),
        BucketActivityLog.action == "upload_link_accessed",
        BucketActivityLog.actor_role.in_(["uploader", "client"]),
    ).limit(1))).scalar_one_or_none()
    if accessed is not None:
        return True
    identities = []
    if profile.client_id:
        identities.append(Client.id == profile.client_id)
    if email.strip():
        identities.append(func.lower(User.email) == email.strip().lower())
    if not identities:
        return False
    signed_in = (await db.execute(select(User.id).outerjoin(Client, Client.user_id == User.id).where(
        User.role == Role.CLIENT,
        User.last_seen_at.is_not(None),
        or_(*identities),
    ).limit(1))).scalar_one_or_none()
    return signed_in is not None
