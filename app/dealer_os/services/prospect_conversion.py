"""Explicit conversion of Marketing prospects into application workflows.

Marketing is deliberately pre-application.  This module is the only bridge
from a prospect into either a Portfolio application or Dealer AI Intake, and
all helpers flush rather than commit so a failed room/file setup rolls back
without moving the prospect to Converted.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dealer_prospect import DealerProspect
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User

from ..deps import is_rep
from ..models import (
    DealerApplicationContact,
    DealerBusiness,
    DealerRepCompany,
    DealerRepContact,
    DealerRepLead,
    DealerSourceConnection,
)
from ..prospect_schemas import ProspectPortfolioApplicationCreate
from . import buckets_link, client_room
from .audit import log_action
from .precall import next_case_ref
from .prospects import TEAM_ROLES, normalize_dealer_name, normalize_email, normalize_phone
from .targets import propose_targets

ConversionTarget = Literal["portfolio_application", "dealer_ai_intake"]
log = logging.getLogger(__name__)


def _conversion_signal_lock_key(kind: str, value: str) -> int:
    """Hash one normalized identity signal into a PostgreSQL advisory key."""

    material = f"marketing-conversion\x1f{kind}\x1f{value}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)


def conversion_identity_lock_keys(prospect: DealerProspect) -> tuple[int, ...]:
    """Return stable, ordered locks for every non-empty match signal.

    Destination matching is an OR across dealer name, email, and phone.  A
    composite hash would fail to serialize two records that share only one of
    those values, so each signal gets its own lock.  Sorting the integer keys
    gives all transactions the same acquisition order and prevents deadlocks
    when two prospects overlap on more than one signal.
    """

    signals = (
        ("dealer_name", str(prospect.dealer_name_normalized or "")),
        ("email", str(prospect.email_normalized or "")),
        ("phone", str(prospect.phone_normalized or "")),
    )
    return tuple(
        sorted(
            {
                _conversion_signal_lock_key(kind, value)
                for kind, value in signals
                if value
            }
        )
    )


async def acquire_conversion_identity_lock(
    db: AsyncSession, prospect: DealerProspect
) -> None:
    """Serialize Marketing conversions sharing a dealer/contact identity.

    These locks are transaction-scoped, so they are released automatically by
    the request transaction.  Callers must acquire them *before* rescanning
    destination candidates and keep the same transaction through creation.

    This helper intentionally scopes only the Marketing conversion workflow.
    Ordinary Portfolio and public AI Intake creation paths do not currently
    participate in these locks; widening that contract safely requires those
    independent workflows to normalize identity the same way.
    """

    for key in conversion_identity_lock_keys(prospect):
        await db.execute(select(func.pg_advisory_xact_lock(key)))


def _portfolio_identity_match(prospect: DealerProspect):
    digits = re.sub(r"\D", "", prospect.phone_normalized)
    normalized_name = func.lower(
        func.regexp_replace(
            func.btrim(func.coalesce(DealerBusiness.name, "")),
            r"\s+",
            " ",
            "g",
        )
    )
    return or_(
        func.lower(DealerBusiness.email) == prospect.email_normalized,
        func.regexp_replace(
            func.coalesce(DealerBusiness.phone, ""), "[^0-9]", "", "g"
        )
        == digits,
        normalized_name == prospect.dealer_name_normalized,
    )


def portfolio_candidate_match_reasons(
    prospect: DealerProspect, application: DealerBusiness
) -> list[str]:
    reasons: list[str] = []
    if application.email and normalize_email(application.email) == prospect.email_normalized:
        reasons.append("email")
    application_phone = normalize_phone(application.phone) if application.phone else None
    if application_phone and application_phone == prospect.phone_normalized:
        reasons.append("phone")
    if application.name and normalize_dealer_name(application.name) == prospect.dealer_name_normalized:
        reasons.append("dealer_name")
    return reasons


async def portfolio_candidates(
    db: AsyncSession, prospect: DealerProspect, user: User
) -> list[DealerBusiness]:
    """Return visible active or archived Portfolio matches without leaking other reps' files."""

    stmt = (
        select(DealerBusiness)
        .where(
            DealerBusiness.is_training.is_(False),
            _portfolio_identity_match(prospect),
        )
        .order_by(DealerBusiness.created_at.desc())
        .limit(20)
    )
    if user.role not in TEAM_ROLES:
        visible_owner_ids = {user.id}
        if prospect.owner_user_id is not None:
            visible_owner_ids.add(prospect.owner_user_id)
        stmt = stmt.where(DealerBusiness.owner_user_id.in_(visible_owner_ids))
    return list((await db.execute(stmt)).scalars().all())


