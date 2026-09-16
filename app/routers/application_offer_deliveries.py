"""Combined offer email, immutable PDFs, and client inbox decisions."""

# FastAPI dependencies intentionally use callable defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.dealer_os.models import DealerRepInboxThread
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_offer_delivery import (
    ApplicationOfferDelivery,
    ApplicationOfferDeliveryItem,
)
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketUploadLink
from app.models.merchant_processing_offer import MerchantProcessingOffer
from app.models.user import User
from app.routers import application_communications as communications
from app.routers import application_profiles as profile_routes
from app.routers.application_terms import apply_terms_issued_lifecycle
from app.routers.buckets import _client_ip, _verify_passcode
from app.schemas.application_offer_delivery import (
    ApplicationTermSheetItemRef,
    AuthenticatedOfferResponse,
    ManualOfferResponse,
    MerchantOfferItemRef,
    OfferDeliveriesRead,
    OfferDeliveryCreate,
    OfferDeliveryRead,
    OfferDeliveryReconciliationRead,
    OfferDeliveryReconciliationRequest,
    OfferDraftRequest,
    OfferDraftResponse,
    OfferItemRef,
    OfferResponseResult,
    ProductionTermSheetItemRef,
    PublicOfferDeliveryAccess,
    PublicOfferResponse,
)
from app.services import application_offer_deliveries as offers
from app.services import application_profiles as profiles
from app.services import merchant_processing, notifications

log = logging.getLogger(__name__)
PROVIDER_HANDOFF_STALE_MINUTES = 30

router = APIRouter(prefix="/application-profiles", tags=["application-offer-deliveries"])
client_router = APIRouter(
    prefix="/application-profiles/client",
    tags=["client-application-offer-deliveries"],
)


def _require_operator(user: User) -> None:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Underwriting role required")


async def _operator_profile(db: AsyncSession, profile_id: UUID, user: User) -> ApplicationProfile:
    _require_operator(user)
    return await profiles.load_profile(db, profile_id, user)


def _ip(request: Request) -> str | None:
    return _client_ip(request)


def _disposition(value: str) -> Literal["inline", "attachment"]:
    return "attachment" if value == "attachment" else "inline"


def _content_disposition(filename: str, disposition: str) -> str:
    safe_name = communications._safe_attachment_filename(filename)
    fallback = safe_name.encode("ascii", "ignore").decode("ascii").replace('"', "_") or "document"
    encoded = quote(safe_name, safe="")
    return f"{_disposition(disposition)}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _pdf_response(data: bytes, filename: str, disposition: str) -> Response:
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition(filename, disposition),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{hashlib.sha256(data).hexdigest()}"',
        },
    )


def _stored_document_response(
    data: bytes, item: ApplicationOfferDeliveryItem, disposition: str
) -> Response:
    safe_inline = item.content_type in {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
        "text/csv",
    }
    selected = _disposition(disposition) if safe_inline else "attachment"
    media_type = item.content_type if safe_inline else "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(item.file_name, selected),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{item.sha256}"',
        },
    )


async def _load_delivery(
    db: AsyncSession,
    *,
    delivery_id: UUID,
    profile_id: UUID | None = None,
    published_only: bool = False,
    lock: bool = False,
) -> ApplicationOfferDelivery:
    stmt = (
        select(ApplicationOfferDelivery)
        .where(ApplicationOfferDelivery.id == delivery_id)
        .options(selectinload(ApplicationOfferDelivery.items))
    )
    if profile_id is not None:
        stmt = stmt.where(ApplicationOfferDelivery.profile_id == profile_id)
    if published_only:
        stmt = stmt.where(
            ApplicationOfferDelivery.published_at.is_not(None),
            ApplicationOfferDelivery.status.notin_(["sending", "failed"]),
        )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer delivery not found.")
    return row


def _delivery_base(profile_id: UUID) -> str:
    return f"/api/v1/application-profiles/{profile_id}/offer-deliveries"


def _provider_correlation_id(delivery_id: UUID) -> str:
    """Stable application correlation persisted before transport starts."""

    return f"qc-offer-{delivery_id}"


def _provider_headers(delivery: ApplicationOfferDelivery) -> dict[str, str]:
    correlation = delivery.provider_correlation_id
    return {
        "Message-ID": f"<{correlation}@qualifiedcommercial.com>",
        "X-QC-Offer-Correlation": correlation,
    }


def _provider_outcome_is_uncertain(message) -> bool:
    """A transport exception may happen after Gmail/SES accepted the MIME."""

    detail = str(message.provider_error or "").strip().lower()
    if detail in {"not_configured", "bad recipients", "bad recipient"}:
        return False
    return not detail or detail.startswith(("gmail_send_failed:", "send_failed:"))


async def _persist_uncertain_provider_outcome(
    db: AsyncSession,
    *,
    delivery: ApplicationOfferDelivery,
    message,
) -> None:
    delivery.status = "sending"
    delivery.provider = message.provider
    delivery.provider_detail = (
        "Provider handoff outcome is uncertain. Verify the stable correlation before reconciling."
    )
    delivery.message_send_id = message.message_send_id
    await db.commit()


def _reconciliation_read(
    delivery: ApplicationOfferDelivery,
) -> OfferDeliveryReconciliationRead:
    return OfferDeliveryReconciliationRead(
        delivery_id=delivery.id,
        status=delivery.status,
        provider_correlation_id=delivery.provider_correlation_id,
        provider_handoff_started_at=delivery.provider_handoff_started_at,
        reconciliation_available_at=(
            delivery.provider_handoff_started_at
            + timedelta(minutes=PROVIDER_HANDOFF_STALE_MINUTES)
            if delivery.provider_handoff_started_at
            else None
        ),
        provider=delivery.provider,
        provider_message_id=delivery.provider_message_id,
        sent_at=delivery.sent_at,
        reconciled_at=delivery.reconciled_at,
        reconciled_by_user_id=delivery.reconciled_by_user_id,
        reconciliation_outcome=delivery.reconciliation_outcome,
    )


async def _list_profile_deliveries(
    db: AsyncSession,
    profile_id: UUID,
    *,
    published_only: bool,
    base_url: str,
    allowed_delivery_ids: set[UUID] | None = None,
    uncertain_delivery_ids: set[UUID] | None = None,
) -> OfferDeliveriesRead:
    stmt = (
        select(ApplicationOfferDelivery)
        .where(ApplicationOfferDelivery.profile_id == profile_id)
        .options(selectinload(ApplicationOfferDelivery.items))
        .order_by(ApplicationOfferDelivery.created_at.desc())
    )
    if published_only:
        published = (
            ApplicationOfferDelivery.published_at.is_not(None)
            & ApplicationOfferDelivery.status.notin_(["sending", "failed"])
        )
        uncertain = (
            ApplicationOfferDelivery.status == "sending"
        ) & ApplicationOfferDelivery.id.in_(uncertain_delivery_ids or set())
        stmt = stmt.where(or_(published, uncertain))
    if allowed_delivery_ids is not None:
        stmt = stmt.where(ApplicationOfferDelivery.id.in_(allowed_delivery_ids))
    rows = list((await db.execute(stmt)).scalars().unique().all())
    changed = False
    for row in rows:
        before = row.status
        if row.published_at is not None and row.status not in {"sending", "failed"}:
            offers.refresh_delivery_status(row)
        changed = changed or before != row.status
    if changed:
        await db.commit()
    return OfferDeliveriesRead(
        deliveries=[offers.delivery_read(row, base_url=base_url) for row in rows]
    )


