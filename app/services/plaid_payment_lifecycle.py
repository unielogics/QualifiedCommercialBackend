"""Payment Item revocation and cleanup; never creates money movement."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.payments import AchMandate, PaymentFundingSource
from app.services import ach_fee_workflow, payments, plaid_transfer

log = logging.getLogger(__name__)
PENDING_SOURCE_TTL = timedelta(hours=24)


async def handle_item_webhook(db: AsyncSession, payload: dict[str, Any]) -> str:
    """Only called after Plaid signature verification, scoped by environment."""
    code = payload.get("webhook_code")
    if code not in {"USER_PERMISSION_REVOKED", "USER_ACCOUNT_REVOKED"}:
        return "ignored: payment item event"
    item_id = str(payload.get("item_id") or "")
    environment = str(payload.get("environment") or "")
    if not item_id or environment not in {"sandbox", "production"}:
        return "ignored: missing payment item scope"
    query = select(PaymentFundingSource).where(
        PaymentFundingSource.plaid_item_id == item_id,
        PaymentFundingSource.plaid_environment == environment,
    )
    if code == "USER_ACCOUNT_REVOKED":
        account_id = str(payload.get("account_id") or "")
        if not account_id:
            return "ignored: missing payment account scope"
        query = query.where(PaymentFundingSource.plaid_account_id == account_id)
    sources = list((await db.execute(query.with_for_update())).scalars().all())
    now = datetime.now(UTC)
    for source in sources:
        source.status = "revoked"
        source.revoked_at = source.revoked_at or now
        # Full Item revocation invalidates its access token. For account-only
        # revocation retain the Item for the cleanup worker to remove explicitly.
        if code == "USER_PERMISSION_REVOKED":
            source.access_token_ciphertext = None
        mandates = list((await db.execute(select(AchMandate).where(
            AchMandate.funding_source_id == source.id,
            AchMandate.status == "active",
        ).with_for_update())).scalars().all())
        for mandate in mandates:
            mandate.status = "revoked"
            mandate.revoked_at = mandate.revoked_at or now
            mandate.terminated_at = mandate.terminated_at or now
            mandate.termination_reason = "Bank account access revoked at Plaid"
            await ach_fee_workflow.extend_mandate_retention(db, mandate, anchor=now)
        await payments.cancel_unclaimed_transfer_intents(
            db, funding_source_ids=[source.id], reason="plaid_permission_revoked"
        )
    return "payment sources revoked" if sources else "ignored: unknown payment item"


async def cleanup_payment_items(*, limit: int = 25) -> int:
    """Expire abandoned exchanges and remove replaced Items, even while payments are off.

    Commit ineligibility before Plaid I/O. A failure retains the encrypted token
    for retry. Scope every call to the credentials' environment.
    """
    if not plaid_transfer.enabled():
        return 0
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        rows = list((await db.execute(select(PaymentFundingSource).where(
            PaymentFundingSource.plaid_environment == plaid_transfer.environment(),
            PaymentFundingSource.access_token_ciphertext.is_not(None),
            or_(
                PaymentFundingSource.status.in_(["expired", "revoked"]),
                (PaymentFundingSource.status == "pending")
                & (PaymentFundingSource.created_at <= now - PENDING_SOURCE_TTL),
            ),
        ).order_by(PaymentFundingSource.created_at).limit(limit)
        .with_for_update(skip_locked=True))).scalars().all())
        pending = []
        for source in rows:
            if source.status == "pending":
                source.status = "expired"
                source.revoked_at = now
            pending.append((source.id, source.access_token_ciphertext))
        await db.commit()
    removed = 0
    for source_id, ciphertext in pending:
        token = plaid_transfer.decrypt_access_token(ciphertext)
        error = None
        if token:
            try:
                await plaid_transfer.remove_item(token)
            except plaid_transfer.PlaidTransferError as exc:
                error = exc.code or "provider_removal_failed"
        else:
            error = "cleanup_token_unavailable"
        async with SessionLocal() as db:
            source = await db.get(PaymentFundingSource, source_id, with_for_update=True)
            if source is None or source.status not in {"expired", "revoked"}:
                continue
            metadata = dict(source.metadata_json or {})
            metadata["plaid_cleanup_error"] = error
            if error is None and source.access_token_ciphertext == ciphertext:
                source.access_token_ciphertext = None
                metadata["plaid_item_removed_at"] = datetime.now(UTC).isoformat()
                removed += 1
            source.metadata_json = metadata
            await db.commit()
        if error:
            log.warning("Payment Item cleanup deferred local_source=%s code=%s", source_id, error)
    return removed