async def portfolio_restricted_match_exists(
    db: AsyncSession, prospect: DealerProspect, user: User
) -> bool:
    """Check for matching Portfolio files the caller may not discover.

    Only a boolean crosses this boundary.  Candidate identities, ownership,
    match reasons, counts, and record ids must never be returned to a scoped
    rep or broker.
    """

    if user.role in TEAM_ROLES:
        return False
    visible_owner_ids = {user.id}
    if prospect.owner_user_id is not None:
        visible_owner_ids.add(prospect.owner_user_id)
    hidden_owner = or_(
        DealerBusiness.owner_user_id.is_(None),
        DealerBusiness.owner_user_id.not_in(visible_owner_ids),
    )
    stmt = select(
        select(DealerBusiness.id)
        .where(
            DealerBusiness.is_training.is_(False),
            _portfolio_identity_match(prospect),
            hidden_owner,
        )
        .exists()
    )
    return bool((await db.execute(stmt)).scalar_one())


async def _link_contact(
    db: AsyncSession, prospect: DealerProspect, application: DealerBusiness
) -> None:
    existing = (
        await db.execute(
            select(DealerApplicationContact.id).where(
                DealerApplicationContact.dealer_id == application.id,
                DealerApplicationContact.contact_id == prospect.primary_contact_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            DealerApplicationContact(
                dealer_id=application.id,
                contact_id=prospect.primary_contact_id,
                relationship="primary_contact",
                is_primary=True,
            )
        )
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    if contact is not None and contact.dealer_id is None:
        contact.dealer_id = application.id


async def create_portfolio_application(
    db: AsyncSession,
    prospect: DealerProspect,
    user: User,
    payload: ProspectPortfolioApplicationCreate,
) -> DealerBusiness:
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    company = await db.get(DealerRepCompany, prospect.company_id)
    if contact is None or company is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Prospect contact is incomplete")

    owner_id = prospect.owner_user_id or user.id
    application = DealerBusiness(
        name=company.name,
        legal_name=company.name,
        email=contact.email or prospect.email_normalized,
        phone=contact.phone_e164 or prospect.phone_normalized,
        entity_type=payload.entity_type,
        funding_goal=Decimal(str(payload.requested_amount)),
        client_requested_amount=Decimal(str(payload.requested_amount)),
        funding_purpose=payload.funding_purpose,
        use_of_proceeds_note=payload.use_of_proceeds_note,
        industry="auto_dealer",
        industry_label="Auto dealer",
        application_lifecycle="active",
        status="active",
        owner_user_id=owner_id,
        case_ref=await next_case_ref(db),
        source_kind="dealer_prospect",
        source_detail=f"Marketing prospect {prospect.id}"[:200],
        source_actor_name=(user.name or user.email or "")[:200],
        source_user_id=user.id,
    )
    db.add(application)
    await db.flush()
    db.add(DealerSourceConnection(dealer_id=application.id, kind="uploads", status="active"))
    await propose_targets(db, application)
    # A prospect-to-Portfolio conversion must not silently adopt an unrelated
    # AI Intake room merely because an email address happens to match.
    await buckets_link.ensure_bucket(db, application, adopt_intake=False)
    try:
        room = await client_room.initialize_room(db, application, payload.secure_room_pin)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:
        log.exception(
            "dealer-os: client room creation failed for Marketing conversion %s",
            application.id,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The secure client room could not be created. Try converting the prospect again.",
        ) from exc
    await log_action(
        db,
        application.id,
        user,
        "room.passcode_initialized",
        "dealer",
        entity_id=application.id,
        after={"link_id": str(room.link.id), "expires": False},
    )

    owner = await db.get(User, owner_id)
    if owner is not None and is_rep(owner):
        db.add(
            DealerRepLead(
                dealer_id=application.id,
                rep_user_id=owner_id,
                status="draft",
                status_history=[
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "from": None,
                        "to": "draft",
                        "by": str(user.id),
                        "by_name": user.name,
                        "source": "prospect_conversion",
                    }
                ],
            )
        )
    await _link_contact(db, prospect, application)
    await log_action(
        db,
        application.id,
        user,
        "application.created_from_prospect",
        "dealer",
        entity_id=application.id,
        after={"prospect_id": str(prospect.id)},
    )
    await db.flush()
    return application


async def link_portfolio_application(
    db: AsyncSession,
    prospect: DealerProspect,
    application: DealerBusiness,
    user: User,
    *,
    reactivate: bool,
) -> None:
    if reactivate:
        application.archived_at = None
        application.archived_by_user_id = None
        application.application_lifecycle = "active"
        if application.status in {"archived", "draft"}:
            application.status = "active"
        await log_action(
            db,
            application.id,
            user,
            "application.reactivated_from_prospect",
            "dealer",
            entity_id=application.id,
            after={"prospect_id": str(prospect.id)},
        )
    await _link_contact(db, prospect, application)
    await db.flush()


def intake_archived(intake: PublicUnderwritingIntake) -> bool:
    return bool(
        getattr(intake, "delete_requested_at", None)
        or getattr(intake, "status", None) in {"archived", "deleted"}
    )


def conversion_route(
    target: ConversionTarget, destination_id
) -> str:
    if target == "portfolio_application":
        return f"/applications/{destination_id}"
    return f"/admin/ai-underwriter-leads?lead={destination_id}"