def _request_fingerprint(payload: OfferDeliveryCreate) -> str:
    serialized = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@router.post(
    "/{profile_id}/communications/email/offer-draft",
    response_model=OfferDraftResponse,
)
async def draft_combined_offer_email(
    profile_id: UUID,
    payload: OfferDraftRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDraftResponse:
    profile = await _operator_profile(db, profile_id, user)
    await communications._enforce_direct_client_contact(db, profile)
    resolved = await offers.resolve_offers(db, profile, payload.items)
    return await offers.build_draft(
        db,
        profile,
        resolved,
        guidance=payload.guidance,
        user_id=user.id,
    )


@router.get("/{profile_id}/offer-items/{kind}/{source_id}/document")
async def preview_current_offer_document(
    profile_id: UUID,
    kind: Literal["merchant_offer", "production_term_sheet", "application_term_sheet"],
    source_id: UUID,
    expected_version: int,
    user: CurrentUser,
    disposition: str = Query(default="inline"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    from app.schemas.application_offer_delivery import (
        ApplicationTermSheetItemRef,
        MerchantOfferItemRef,
        ProductionTermSheetItemRef,
    )

    profile = await _operator_profile(db, profile_id, user)
    ref = (
        MerchantOfferItemRef(kind=kind, offer_id=source_id, expected_version=expected_version)
        if kind == "merchant_offer"
        else ProductionTermSheetItemRef(
            kind=kind, term_sheet_id=source_id, expected_version=expected_version
        )
        if kind == "production_term_sheet"
        else ApplicationTermSheetItemRef(
            kind=kind, term_sheet_id=source_id, expected_version=expected_version
        )
    )
    resolved = (await offers.resolve_offers(db, profile, [ref]))[0]
    pdf = await offers.render_base_pdf(db, profile, resolved)
    return _pdf_response(pdf, resolved.file_name, disposition)


async def _verified_recipients(
    db: AsyncSession,
    profile: ApplicationProfile,
    payload: OfferDeliveryCreate,
):
    contacts, _ = await communications._contacts(db, profile)
    by_id = {item.id: item for item in contacts}
    to = by_id.get(payload.to_contact_id)
    if to is None or not to.email:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Choose a current file contact with an email address.",
        )
    cc_emails: list[str] = []
    for contact_id in dict.fromkeys(payload.cc_contact_ids):
        contact = by_id.get(contact_id)
        if contact is None or not contact.email:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Every Cc recipient must be a current file contact with an email address.",
            )
        if contact.email != to.email:
            cc_emails.append(contact.email)
    return to, list(dict.fromkeys(cc_emails))


async def _revalidate_saved_recipients(
    db: AsyncSession,
    profile: ApplicationProfile,
    delivery: ApplicationOfferDelivery,
) -> None:
    contacts, _ = await communications._contacts(db, profile)
    by_id = {item.id: item for item in contacts}
    to = by_id.get(delivery.to_contact_id)
    if to is None or not to.email:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The saved recipient is no longer a verified file contact. Create a new delivery.",
        )
    cc_emails: list[str] = []
    for contact_id in delivery.cc_contact_ids or []:
        contact = by_id.get(contact_id)
        if contact is None or not contact.email:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A saved Cc recipient is no longer a verified file contact. Create a new delivery.",
            )
        if contact.email != to.email:
            cc_emails.append(contact.email)
    current_cc = list(dict.fromkeys(cc_emails))
    if to.email != delivery.recipient_emails[0] or current_cc != list(delivery.cc_emails or []):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Recipient details changed after this delivery was saved. Review them and use a new delivery key.",
        )


def _ref_for_snapshot(item: ApplicationOfferDeliveryItem):
    if item.kind == "merchant_offer":
        return MerchantOfferItemRef(
            kind="merchant_offer",
            offer_id=item.source_id,
            expected_version=item.source_version or 1,
        )
    if item.kind == "production_term_sheet":
        return ProductionTermSheetItemRef(
            kind="production_term_sheet",
            term_sheet_id=item.source_id,
            expected_version=item.source_version or 1,
        )
    if item.kind == "application_term_sheet":
        return ApplicationTermSheetItemRef(
            kind="application_term_sheet",
            term_sheet_id=item.source_id,
            expected_version=item.source_version or 1,
        )
    return None


def _ordered_offer_refs(refs: list[OfferItemRef]) -> list[OfferItemRef]:
    """Keep row-lock acquisition stable across differently ordered requests."""

    kind_order = {
        "production_term_sheet": 0,
        "application_term_sheet": 0,
        "merchant_offer": 1,
    }

    def key(ref: OfferItemRef) -> tuple[int, str]:
        source_id = (
            getattr(ref, "term_sheet_id", None)
            or getattr(ref, "offer_id", None)
            or UUID(int=0)
        )
        return kind_order.get(ref.kind, 99), str(source_id)

    return sorted(refs, key=key)


async def _lock_sources_for_provider_handoff(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    refs: list[OfferItemRef],
    delivery: ApplicationOfferDelivery,
) -> None:
    """Revalidate and hold source-row locks until acceptance is persisted."""

    try:
        await offers.resolve_offers(
            db,
            profile,
            _ordered_offer_refs(list(refs)),
            lock=True,
        )
    except BaseException as original_error:
        # No provider call has started yet, so this state is known-safe to
        # retry. Reload after rollback because a deadlock invalidates the
        # transaction and expires the in-memory ORM state.
        await db.rollback()
        try:
            persisted = await _load_delivery(
                db,
                delivery_id=delivery.id,
                profile_id=profile.id,
                lock=True,
            )
            if persisted.status == "sending" and not persisted.provider_message_id:
                persisted.status = "failed"
                persisted.provider_detail = (
                    "The selected offer changed before provider handoff. Create a new draft."
                    if isinstance(original_error, HTTPException)
                    else "Delivery preparation stopped before email provider handoff; retry is safe."
                )
                await db.commit()
        except Exception:
            await db.rollback()
            log.exception(
                "could not mark offer delivery %s retryable after a pre-provider failure",
                delivery.id,
            )
        raise


async def _persist_provider_acceptance(
    db: AsyncSession,
    *,
    delivery: ApplicationOfferDelivery,
    message,
    accepted_at: datetime,
) -> None:
    """Publish first, in the smallest post-provider transaction possible."""

    delivery.status = "sent"
    delivery.sent_at = accepted_at
    delivery.published_at = accepted_at
    delivery.provider = message.provider
    delivery.provider_message_id = message.provider_message_id
    delivery.provider_detail = None
    delivery.message_send_id = message.message_send_id
    await db.commit()


