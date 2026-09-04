"""Dealer -> funding-file handoff — Phase 3 Wave 2.

start_handoff turns a monitored DealerBusiness into an AI-underwriter funding
intake by mirroring the SAME creation path the admin dealer-variant lead uses
(app/routers/dealer_ai_intake.py::create_admin_ai_lead): DealerIntakeStart
adapter -> _find_or_create_client -> _create_bucket_for_intake -> a
PublicUnderwritingIntake row with a fresh public token. All of those helpers
are imported READ-ONLY (lazily, inside the function, so module import order
never couples dealer_os to the intake router at startup).

Idempotency contract: dealers carry a plain-UUID breadcrumb
(dos_dealers.handoff_intake_id, no FK by design). When it points at an intake
that still exists, start_handoff returns it without creating anything; when
the intake was deleted, a fresh one is created and the breadcrumb re-stamped.

Flushes, never commits — callers own the transaction boundary.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

from ..models import DealerBusiness
from .audit import log_action

# Where the team opens the created funding file.
HANDOFF_URL_TEMPLATE = (
    "https://app.qualifiedcommercial.com/admin/ai-underwriter-leads?lead={intake_id}"
)


def handoff_url(intake_id: UUID | str) -> str:
    return HANDOFF_URL_TEMPLATE.format(intake_id=intake_id)


def resolve_existing_handoff_id(
    handoff_intake_id: UUID | None, intake_exists: bool
) -> UUID | None:
    """Pure idempotency core: the stored breadcrumb wins only while the intake
    it points at still exists; a dangling breadcrumb means 'create a new one'."""
    if handoff_intake_id is not None and intake_exists:
        return handoff_intake_id
    return None


async def find_existing_handoff(db: AsyncSession, dealer: DealerBusiness) -> UUID | None:
    """The dealer's live handoff intake id, or None (never set / since deleted)."""
    if dealer.handoff_intake_id is None:
        return None
    from app.models.public_underwriting_intake import PublicUnderwritingIntake  # read-only

    intake = await db.get(PublicUnderwritingIntake, dealer.handoff_intake_id)
    return resolve_existing_handoff_id(dealer.handoff_intake_id, intake is not None)


async def start_handoff(
    db: AsyncSession, dealer: DealerBusiness, user: User, request: Request
) -> UUID:
    """Create (or return the existing) funding intake for this dealer.

    Mirrors the admin dealer-variant create path via read-only imports.
    Requires dealer.email (400 otherwise). Flushes, never commits.
    """
    existing = await find_existing_handoff(db, dealer)
    if existing is not None:
        return existing

    if not (dealer.email or "").strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Client has no email on file — add one before starting a funding file.",
        )

    # READ-ONLY reuse of the intake creation helpers (lazy import by contract).
    from app.models.public_underwriting_intake import PublicUnderwritingIntake
    from app.routers.dealer_ai_intake import (
        DEALER_VARIANT,
        DealerIntakeStart,
        _create_bucket_for_intake,
        _find_or_create_client,
        _hash_token,
        _new_public_token,
        _normalize_email,
    )

    adapter = DealerIntakeStart(
        full_name=dealer.name.strip(),
        email=_normalize_email(dealer.email),
        phone=dealer.phone,
        business_name=(dealer.legal_name or dealer.name).strip(),
    )
    client = await _find_or_create_client(db, adapter)
    bucket, link = await _create_bucket_for_intake(db, client, adapter, request)

    token = _new_public_token()
    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        broker_id=None,  # house-attributed, same as admin-created leads
        token_hash=_hash_token(token),
        variant=DEALER_VARIANT,
        full_name=adapter.full_name,
        email=client.email or adapter.email,
        phone=dealer.phone,
        business_name=adapter.business_name,
        asset_rows=[],
        intake_state={"source": "dealer_os_handoff", "messages": []},
        # The acting user used to survive only in dos_audit_log, so the funding
        # file itself could not say who handed it over.
        source_kind="capital_os_handoff",
        source_actor_name=(user.name or user.email or "")[:200],
        source_user_id=user.id,
        source_detail=f"From Capital OS file {dealer.id}"[:200],
    )
    db.add(intake)
    await db.flush()

    dealer.handoff_intake_id = intake.id
    await log_action(
        db,
        dealer.id,
        user,
        "handoff",
        "dealer",
        entity_id=dealer.id,
        after={
            "intake_id": str(intake.id),
            "bucket_id": str(bucket.id),
            "email": intake.email,
            "variant": DEALER_VARIANT,
        },
    )
    return intake.id
