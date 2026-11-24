"""Record the optional choice from a completed, verified Clerk signup."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.user import User


async def enroll_verified_signup(db: AsyncSession, user: User, clerk_user: dict | None) -> None:
    if user.role not in {
        Role.CLIENT, Role.DEALER, Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER, Role.CAPITAL_PARTNER,
    } or not clerk_user:
        return
    metadata = clerk_user.get("unsafe_metadata")
    if not isinstance(metadata, dict) or metadata.get("qc_lending_updates_v1") is not True:
        return
    signup_email = metadata.get("qc_lending_updates_email_v1")
    if not isinstance(signup_email, str) or signup_email.strip().casefold() != user.email.casefold():
        return
    primary_id = clerk_user.get("primary_email_address_id")
    if not primary_id:
        return
    for address in clerk_user.get("email_addresses") or []:
        if (
            address.get("id") == primary_id
            and (address.get("verification") or {}).get("status") == "verified"
            and (address.get("email_address") or "").casefold() == user.email.casefold()
        ):
            from app.services.email_marketing import enroll

            await enroll(
                db, email=user.email, consent=True, source="account_signup", outcome="account_created",
                client_id=user.client.id if user.role == Role.CLIENT and user.client else None,
                locale=user.ui_locale or "en",
            )
            return