async def _apply_delivery_effects(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    delivery_id: UUID,
    user: User,
) -> None:
    """Idempotently apply lifecycle/audit effects after the delivery is public."""

    delivery = await _load_delivery(
        db,
        delivery_id=delivery_id,
        profile_id=profile.id,
        published_only=True,
        lock=True,
    )
    if delivery.effects_applied_at is not None:
        return

    application_lifecycle_needed = False
    for item in delivery.items:
        if item.kind == "merchant_offer":
            source = await db.get(MerchantProcessingOffer, item.source_id, with_for_update=True)
            if source is not None and source.terms_version == item.source_version:
                if source.status not in merchant_processing.RESPONDED_STATUSES:
                    source.status = merchant_processing.STATUS_SENT
                    source.sent_at = delivery.sent_at
                    source.sent_by_user_id = user.id
                    await merchant_processing.sync_intake_state(db, source)
        elif item.kind == "application_term_sheet":
            application_lifecycle_needed = True
        if item.kind == "evidence_file":
            continue
        older = list(
            (
                await db.execute(
                    select(ApplicationOfferDeliveryItem)
                    .join(ApplicationOfferDelivery)
                    .where(
                        ApplicationOfferDelivery.profile_id == profile.id,
                        ApplicationOfferDelivery.id != delivery.id,
                        ApplicationOfferDelivery.published_at.is_not(None),
                        ApplicationOfferDelivery.sent_at < delivery.sent_at,
                        ApplicationOfferDeliveryItem.kind == item.kind,
                        ApplicationOfferDeliveryItem.decision_status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
        for old in older:
            old.decision_status = "superseded"
    if application_lifecycle_needed:
        await apply_terms_issued_lifecycle(db, profile, user)

    await profiles.log_profile_action(
        db,
        profile,
        user,
        "offer_delivery.sent",
        f"Emailed and published a combined offer package with {len([item for item in delivery.items if item.kind != 'evidence_file'])} offer item(s)",
        target_type="offer_delivery",
        target_id=delivery.id,
        metadata={
            "provider_message_id": delivery.provider_message_id,
            "expires_at": delivery.expires_at.isoformat() if delivery.expires_at else None,
            "pdf_sha256": [item.sha256 for item in delivery.items],
        },
    )
    delivery.effects_applied_at = datetime.now(UTC)
    await db.commit()


async def _finalize_delivery_success(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    delivery: ApplicationOfferDelivery,
    message,
    user: User,
) -> None:
    accepted_at = datetime.now(UTC)
    await _persist_provider_acceptance(
        db,
        delivery=delivery,
        message=message,
        accepted_at=accepted_at,
    )
    try:
        await _apply_delivery_effects(
            db,
            profile=profile,
            delivery_id=delivery.id,
            user=user,
        )
    except Exception:  # delivery is already sent/published; reconcile on next access
        await db.rollback()
        log.exception(
            "offer delivery %s was accepted by the provider but ancillary effects need reconciliation",
            delivery.id,
        )


async def _retry_failed_delivery(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    delivery: ApplicationOfferDelivery,
    user: User,
) -> OfferDeliveryRead:
    await communications._enforce_direct_client_contact(db, profile)
    await _revalidate_saved_recipients(db, profile, delivery)
    offer_items = [item for item in delivery.items if item.kind != "evidence_file"]
    refs = [_ref_for_snapshot(item) for item in offer_items]
    resolved = await offers.resolve_offers(
        db,
        profile,
        _ordered_offer_refs([ref for ref in refs if ref is not None]),
        lock=True,
    )
    resolved_by_key = {item.key: item for item in resolved}
    if set(resolved_by_key) != {item.item_key for item in offer_items}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "One or more offers changed after the failed send. Create a new draft and delivery key.",
        )
    link = await profile_routes._profile_room_link(db, profile)
    if (
        link.status != "active"
        or (link.expires_at and link.expires_at <= datetime.now(UTC))
        or not link.passcode_hash
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The PIN-protected client secure room must be active before retrying.",
        )
    delivery.room_link_id = link.id
    delivery.room_token_hash = hashlib.sha256(link.token.encode("utf-8")).hexdigest()
    delivery.access_passcode_hash = link.passcode_hash
    prepared_at = datetime.now(UTC)
    item_links: dict[str, str] = {}
    for item in offer_items:
        source = resolved_by_key[item.item_key]
        expiry = offers.item_offer_expiry(source, prepared_at)
        if expiry <= prepared_at:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An offer in this package has expired. Create a new terms version before retrying.",
            )
        query = urlencode({"tab": "inbox", "delivery": str(delivery.id), "item": str(item.id)})
        response_url = profile_routes._room_url(link, query=query)
        item_links[item.item_key] = response_url
        base_pdf = await offers.render_base_pdf(db, profile, source)
        pdf = await asyncio.to_thread(
            offers.append_response_page,
            base_pdf,
            title=source.title,
            response_url=response_url,
            expires_at=expiry,
            disclaimer=(
                merchant_processing.DISCLAIMER_TEXT
                if source.kind == "merchant_offer"
                else offers.LOAN_DISCLAIMER
            ),
        )
        item.expires_at = expiry
        item.document_bytes = pdf
        item.size_bytes = len(pdf)
        item.sha256 = hashlib.sha256(pdf).hexdigest()
        item.decision_status = "pending"
        item.responded_at = None
        item.responded_name = None
        item.response_reason = None
        item.response_channel = None
        item.response_ip = None
        item.response_user_agent = None
        item.response_user_id = None
        item.response_attestation = None
        item.storage_key = await offers.archive_snapshot(
            profile_id=profile.id,
            delivery_id=delivery.id,
            item_id=item.id,
            filename=item.file_name,
            data=pdf,
        )
    delivery.expires_at = min(item.expires_at for item in offer_items if item.expires_at)
    delivery.body = offers.compose_body(
        delivery.personal_message,
        resolved,
        item_links=item_links,
        expires_at=delivery.expires_at,
        item_expiries={item.item_key: item.expires_at for item in offer_items},
    )
    delivery.body_html = offers.compose_body_html(
        delivery.personal_message,
        resolved,
        item_links=item_links,
        expires_at=delivery.expires_at,
        item_expiries={item.item_key: item.expires_at for item in offer_items},
    )
    attachments = [
        (item.file_name, await offers.snapshot_bytes(item), item.content_type)
        for item in delivery.items
    ]
    thread = await db.get(DealerRepInboxThread, delivery.email_thread_id)
    if thread is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The saved email thread is unavailable; use a new delivery key.",
        )
    delivery.status = "sending"
    delivery.provider_detail = None
    delivery.provider_handoff_started_at = datetime.now(UTC)
    await db.commit()
    await _lock_sources_for_provider_handoff(
        db,
        profile=profile,
        refs=[ref for ref in refs if ref is not None],
        delivery=delivery,
    )
    message = await communications._send_thread_email(
        db,
        profile=profile,
        thread=thread,
        user=user,
        to_email=delivery.recipient_emails[0],
        cc_emails=list(delivery.cc_emails or []),
        subject=delivery.subject,
        body=delivery.body,
        body_html=delivery.body_html,
        attachments=attachments,
        headers=_provider_headers(delivery),
    )
    if message.delivery_status != "sent":
        if _provider_outcome_is_uncertain(message):
            await _persist_uncertain_provider_outcome(
                db,
                delivery=delivery,
                message=message,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The provider outcome is uncertain. Do not resend; reconcile this delivery after the safety timeout.",
            )
        delivery.status = "failed"
        delivery.provider_detail = (
            message.provider_error or "Email provider did not accept the message."
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The email provider did not accept the offer package."
        )
    await _finalize_delivery_success(
        db,
        profile=profile,
        delivery=delivery,
        message=message,
        user=user,
    )
    await db.refresh(delivery)
    delivery = await _load_delivery(db, delivery_id=delivery.id, profile_id=profile.id)
    return offers.delivery_read(delivery, base_url=_delivery_base(profile.id))


async def _resume_existing_delivery(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    delivery: ApplicationOfferDelivery,
    request_fingerprint: str,
    user: User,
) -> OfferDeliveryRead:
    if delivery.profile_id != profile.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This delivery key belongs to another application file.",
        )
    if delivery.request_fingerprint != request_fingerprint:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This delivery key was already used for a different recipient, message, or package.",
        )
    if delivery.status == "failed":
        claimed = await _load_delivery(
            db,
            delivery_id=delivery.id,
            profile_id=profile.id,
            lock=True,
        )
        if claimed.status != "failed":
            return await _resume_existing_delivery(
                db,
                profile=profile,
                delivery=claimed,
                request_fingerprint=request_fingerprint,
                user=user,
            )
        claimed.status = "sending"
        claimed.provider_detail = "Retry claimed; provider handoff has not started."
        claimed.provider_handoff_started_at = None
        await db.commit()
        try:
            return await _retry_failed_delivery(
                db,
                profile=profile,
                delivery=claimed,
                user=user,
            )
        except Exception:
            await db.rollback()
            retry_state = await _load_delivery(
                db,
                delivery_id=claimed.id,
                profile_id=profile.id,
                lock=True,
            )
            if (
                retry_state.status == "sending"
                and not retry_state.provider_message_id
                and retry_state.provider_handoff_started_at is None
            ):
                retry_state.status = "failed"
                retry_state.provider_detail = "Retry stopped before provider acceptance."
                await db.commit()
            raise
    if delivery.status == "sending":
        if not delivery.provider_message_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This offer delivery is already in progress. If it remains here, verify the provider handoff before retrying so the client is not emailed twice.",
            )
        # A provider result was durably recorded but the prior request stopped
        # before publishing. Complete that transaction without another send.
        delivery.status = "sent"
        delivery.sent_at = delivery.sent_at or datetime.now(UTC)
        delivery.published_at = delivery.published_at or delivery.sent_at
        await db.commit()
    if delivery.published_at is not None and delivery.effects_applied_at is None:
        try:
            await _apply_delivery_effects(
                db,
                profile=profile,
                delivery_id=delivery.id,
                user=user,
            )
        except Exception:
            await db.rollback()
            log.exception("could not reconcile offer delivery effects for %s", delivery.id)
    refreshed = await _load_delivery(
        db,
        delivery_id=delivery.id,
        profile_id=profile.id,
    )
    return offers.delivery_read(refreshed, base_url=_delivery_base(profile.id))


