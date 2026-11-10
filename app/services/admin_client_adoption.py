"""Attach an operator-created MCA application to its existing CRM client."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.enums import Role
from app.models.bucket import Bucket
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.scoping import scope_client_query

MCA_PRODUCT_KEY = "merchant_cash_advance"


async def client_for_admin_mca_application(db: Any, user: Any, payload: Any, *, intake_variant: str) -> Client | None:
    client_id = getattr(payload, "client_id", None)
    if client_id is None:
        return None
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(403, "Super-admin role required")
    if payload.variant != "main_street" or str(payload.intent or "").strip().casefold() != MCA_PRODUCT_KEY:
        raise HTTPException(422, "Existing-client adoption requires the MCA funding product")
    # The lock serializes retries for a client before looking for its active file.
    client = (await db.execute(scope_client_query(user, select(Client).where(Client.id == client_id)).with_for_update())).scalar_one_or_none()
    if client is None:
        raise HTTPException(404, "Client not found")
    current_email = str(client.email or "").strip().casefold()
    proposed_email = str(payload.email or "").strip().casefold()
    if current_email and current_email != proposed_email:
        raise HTTPException(409, "Review the saved client email before creating this MCA file")
    if not payload.force_new:
        state = PublicUnderwritingIntake.intake_state
        existing = (await db.execute(
            select(PublicUnderwritingIntake.id)
            .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
            .where(PublicUnderwritingIntake.client_id == client.id,
                   PublicUnderwritingIntake.variant == intake_variant,
                   Bucket.archived_at.is_(None),
                   or_(state["funding_intent"].astext == MCA_PRODUCT_KEY,
                       state["main_street_details"]["intent"].astext == MCA_PRODUCT_KEY))
            .order_by(PublicUnderwritingIntake.updated_at.desc()).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, {"message": "An active MCA file already exists for this client.", "intake_id": str(existing)})
    return client