@router.post(
    "/{profile_id}/offer-deliveries",
    response_model=OfferDeliveryRead,
    status_code=status.HTTP_201_CREATED,
)
async def send_combined_offer_delivery(
    profile_id: UUID,
    payload: OfferDeliveryCreate,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveryRead:
    profile = await _operator_profile(db, profile_id, user)
    await communications._enforce_direct_client_contact(db, profile)
    request_fingerprint = _request_fingerprint(payload)
    existing = (
        await db.execute(
            select(ApplicationOfferDelivery)
            .where(ApplicationOfferDelivery.idempotency_key == payload.idempotency_key)
            .options(selectinload(ApplicationOfferDelivery.items))
        )
    ).scalar_one_or_none()
    if existing is not None:
        return await _resume_existing_delivery(
            db,
            profile=profile,
            delivery=existing,
            request_fingerprint=request_fingerprint,
            user=user,
        )

    to, cc_emails = await _verified_recipients(db, profile, payload)
    await profile_routes._require_training_live_action(
        db,
        profile=profile,
        user=user,
        request=request,
        action="Send combined client offer package",
        provider="Gmail / SES",
        recipient=to.email,
        effect="Email immutable loan and/or merchant offer PDFs and publish them to the client inbox",
    )
    resolved = await offers.resolve_offers(
        db,
        profile,
        _ordered_offer_refs(payload.items),
        lock=True,
    )
    current_fingerprint = offers.draft_fingerprint(resolved)
    if payload.draft_fingerprint and payload.draft_fingerprint != current_fingerprint:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The selected offers changed after the draft was created. Regenerate the email before sending.",
        )

    # Issuance freezes application-term dates. Do this before calculating the
    # delivery deadline so a source expiration shorter than 48 hours wins.
    from app.models.application_terms import ApplicationTermSheet
    from app.services import application_terms as application_terms_service

    for resolved_item in resolved:
        if resolved_item.kind == "application_term_sheet":
            source = resolved_item.source
            assert isinstance(source, ApplicationTermSheet)
            application_terms_service.issue(source)
            resolved_item.source_expires_at = offers._application_expiry(source)

    link = await profile_routes._profile_room_link(db, profile)
    if link.status != "active" or (link.expires_at and link.expires_at <= datetime.now(UTC)):
        raise HTTPException(status.HTTP_409_CONFLICT, "The client secure room is not active.")
    if not link.passcode_hash:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Set a PIN on the client secure room before sending an offer package.",
        )
    prepared_at = datetime.now(UTC)
    item_expiries = [offers.item_offer_expiry(item, prepared_at) for item in resolved]
    if any(value <= prepared_at for value in item_expiries):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "One of the selected offers has already expired. Save or issue a new version before sending.",
        )
    delivery_id = uuid4()
    delivery = ApplicationOfferDelivery(
        id=delivery_id,
        profile_id=profile.id,
        idempotency_key=payload.idempotency_key,
        request_fingerprint=request_fingerprint,
        room_link_id=link.id,
        room_token_hash=hashlib.sha256(link.token.encode("utf-8")).hexdigest(),
        access_passcode_hash=link.passcode_hash,
        status="sending",
        to_contact_id=payload.to_contact_id,
        cc_contact_ids=list(dict.fromkeys(payload.cc_contact_ids)),
        recipient_emails=[to.email, *cc_emails],
        cc_emails=cc_emails,
        subject=payload.subject.strip(),
        personal_message=payload.personal_message.strip(),
        body="Preparing immutable offer package",
        body_html="<p>Preparing immutable offer package</p>",
        draft_fingerprint=current_fingerprint,
        provider_correlation_id=_provider_correlation_id(delivery_id),
        sent_by_user_id=user.id,
        expires_at=min(item_expiries),
    )
    db.add(delivery)
    try:
        # Claim the key before rendering/uploading PDFs. Concurrent requests
        # then converge on the one saved delivery instead of sending twice.
        await db.flush()
    except IntegrityError:
        await db.rollback()
        winner = (
            await db.execute(
                select(ApplicationOfferDelivery)
                .where(ApplicationOfferDelivery.idempotency_key == payload.idempotency_key)
                .options(selectinload(ApplicationOfferDelivery.items))
            )
        ).scalar_one_or_none()
        if winner is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Another request is creating this offer delivery. Retry with the same delivery key.",
            ) from None
        return await _resume_existing_delivery(
            db,
            profile=profile,
            delivery=winner,
            request_fingerprint=request_fingerprint,
            user=user,
        )
    item_rows: list[ApplicationOfferDeliveryItem] = []
    item_links: dict[str, str] = {}
    for resolved_item, expires_at in zip(resolved, item_expiries, strict=True):
        item_id = uuid4()
        query = urlencode({"tab": "inbox", "delivery": str(delivery.id), "item": str(item_id)})
        response_url = profile_routes._room_url(link, query=query)
        item_links[resolved_item.key] = response_url
        base_pdf = await offers.render_base_pdf(db, profile, resolved_item)
        pdf = await asyncio.to_thread(
            offers.append_response_page,
            base_pdf,
            title=resolved_item.title,
            response_url=response_url,
            expires_at=expires_at,
            disclaimer=(
                merchant_processing.DISCLAIMER_TEXT
                if resolved_item.kind == "merchant_offer"
                else offers.LOAN_DISCLAIMER
            ),
        )
        digest = hashlib.sha256(pdf).hexdigest()
        storage_key = await offers.archive_snapshot(
            profile_id=profile.id,
            delivery_id=delivery.id,
            item_id=item_id,
            filename=resolved_item.file_name,
            data=pdf,
        )
        row = ApplicationOfferDeliveryItem(
            id=item_id,
            delivery_id=delivery.id,
            item_key=resolved_item.key,
            kind=resolved_item.kind,
            source_id=resolved_item.source_id,
            source_version=resolved_item.version,
            label=resolved_item.label,
            title=resolved_item.title,
            canonical_summary={"lines": resolved_item.lines},
            file_name=resolved_item.file_name,
            content_type="application/pdf",
            size_bytes=len(pdf),
            sha256=digest,
            storage_key=storage_key,
            document_bytes=pdf,
            expires_at=expires_at,
            decision_status="pending",
        )
        db.add(row)
        item_rows.append(row)

    for ref in payload.evidence_attachments:
        file, data = await offers.evidence_snapshot(db, profile=profile, file_id=ref.file_id)
        if len(data) > communications.MAX_EMAIL_ATTACHMENT_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{file.file_name} is too large to email."
            )
        key = f"evidence_file:{file.id}"
        digest = hashlib.sha256(data).hexdigest()
        row = ApplicationOfferDeliveryItem(
            id=uuid4(),
            delivery_id=delivery.id,
            item_key=key,
            kind="evidence_file",
            source_id=file.id,
            source_version=None,
            label=file.file_name,
            title=file.file_name,
            canonical_summary={},
            file_name=communications._safe_attachment_filename(file.file_name),
            content_type=file.content_type or "application/octet-stream",
            size_bytes=len(data),
            sha256=digest,
            storage_key=file.s3_key,
            document_bytes=data,
            expires_at=None,
            decision_status="not_applicable",
        )
        db.add(row)
        item_rows.append(row)
    if (
        sum(item.size_bytes for item in item_rows)
        > communications.MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES
    ):
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "The selected files are too large to send in one email.",
        )

    delivery.body = offers.compose_body(
        payload.personal_message,
        resolved,
        item_links=item_links,
        expires_at=delivery.expires_at,
        item_expiries={
            item.key: expiry for item, expiry in zip(resolved, item_expiries, strict=True)
        },
    )
    delivery.body_html = offers.compose_body_html(
        payload.personal_message,
        resolved,
        item_links=item_links,
        expires_at=delivery.expires_at,
        item_expiries={
            item.key: expiry for item, expiry in zip(resolved, item_expiries, strict=True)
        },
    )
    thread = DealerRepInboxThread(
        owner_user_id=user.id,
        contact_id=None,
        dealer_id=profile.dealer_id,
        profile_id=profile.id,
        participant_emails=[to.email, *cc_emails],
        subject=delivery.subject,
        subject_key=communications._subject_key(delivery.subject),
        channel="email",
        source="combined_offer",
        last_message_at=prepared_at,
    )
    db.add(thread)
    await db.flush()
    delivery.email_thread_id = thread.id
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "offer_delivery.queued",
        f"Queued a combined offer package with {len(resolved)} offer item(s)",
        target_type="offer_delivery",
        target_id=delivery.id,
        metadata={
            "idempotency_key": str(payload.idempotency_key),
            "to": to.email,
            "cc": cc_emails,
            "items": [item.item_key for item in item_rows],
            "draft_fingerprint": current_fingerprint,
        },
    )
    # Own the idempotency key and immutable snapshots before the provider call.
    delivery.provider_handoff_started_at = datetime.now(UTC)
    await db.commit()
    await _lock_sources_for_provider_handoff(
        db,
        profile=profile,
        refs=payload.items,
        delivery=delivery,
    )
    attachments = [
        (item.file_name, bytes(item.document_bytes), item.content_type) for item in item_rows
    ]
    message = await communications._send_thread_email(
        db,
        profile=profile,
        thread=thread,
        user=user,
        to_email=to.email,
        cc_emails=cc_emails,
        subject=delivery.subject,
        body=delivery.body,
        body_html=delivery.body_html,
        attachments=attachments,
        headers=_provider_headers(delivery),
    )
    if message.delivery_status != "sent":
        if _provider_outcome_is_uncertain(message):
            await _persist_uncertain_provider_outcome(
                db,
                delivery=delivery,
                message=message,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The provider outcome is uncertain. Do not resend; reconcile this delivery after the safety timeout.",
            )
        delivery.status = "failed"
        delivery.provider = message.provider
        delivery.provider_detail = (
            message.provider_error or "Email provider did not accept the message."
        )
        delivery.message_send_id = message.message_send_id
        await profiles.log_profile_action(
            db,
            profile,
            user,
            "offer_delivery.failed",
            "The combined offer package was saved but the email provider did not accept it",
            target_type="offer_delivery",
            target_id=delivery.id,
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The email provider did not accept the offer package. The exact package was saved for retry.",
        )

    await _finalize_delivery_success(
        db,
        profile=profile,
        delivery=delivery,
        message=message,
        user=user,
    )
    delivery = await _load_delivery(db, delivery_id=delivery.id, profile_id=profile.id)
    return offers.delivery_read(delivery, base_url=_delivery_base(profile.id))


@router.get("/{profile_id}/offer-deliveries", response_model=OfferDeliveriesRead)
async def list_operator_offer_deliveries(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveriesRead:
    profile = await _operator_profile(db, profile_id, user)
    pending_effect_ids = list(
        (
            await db.execute(
                select(ApplicationOfferDelivery.id).where(
                    ApplicationOfferDelivery.profile_id == profile.id,
                    ApplicationOfferDelivery.published_at.is_not(None),
                    ApplicationOfferDelivery.effects_applied_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for pending_id in pending_effect_ids:
        try:
            await _apply_delivery_effects(
                db,
                profile=profile,
                delivery_id=pending_id,
                user=user,
            )
        except Exception:
            await db.rollback()
            log.exception("could not reconcile offer delivery effects for %s", pending_id)
    return await _list_profile_deliveries(
        db, profile.id, published_only=False, base_url=_delivery_base(profile.id)
    )


@router.get("/{profile_id}/offer-deliveries/{delivery_id}", response_model=OfferDeliveryRead)
async def get_operator_offer_delivery(
    profile_id: UUID,
    delivery_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveryRead:
    profile = await _operator_profile(db, profile_id, user)
    delivery = await _load_delivery(db, delivery_id=delivery_id, profile_id=profile.id)
    if delivery.published_at is not None and delivery.effects_applied_at is None:
        try:
            await _apply_delivery_effects(
                db,
                profile=profile,
                delivery_id=delivery.id,
                user=user,
            )
        except Exception:
            await db.rollback()
            log.exception("could not reconcile offer delivery effects for %s", delivery.id)
        delivery = await _load_delivery(
            db,
            delivery_id=delivery_id,
            profile_id=profile.id,
        )
    offers.refresh_delivery_status(delivery)
    await db.commit()
    return offers.delivery_read(delivery, base_url=_delivery_base(profile.id))


@router.get(
    "/{profile_id}/offer-deliveries/{delivery_id}/reconciliation",
    response_model=OfferDeliveryReconciliationRead,
)
async def get_offer_delivery_reconciliation(
    profile_id: UUID,
    delivery_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveryReconciliationRead:
    profile = await _operator_profile(db, profile_id, user)
    delivery = await _load_delivery(db, delivery_id=delivery_id, profile_id=profile.id)
    return _reconciliation_read(delivery)


@router.post(
    "/{profile_id}/offer-deliveries/{delivery_id}/reconciliation",
    response_model=OfferDeliveryReconciliationRead,
)
async def reconcile_offer_delivery(
    profile_id: UUID,
    delivery_id: UUID,
    payload: OfferDeliveryReconciliationRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveryReconciliationRead:
    """Resolve a provider outcome that was lost after transport handoff.

    The operator must verify the durable RFC correlation id against Gmail/SES
    before making either attestation. Until then the normal idempotent retry
    remains blocked, preventing a duplicate client email.
    """

    profile = await _operator_profile(db, profile_id, user)
    delivery = await _load_delivery(
        db,
        delivery_id=delivery_id,
        profile_id=profile.id,
        lock=True,
    )
    if delivery.status != "sending" or delivery.published_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only a delivery with an uncertain provider outcome can be reconciled.",
        )

    now = datetime.now(UTC)
    handoff_started_at = delivery.provider_handoff_started_at
    if (
        handoff_started_at is None
        or now
        < handoff_started_at.astimezone(UTC)
        + timedelta(minutes=PROVIDER_HANDOFF_STALE_MINUTES)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The provider handoff may still be active. Reconciliation is available only after the safety timeout.",
        )
    if payload.outcome == "confirmed_not_sent":
        if delivery.provider_message_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A provider message ID is already recorded; this delivery cannot be marked unsent.",
            )
        delivery.status = "failed"
        delivery.provider_detail = "Operator confirmed that no provider send occurred."
        delivery.reconciled_at = now
        delivery.reconciled_by_user_id = user.id
        delivery.reconciliation_outcome = payload.outcome
        delivery.reconciliation_attestation = payload.attestation.strip()
        await profiles.log_profile_action(
            db,
            profile,
            user,
            "offer_delivery.reconciled_not_sent",
            "Confirmed that the uncertain combined-offer handoff did not send",
            target_type="offer_delivery",
            target_id=delivery.id,
            metadata={
                "provider_correlation_id": delivery.provider_correlation_id,
                "attestation": delivery.reconciliation_attestation,
            },
        )
        await db.commit()
        return _reconciliation_read(delivery)

    assert payload.accepted_at is not None
    assert payload.provider is not None
    assert payload.provider_message_id is not None
    accepted_at = payload.accepted_at.astimezone(UTC)
    if accepted_at > now + timedelta(minutes=5):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The provider acceptance time cannot be in the future.",
        )
    created_at = delivery.created_at
    if created_at and accepted_at < created_at.astimezone(UTC) - timedelta(minutes=5):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The provider acceptance time predates this delivery.",
        )
    duplicate = (
        await db.execute(
            select(ApplicationOfferDelivery.id).where(
                ApplicationOfferDelivery.id != delivery.id,
                ApplicationOfferDelivery.provider == payload.provider,
                ApplicationOfferDelivery.provider_message_id == payload.provider_message_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That provider message ID is already associated with another delivery.",
        )

    delivery.status = "sent"
    delivery.sent_at = accepted_at
    delivery.published_at = now
    delivery.provider = payload.provider
    delivery.provider_message_id = payload.provider_message_id
    delivery.provider_detail = "Provider acceptance recorded by audited operator reconciliation."
    delivery.reconciled_at = now
    delivery.reconciled_by_user_id = user.id
    delivery.reconciliation_outcome = payload.outcome
    delivery.reconciliation_attestation = payload.attestation.strip()
    # Publish the provider truth first. Ancillary lifecycle effects are
    # idempotent and reconcile on subsequent operator reads if they fail.
    await db.commit()

    try:
        await profiles.log_profile_action(
            db,
            profile,
            user,
            "offer_delivery.reconciled_provider_accepted",
            "Reconciled an uncertain combined-offer handoff as provider accepted",
            target_type="offer_delivery",
            target_id=delivery.id,
            metadata={
                "provider": payload.provider,
                "provider_message_id": payload.provider_message_id,
                "provider_correlation_id": delivery.provider_correlation_id,
                "accepted_at": accepted_at.isoformat(),
                "attestation": delivery.reconciliation_attestation,
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("could not append reconciliation audit event for %s", delivery.id)
    try:
        await _apply_delivery_effects(
            db,
            profile=profile,
            delivery_id=delivery.id,
            user=user,
        )
    except Exception:
        await db.rollback()
        log.exception("could not reconcile offer delivery effects for %s", delivery.id)
    delivery = await _load_delivery(db, delivery_id=delivery.id, profile_id=profile.id)
    return _reconciliation_read(delivery)


@router.get("/{profile_id}/offer-deliveries/{delivery_id}/items/{item_id}/document")
async def get_operator_offer_document(
    profile_id: UUID,
    delivery_id: UUID,
    item_id: UUID,
    user: CurrentUser,
    disposition: str = Query(default="inline"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    profile = await _operator_profile(db, profile_id, user)
    delivery = await _load_delivery(db, delivery_id=delivery_id, profile_id=profile.id)
    item = next((value for value in delivery.items if value.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer document not found.")
    return _stored_document_response(await offers.snapshot_bytes(item), item, disposition)


async def _notify_response(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    delivery: ApplicationOfferDelivery,
    item: ApplicationOfferDeliveryItem,
    actor: User | None,
) -> None:
    recipients = set(
        (
            await db.execute(
                select(User.id).where(
                    User.role.in_([Role.SUPER_ADMIN, Role.LOAN_EXEC]),
                    User.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    await notifications.notify_users(
        db,
        recipient_ids=recipients,
        event_type="offer.response_recorded",
        title=f"Client {item.decision_status.replace('_', ' ')} {item.title.lower()}",
        body=f"{item.responded_name or 'The client'} responded to {item.label}.",
        category="underwriting",
        priority="high",
        target_type="offer_delivery",
        target_id=str(delivery.id),
        deep_link=f"/admin/ai-underwriter-leads?profile={profile.id}&tab=underwriting",
        actor_user_id=actor.id if actor else None,
        push=True,
        email=False,
    )
    await profiles.log_profile_action(
        db,
        profile,
        actor,
        "offer_delivery.response_recorded",
        f"{item.responded_name or 'Client'} {item.decision_status} {item.label} via {item.response_channel}",
        target_type="offer_delivery_item",
        target_id=item.id,
        metadata={
            "delivery_id": str(delivery.id),
            "source_version": item.source_version,
            "sha256": item.sha256,
            "response": item.decision_status,
            "channel": item.response_channel,
            "responded_at": item.responded_at.isoformat() if item.responded_at else None,
        },
    )


@router.post(
    "/{profile_id}/offer-deliveries/{delivery_id}/items/{item_id}/manual-response",
    response_model=OfferResponseResult,
)
async def record_manual_offer_response(
    profile_id: UUID,
    delivery_id: UUID,
    item_id: UUID,
    payload: ManualOfferResponse,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferResponseResult:
    profile = await _operator_profile(db, profile_id, user)
    delivery = await _load_delivery(
        db, delivery_id=delivery_id, profile_id=profile.id, published_only=True, lock=True
    )
    item = next((value for value in delivery.items if value.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer item not found.")
    changed = await offers.record_response(
        db,
        profile=profile,
        delivery=delivery,
        item=item,
        response=payload.response,
        responder_name=payload.responder_name,
        reason=payload.reason,
        channel=payload.channel,
        responded_at=payload.received_at,
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
        user_id=user.id,
        attestation=payload.attestation,
    )
    if changed:
        await _notify_response(db, profile=profile, delivery=delivery, item=item, actor=user)
    await db.commit()
    read = offers.delivery_read(delivery, base_url=_delivery_base(profile.id))
    return OfferResponseResult(
        delivery=read, item=next(value for value in read.items if value.id == item.id)
    )


async def _public_profile(
    db: AsyncSession, token: str, passcode: str, request: Request
) -> tuple[ApplicationProfile, set[UUID] | None, set[UUID]]:
    """Authorize the live room or an immutable delivery credential snapshot.

    A room PIN/status may change after an email is sent. The old credential is
    intentionally limited to deliveries stamped with that exact token/PIN; it
    cannot reopen the rest of the mutable document room.
    """

    attempt_scope = _ip(request) or "unknown"
    link = (
        await db.execute(select(BucketUploadLink).where(BucketUploadLink.token == token))
    ).scalar_one_or_none()
    live_link = bool(
        link is not None
        and link.status == "active"
        and not (link.expires_at and link.expires_at <= datetime.now(UTC))
    )
    current_pin_valid = bool(
        link is not None
        and live_link
        and _verify_passcode(
            passcode,
            link.passcode_hash,
            attempt_scope=attempt_scope,
        )
    )
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if current_pin_valid:
        profile = (
            await db.execute(
                select(ApplicationProfile).where(
                    ApplicationProfile.primary_bucket_id == link.bucket_id
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Application file not found.")
        uncertain_rows = list(
            (
                await db.execute(
                    select(ApplicationOfferDelivery).where(
                        ApplicationOfferDelivery.profile_id == profile.id,
                        ApplicationOfferDelivery.room_token_hash == token_hash,
                        ApplicationOfferDelivery.status == "sending",
                        ApplicationOfferDelivery.published_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        uncertain = {
            row.id
            for row in uncertain_rows
            if row.access_passcode_hash == link.passcode_hash
        }
        return profile, None, uncertain

    candidates = list(
        (
            await db.execute(
                select(ApplicationOfferDelivery).where(
                    ApplicationOfferDelivery.room_token_hash == token_hash,
                    ApplicationOfferDelivery.status != "failed",
                    or_(
                        ApplicationOfferDelivery.published_at.is_not(None),
                        ApplicationOfferDelivery.status == "sending",
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    checked_hashes = (
        {link.passcode_hash: current_pin_valid}
        if live_link and link is not None and link.passcode_hash
        else {}
    )
    for digest in {row.access_passcode_hash for row in candidates}:
        if digest not in checked_hashes:
            checked_hashes[digest] = _verify_passcode(
                passcode,
                digest,
                attempt_scope=attempt_scope,
            )
    valid_hashes = {digest for digest, valid in checked_hashes.items() if valid}
    allowed = {row.id for row in candidates if row.access_passcode_hash in valid_hashes}
    if not allowed:
        if link is None and not candidates:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Secure room not found.")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid access code.")
    profile_ids = {row.profile_id for row in candidates if row.id in allowed}
    if len(profile_ids) != 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application file not found.")
    profile = await db.get(ApplicationProfile, next(iter(profile_ids)))
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application file not found.")
    uncertain = {
        row.id
        for row in candidates
        if row.id in allowed and row.status == "sending" and row.published_at is None
    }
    return profile, allowed, uncertain


def _public_base(token: str) -> str:
    return f"/api/v1/application-profiles/public/room/{token}/offer-deliveries"


@router.post("/public/room/{token}/offer-deliveries", response_model=OfferDeliveriesRead)
async def list_public_offer_deliveries(
    token: str,
    payload: PublicOfferDeliveryAccess,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveriesRead:
    profile, allowed, uncertain = await _public_profile(db, token, payload.passcode, request)
    result = await _list_profile_deliveries(
        db,
        profile.id,
        published_only=True,
        base_url=_public_base(token),
        allowed_delivery_ids=allowed,
        uncertain_delivery_ids=uncertain,
    )
    now = datetime.now(UTC)
    for row in (
        (
            await db.execute(
                select(ApplicationOfferDelivery).where(
                    ApplicationOfferDelivery.profile_id == profile.id,
                    ApplicationOfferDelivery.published_at.is_not(None),
                    ApplicationOfferDelivery.client_seen_at.is_(None),
                    *([ApplicationOfferDelivery.id.in_(allowed)] if allowed is not None else []),
                )
            )
        )
        .scalars()
        .all()
    ):
        row.client_seen_at = now
    await db.commit()
    return result


@router.post("/public/room/{token}/offer-deliveries/{delivery_id}/items/{item_id}/document")
async def get_public_offer_document(
    token: str,
    delivery_id: UUID,
    item_id: UUID,
    payload: PublicOfferDeliveryAccess,
    request: Request,
    disposition: str = Query(default="inline"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    profile, allowed, uncertain = await _public_profile(db, token, payload.passcode, request)
    if allowed is not None and delivery_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer delivery not found.")
    delivery = await _load_delivery(db, delivery_id=delivery_id, profile_id=profile.id)
    published = bool(
        delivery.published_at is not None and delivery.status not in {"sending", "failed"}
    )
    if not published and delivery_id not in uncertain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer delivery not found.")
    item = next((value for value in delivery.items if value.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer document not found.")
    return _stored_document_response(await offers.snapshot_bytes(item), item, disposition)


@router.post(
    "/public/room/{token}/offer-deliveries/{delivery_id}/items/{item_id}/respond",
    response_model=OfferResponseResult,
)
async def respond_to_public_offer(
    token: str,
    delivery_id: UUID,
    item_id: UUID,
    payload: PublicOfferResponse,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> OfferResponseResult:
    profile, allowed, _uncertain = await _public_profile(db, token, payload.passcode, request)
    if allowed is not None and delivery_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer delivery not found.")
    delivery = await _load_delivery(
        db, delivery_id=delivery_id, profile_id=profile.id, published_only=True, lock=True
    )
    item = next((value for value in delivery.items if value.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer item not found.")
    if (
        payload.response == "accepted"
        and item.kind in {"production_term_sheet", "application_term_sheet"}
        and not payload.acknowledged_non_binding
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Acknowledge that indicative loan terms are non-binding before accepting.",
        )
    changed = await offers.record_response(
        db,
        profile=profile,
        delivery=delivery,
        item=item,
        response=payload.response,
        responder_name=payload.responder_name,
        reason=payload.reason,
        channel="secure_room",
        responded_at=datetime.now(UTC),
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
        user_id=None,
        attestation=(
            "Client acknowledged the non-binding indicative-terms disclosure."
            if payload.acknowledged_non_binding
            else None
        ),
    )
    if changed:
        await _notify_response(db, profile=profile, delivery=delivery, item=item, actor=None)
    await db.commit()
    read = offers.delivery_read(delivery, base_url=_public_base(token))
    return OfferResponseResult(
        delivery=read, item=next(value for value in read.items if value.id == item.id)
    )


def _client_base() -> str:
    return "/api/v1/application-profiles/client/offer-deliveries"


def _require_client(user: User) -> UUID:
    if user.role != Role.CLIENT or user.client is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client account required.")
    return user.client.id


async def _client_delivery(
    db: AsyncSession, user: User, delivery_id: UUID, *, lock: bool = False
) -> tuple[ApplicationProfile, ApplicationOfferDelivery]:
    client_id = _require_client(user)
    stmt = (
        select(ApplicationOfferDelivery, ApplicationProfile)
        .join(ApplicationProfile, ApplicationProfile.id == ApplicationOfferDelivery.profile_id)
        .where(
            ApplicationOfferDelivery.id == delivery_id,
            ApplicationOfferDelivery.published_at.is_not(None),
            ApplicationOfferDelivery.status.notin_(["sending", "failed"]),
            ApplicationProfile.client_id == client_id,
        )
        .options(selectinload(ApplicationOfferDelivery.items))
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer delivery not found.")
    return row[1], row[0]


@client_router.get("/offer-deliveries", response_model=OfferDeliveriesRead)
async def list_authenticated_client_offer_deliveries(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveriesRead:
    client_id = _require_client(user)
    rows = list(
        (
            await db.execute(
                select(ApplicationOfferDelivery)
                .join(ApplicationProfile)
                .where(
                    ApplicationProfile.client_id == client_id,
                    ApplicationOfferDelivery.published_at.is_not(None),
                    ApplicationOfferDelivery.status.notin_(["sending", "failed"]),
                )
                .options(selectinload(ApplicationOfferDelivery.items))
                .order_by(ApplicationOfferDelivery.sent_at.desc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    changed = False
    for row in rows:
        previous = row.status
        offers.refresh_delivery_status(row)
        changed = changed or previous != row.status
    if changed:
        await db.commit()
    return OfferDeliveriesRead(
        deliveries=[offers.delivery_read(row, base_url=_client_base()) for row in rows]
    )


@client_router.post("/offer-deliveries/{delivery_id}/seen", response_model=OfferDeliveryRead)
async def mark_authenticated_client_offer_seen(
    delivery_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferDeliveryRead:
    _, delivery = await _client_delivery(db, user, delivery_id, lock=True)
    delivery.client_seen_at = delivery.client_seen_at or datetime.now(UTC)
    offers.refresh_delivery_status(delivery)
    await db.commit()
    return offers.delivery_read(delivery, base_url=_client_base())


@client_router.get("/offer-deliveries/{delivery_id}/items/{item_id}/document")
async def get_authenticated_client_offer_document(
    delivery_id: UUID,
    item_id: UUID,
    user: CurrentUser,
    disposition: str = Query(default="inline"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _, delivery = await _client_delivery(db, user, delivery_id)
    item = next((value for value in delivery.items if value.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer document not found.")
    return _stored_document_response(await offers.snapshot_bytes(item), item, disposition)


@client_router.post(
    "/offer-deliveries/{delivery_id}/items/{item_id}/respond",
    response_model=OfferResponseResult,
)
async def respond_to_authenticated_client_offer(
    delivery_id: UUID,
    item_id: UUID,
    payload: AuthenticatedOfferResponse,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OfferResponseResult:
    profile, delivery = await _client_delivery(db, user, delivery_id, lock=True)
    item = next((value for value in delivery.items if value.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer item not found.")
    if (
        payload.response == "accepted"
        and item.kind in {"production_term_sheet", "application_term_sheet"}
        and not payload.acknowledged_non_binding
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Acknowledge that indicative loan terms are non-binding before accepting.",
        )
    changed = await offers.record_response(
        db,
        profile=profile,
        delivery=delivery,
        item=item,
        response=payload.response,
        responder_name=payload.responder_name,
        reason=payload.reason,
        channel="authenticated_client",
        responded_at=datetime.now(UTC),
        ip_address=_ip(request),
        user_agent=request.headers.get("user-agent"),
        user_id=user.id,
        attestation=(
            "Client acknowledged the non-binding indicative-terms disclosure."
            if payload.acknowledged_non_binding
            else None
        ),
    )
    if changed:
        await _notify_response(db, profile=profile, delivery=delivery, item=item, actor=user)
    await db.commit()
    read = offers.delivery_read(delivery, base_url=_client_base())
    return OfferResponseResult(
        delivery=read, item=next(value for value in read.items if value.id == item.id)
    )
