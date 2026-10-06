"""Durable Plaid Transfer dispatch and reconciliation jobs.

Database claims are committed before any network request. Provider calls then
run without a row lock, and their definite result is applied in a new
transaction. Ambiguous transfer creation is reconciled by its durable
authorization before any same-authorization retry. Ambiguous refunds keep the
same provider idempotency key and are recovered only after a stale-intent
window, so a process interruption cannot create a second money movement.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, text

from app.config import get_settings
from app.db import SessionLocal
from app.models.payments import (
    AchMandate,
    PaymentFundingSource,
    PaymentRefund,
    PaymentTransfer,
)
from app.services import ach_fee_workflow, payments, plaid_transfer

log = logging.getLogger(__name__)
FIRM_TIMEZONE = ZoneInfo("America/New_York")
_TRANSFER_SYNC_PROCESS_LOCK = asyncio.Lock()
_TRANSFER_SYNC_ADVISORY_KEY = "qc:plaid-transfer-event-sync"


class _DeferredUnknownTransferEvent(RuntimeError):
    """Provider enrichment failed; keep the durable cursor before this event."""


@dataclass(frozen=True)
class _ProviderContext:
    transfer_id: UUID
    amount_cents: int
    ach_class: plaid_transfer.AchClass
    idempotency_key: str
    attempt_no: int
    account_id: str
    access_token: str
    legal_name: str
    email: str | None
    ip_address: str | None
    user_agent: str | None
    authorization_id: str | None
    fee_obligation_id: UUID | None
    installment_id: UUID | None


async def _provider_context(transfer_id: UUID) -> _ProviderContext | None:
    async with SessionLocal() as db:
        transfer = await db.get(PaymentTransfer, transfer_id)
        if transfer is None or transfer.status != "submitting":
            return None
        if transfer.installment_id and not get_settings().private_funding_payments_enabled:
            return None
        source = await db.get(PaymentFundingSource, transfer.funding_source_id)
        mandate = await db.get(AchMandate, transfer.mandate_id)
        # Mandate/source/plan eligibility was checked under the transfer row
        # lock immediately before claim_due_transfers changed this intent from
        # authorizing to submitting. A later revocation stops future intents,
        # but cannot claim that this provider handoff was cancelled.
        if source is None or mandate is None:
            return None
        if (
            source.status != "verified"
            or source.revoked_at is not None
            or mandate.status != "active"
            or mandate.revoked_at is not None
            or not payments._mandate_matches_source(mandate, source)
            or str(transfer.ach_class).upper() != str(mandate.ach_class).upper()
        ):
            return None
        access_token = plaid_transfer.decrypt_access_token(source.access_token_ciphertext)
        if not access_token or not source.plaid_account_id:
            return None
        ach_class = str(source.ach_class or mandate.ach_class).strip().lower()
        if ach_class not in {"ccd", "web"}:
            return None
        return _ProviderContext(
            transfer_id=transfer.id,
            amount_cents=transfer.amount_cents,
            ach_class=ach_class,  # type: ignore[arg-type]
            idempotency_key=transfer.idempotency_key,
            attempt_no=transfer.attempt_no,
            account_id=source.plaid_account_id,
            access_token=access_token,
            legal_name=mandate.payer_name or mandate.typed_name,
            email=mandate.payer_email,
            ip_address=mandate.ip_address,
            user_agent=mandate.user_agent,
            authorization_id=transfer.plaid_authorization_id,
            fee_obligation_id=transfer.fee_obligation_id,
            installment_id=transfer.installment_id,
        )


async def _submission_failed(
    transfer_id: UUID, *, code: str, message: str, retryable: bool
) -> None:
    async with SessionLocal() as db:
        await payments.record_transfer_submission_failure(
            db,
            transfer_id=transfer_id,
            code=code,
            message=message,
            retryable=retryable,
        )
        await db.commit()


async def _record_authorization(transfer_id: UUID, authorization_id: str) -> None:
    async with SessionLocal() as db:
        await payments.record_transfer_authorization(
            db,
            transfer_id=transfer_id,
            plaid_authorization_id=authorization_id,
        )
        await db.commit()


async def _recover_ambiguous_create(
    *,
    context: _ProviderContext,
    authorization_id: str,
    amount: Decimal,
) -> dict[str, Any] | None:
    """Lookup first, then make one idempotent same-authorization retry."""

    try:
        recovered = await plaid_transfer.get_transfer_by_authorization(
            authorization_id
        )
    except plaid_transfer.PlaidTransferError:
        return None
    if recovered:
        return recovered
    # Plaid guarantees transfer/create idempotency for an authorization_id.
    # This retry cannot create a second debit and only follows a definitive
    # provider lookup showing no transfer for that authorization.
    try:
        return await plaid_transfer.create_transfer(
            access_token=context.access_token,
            account_id=context.account_id,
            authorization_id=authorization_id,
            amount=amount,
            description=(
                f"Retry {context.attempt_no - 1}"
                if context.attempt_no > 1
                else "QC Fees" if context.fee_obligation_id else "QC Payment"
            ),
            metadata={
                "payment_transfer_id": str(context.transfer_id),
                **(
                    {"fee_obligation_id": str(context.fee_obligation_id)}
                    if context.fee_obligation_id
                    else {"installment_id": str(context.installment_id)}
                ),
            },
        )
    except plaid_transfer.PlaidTransferError:
        return None


async def dispatch_payment_transfers(*, limit: int = 25) -> int:
    """Claim and submit already-authorized transfers exactly once."""

    settings = get_settings()
    # This flag is the kill switch for new provider handoffs. Reconciliation
    # stays live elsewhere so already-submitted transfers continue to update.
    if (
        not settings.payments_enabled
        or not plaid_transfer.enabled()
        or ach_fee_workflow.legal_approval_required()
    ):
        return 0
    async with SessionLocal() as db:
        claimed = await payments.claim_due_transfers(
            db,
            limit=limit,
            include_private=settings.private_funding_payments_enabled,
        )
        await db.commit()

    submitted = 0
    for claim in claimed:
        context = await _provider_context(claim.transfer_id)
        if context is None:
            await _submission_failed(
                claim.transfer_id,
                code="payment_source_or_mandate_unavailable",
                message="The payment source or signed mandate is no longer eligible.",
                retryable=False,
            )
            continue
        amount = Decimal(context.amount_cents) / Decimal("100")
        authorization_id: str | None = context.authorization_id
        try:
            if authorization_id:
                # A prior worker may have reached Plaid and exited before the
                # local submission row was updated. Resolve the durable
                # authorization first: recording an existing transfer closes
                # that crash window, while a definite empty result permits the
                # same-authorization create below without a duplicate debit.
                recovered = await plaid_transfer.get_transfer_by_authorization(
                    authorization_id
                )
                if recovered:
                    async with SessionLocal() as db:
                        await payments.record_transfer_submission(
                            db,
                            transfer_id=context.transfer_id,
                            plaid_authorization_id=authorization_id,
                            plaid_transfer_id=str(recovered["id"]),
                            provider_status=str(recovered.get("status") or "pending"),
                            metadata={
                                "reconciled_by": "authorization_id_before_create"
                            },
                        )
                        await db.commit()
                    submitted += 1
                    continue
            if not authorization_id:
                authorization = await plaid_transfer.create_authorization(
                    access_token=context.access_token,
                    account_id=context.account_id,
                    amount=amount,
                    ach_class=context.ach_class,
                    legal_name=context.legal_name,
                    email=context.email,
                    idempotency_key=f"qc-auth-{context.transfer_id}",
                    ip_address=context.ip_address,
                    user_agent=context.user_agent,
                    user_present=False,
                )
                authorization_id = str(authorization.get("id") or "")
                decision = str(authorization.get("decision") or "").lower()
                # Plaid returns an authorization id for user_action_required;
                # persist it so Link update mode can repair the exact decision.
                if authorization_id:
                    await _record_authorization(context.transfer_id, authorization_id)
                if decision != "approved" or not authorization_id:
                    rationale = authorization.get("decision_rationale")
                    if isinstance(rationale, dict):
                        code = str(
                            rationale.get("code")
                            or rationale.get("failure_code")
                            or decision
                            or "authorization_not_approved"
                        )
                        provider_message = str(
                            rationale.get("description")
                            or rationale.get("message")
                            or ""
                        )
                    else:
                        code = str(rationale or decision or "authorization_not_approved")
                        provider_message = ""
                    await _submission_failed(
                        context.transfer_id,
                        code=code,
                        message=provider_message or (
                            "The bank requires the client to reconnect or take action."
                            if decision == "user_action_required"
                            else "Plaid did not approve this transfer authorization."
                        ),
                        retryable=decision == "user_action_required",
                    )
                    continue
            transfer = await plaid_transfer.create_transfer(
                access_token=context.access_token,
                account_id=context.account_id,
                authorization_id=authorization_id,
                amount=amount,
                description=(
                    f"Retry {context.attempt_no - 1}"
                    if context.attempt_no > 1
                    else "QC Fees" if context.fee_obligation_id else "QC Payment"
                ),
                metadata={
                    "payment_transfer_id": str(context.transfer_id),
                    **(
                        {"fee_obligation_id": str(context.fee_obligation_id)}
                        if context.fee_obligation_id
                        else {"installment_id": str(context.installment_id)}
                    ),
                },
            )
            async with SessionLocal() as db:
                await payments.record_transfer_submission(
                    db,
                    transfer_id=context.transfer_id,
                    plaid_authorization_id=authorization_id,
                    plaid_transfer_id=str(transfer["id"]),
                    provider_status=str(transfer.get("status") or "pending"),
                    metadata={"request_id": transfer.get("request_id")},
                )
                await db.commit()
            submitted += 1
        except plaid_transfer.PlaidTransferError as exc:
            # A network/5xx result after /transfer/create is ambiguous. Resolve
            # by the durable authorization id; a retry is allowed only after a
            # definite empty lookup and reuses that same idempotent provider key.
            recovered = None
            if authorization_id and exc.retryable:
                recovered = await _recover_ambiguous_create(
                    context=context,
                    authorization_id=authorization_id,
                    amount=amount,
                )
            if recovered:
                async with SessionLocal() as db:
                    await payments.record_transfer_submission(
                        db,
                        transfer_id=context.transfer_id,
                        plaid_authorization_id=authorization_id,
                        plaid_transfer_id=str(recovered["id"]),
                        provider_status=str(recovered.get("status") or "pending"),
                        metadata={"reconciled_by": "authorization_id"},
                    )
                    await db.commit()
                submitted += 1
            else:
                await _submission_failed(
                    context.transfer_id,
                    code=exc.code or "plaid_transfer_error",
                    message=str(exc),
                    retryable=exc.retryable,
                )
            if recovered:
                log.info(
                    "Recovered ambiguous payment transfer %s by authorization id",
                    context.transfer_id,
                )
            else:
                log.warning(
                    "Payment transfer %s needs attention code=%s request_id=%s auth=%s",
                    context.transfer_id,
                    exc.code,
                    exc.request_id,
                    bool(authorization_id),
                )
        except Exception as exc:  # noqa: BLE001 - isolate each durable claim
            await _submission_failed(
                context.transfer_id,
                code="payment_dispatch_error",
                message="Payment dispatch failed before a definite provider result.",
                retryable=True,
            )
            log.exception("Payment transfer %s dispatch failed: %s", context.transfer_id, exc)
    return submitted


def _event_transfer_id(event: dict[str, Any]) -> UUID | None:
    """Recover a local transfer from Plaid metadata after an ambiguous 5xx."""

    candidates: list[Any] = [event.get("metadata")]
    transfer = event.get("transfer")
    if isinstance(transfer, dict):
        candidates.append(transfer.get("metadata"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw = candidate.get("payment_transfer_id")
        if raw:
            try:
                return UUID(str(raw))
            except ValueError:
                pass
    return None


async def _enrich_unknown_transfer_event(event: dict[str, Any]) -> dict[str, Any]:
    """Recover local metadata for provider events first seen after a lost 5xx."""

    provider_id = str(event.get("transfer_id") or "").strip()
    if not provider_id or _event_transfer_id(event):
        return event
    async with SessionLocal() as db:
        known = (
            await db.execute(
                select(PaymentTransfer.id)
                .where(PaymentTransfer.plaid_transfer_id == provider_id)
                .limit(1)
            )
        ).scalar_one_or_none()
    if known:
        return event
    try:
        provider_transfer = await plaid_transfer.get_transfer(provider_id)
    except plaid_transfer.PlaidTransferError as exc:
        log.warning(
            "Could not enrich unknown Plaid transfer event transfer_id=%s code=%s",
            provider_id,
            exc.code,
        )
        raise _DeferredUnknownTransferEvent(provider_id) from exc
    enriched = dict(event)
    enriched["transfer"] = provider_transfer
    return enriched


async def _sync_payment_transfer_events_serial(*, max_pages: int = 20) -> int:
    """Reconcile ordered Plaid events using a durable environment cursor."""

    if not plaid_transfer.enabled():
        return 0
    environment = plaid_transfer.environment()
    applied = 0
    for _page in range(max_pages):
        async with SessionLocal() as db:
            cursor = await payments.get_plaid_transfer_cursor(db, environment)
        try:
            after_id = int(cursor or 0)
        except (TypeError, ValueError):
            after_id = 0
        try:
            events, has_more = await plaid_transfer.sync_events(after_id=after_id, count=500)
        except plaid_transfer.PlaidTransferError as exc:
            async with SessionLocal() as db:
                await payments.set_plaid_transfer_cursor(
                    db, environment, cursor, error=f"{exc.code or 'plaid_error'}: {exc}"
                )
                await db.commit()
            log.warning("Plaid Transfer event sync failed code=%s request_id=%s", exc.code, exc.request_id)
            return applied
        if not events:
            async with SessionLocal() as db:
                await payments.set_plaid_transfer_cursor(db, environment, cursor, error=None)
                await db.commit()
            return applied
        try:
            events = [await _enrich_unknown_transfer_event(event) for event in events]
        except _DeferredUnknownTransferEvent as exc:
            async with SessionLocal() as db:
                await payments.set_plaid_transfer_cursor(
                    db,
                    environment,
                    cursor,
                    error=f"unknown_transfer_enrichment_failed:{exc}",
                )
                await db.commit()
            return applied
        highest = after_id
        async with SessionLocal() as db:
            for event in events:
                local_id = _event_transfer_id(event)
                if local_id and event.get("transfer_id"):
                    row = await db.get(PaymentTransfer, local_id, with_for_update=True)
                    if row and not row.plaid_transfer_id:
                        row.plaid_transfer_id = str(event["transfer_id"])
                await payments.apply_plaid_transfer_event(db, event)
                try:
                    highest = max(highest, int(event.get("event_id") or 0))
                except (TypeError, ValueError):
                    pass
                applied += 1
            await payments.set_plaid_transfer_cursor(db, environment, str(highest), error=None)
            await db.commit()
        if not has_more:
            return applied
    log.warning("Plaid Transfer sync hit its %d-page safety bound", max_pages)
    return applied


async def sync_payment_transfer_events(*, max_pages: int = 20) -> int:
    """Run exactly one event-cursor consumer per database environment.

    The in-process lock collapses duplicate webhook wakes. The Postgres
    advisory lock serializes scheduler/webhook workers across processes, so
    only one worker can read and advance a given durable cursor at a time.
    """

    if not plaid_transfer.enabled():
        return 0
    async with _TRANSFER_SYNC_PROCESS_LOCK:
        async with SessionLocal() as lock_db:
            acquired = bool((await lock_db.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:lock_key))"),
                {"lock_key": _TRANSFER_SYNC_ADVISORY_KEY},
            )).scalar_one())
            if not acquired:
                return 0
            try:
                return await _sync_payment_transfer_events_serial(
                    max_pages=max_pages
                )
            finally:
                await lock_db.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:lock_key))"),
                    {"lock_key": _TRANSFER_SYNC_ADVISORY_KEY},
                )


async def enqueue_due_private_installments(*, limit: int = 50) -> int:
    """Create one durable transfer intent per due fixed installment.

    This job never retries a failed/returned installment automatically.  A
    staff action must create the next attempt.
    """

    settings = get_settings()
    if not (settings.payments_enabled and settings.private_funding_payments_enabled):
        return 0
    async with SessionLocal() as db:
        created = await payments.materialize_due_installment_transfers(
            db,
            through_date=datetime.now(FIRM_TIMEZONE).date(),
            limit=limit,
        )
        await db.commit()
    return created


async def _verify_refund_ledger_capacity(
    *,
    plaid_transfer_id: str,
    amount: Decimal,
) -> None:
    """Fail closed unless the original transfer's Ledger can fund a refund."""

    provider_transfer = await plaid_transfer.get_transfer(plaid_transfer_id)
    returned_transfer_id = str(provider_transfer.get("id") or "").strip()
    if returned_transfer_id != plaid_transfer_id:
        raise plaid_transfer.PlaidTransferError(
            "Plaid returned a different original transfer",
            code="PLAID_REFUND_TRANSFER_MISMATCH",
        )
    ledger_id = str(provider_transfer.get("ledger_id") or "").strip()
    if not ledger_id:
        raise plaid_transfer.PlaidTransferError(
            "The original transfer has no Plaid Ledger reference",
            code="PLAID_REFUND_LEDGER_MISSING",
        )
    originator_client_id = str(
        provider_transfer.get("originator_client_id") or ""
    ).strip() or None
    available = await plaid_transfer.get_ledger_available_balance(
        ledger_id=ledger_id,
        originator_client_id=originator_client_id,
    )
    if available < amount:
        raise plaid_transfer.PlaidTransferError(
            "The original transfer's Plaid Ledger does not have enough available funds for this refund",
            code="PLAID_REFUND_LEDGER_INSUFFICIENT",
        )


async def dispatch_payment_refunds(*, limit: int = 10) -> int:
    """Submit explicit refunds with recoverable provider idempotency.

    New refunds are Ledger-checked before submission. Stale submitting rows
    replay the same provider idempotency key without repeating a balance check
    that may already reflect the accepted refund.
    """

    settings = get_settings()
    if not (
        settings.payments_enabled
        and settings.payment_refunds_enabled
        and plaid_transfer.enabled()
        and not ach_fee_workflow.legal_approval_required()
    ):
        return 0
    stale_before = datetime.now(UTC) - timedelta(minutes=10)
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(PaymentRefund)
                .where(
                    or_(
                        PaymentRefund.status == "pending",
                        and_(
                            PaymentRefund.status == "checking_balance",
                            PaymentRefund.plaid_refund_id.is_(None),
                            PaymentRefund.updated_at <= stale_before,
                        ),
                        and_(
                            PaymentRefund.status == "submitting",
                            PaymentRefund.plaid_refund_id.is_(None),
                            PaymentRefund.updated_at <= stale_before,
                        ),
                    )
                )
                .order_by(PaymentRefund.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        refund_work: list[tuple[UUID, bool]] = []
        for row in rows:
            transfer = await db.get(PaymentTransfer, row.transfer_id, with_for_update=True)
            if (
                transfer is None
                or transfer.installment_id is not None
                or transfer.status != "funds_available"
                or not transfer.plaid_transfer_id
            ):
                row.status = "action_required"
                continue
            if row.status == "pending":
                row.status = "checking_balance"
            refund_work.append((row.id, row.status == "checking_balance"))
        await db.commit()
    completed = 0
    for refund_id, requires_balance_guard in refund_work:
        async with SessionLocal() as db:
            refund = await db.get(PaymentRefund, refund_id, with_for_update=True)
            transfer = (
                await db.get(PaymentTransfer, refund.transfer_id, with_for_update=True)
                if refund
                else None
            )
            expected_status = (
                "checking_balance" if requires_balance_guard else "submitting"
            )
            if refund is None or refund.status != expected_status:
                continue
            if (
                transfer is None
                or transfer.installment_id is not None
                or transfer.status != "funds_available"
                or not transfer.plaid_transfer_id
            ):
                refund.status = "action_required"
                await db.commit()
                continue
            plaid_transfer_id = transfer.plaid_transfer_id
            amount = Decimal(refund.amount_cents) / Decimal("100")
            provider_idempotency_key = refund.provider_idempotency_key
            await db.commit()
        if requires_balance_guard:
            try:
                await _verify_refund_ledger_capacity(
                    plaid_transfer_id=plaid_transfer_id,
                    amount=amount,
                )
            except plaid_transfer.PlaidTransferError as exc:
                async with SessionLocal() as db:
                    refund = await db.get(
                        PaymentRefund, refund_id, with_for_update=True
                    )
                    if refund and refund.status == "checking_balance":
                        if exc.retryable:
                            # This was a read-only provider operation. Stay
                            # before the money-moving state and retry only the
                            # same safe check after the stale window.
                            refund.updated_at = datetime.now(UTC)
                        else:
                            refund.status = "action_required"
                        await db.commit()
                log.warning(
                    "Payment refund %s Ledger guard result=%s code=%s",
                    refund_id,
                    "temporarily unavailable" if exc.retryable else "blocked",
                    exc.code,
                )
                continue

            # Persist that the balance check completed before handoff. A stale
            # checking_balance row is guarded again; stale submitting means
            # the exact idempotent provider request may already have landed.
            async with SessionLocal() as db:
                refund = await db.get(PaymentRefund, refund_id, with_for_update=True)
                transfer = (
                    await db.get(
                        PaymentTransfer,
                        refund.transfer_id,
                        with_for_update=True,
                    )
                    if refund
                    else None
                )
                if refund is None or refund.status != "checking_balance":
                    continue
                if (
                    transfer is None
                    or transfer.installment_id is not None
                    or transfer.status != "funds_available"
                    or transfer.plaid_transfer_id != plaid_transfer_id
                ):
                    refund.status = "action_required"
                    await db.commit()
                    continue
                refund.status = "submitting"
                await db.commit()
        try:
            provider_refund = await plaid_transfer.create_refund(
                transfer_id=plaid_transfer_id,
                amount=amount,
                idempotency_key=provider_idempotency_key,
            )
            async with SessionLocal() as db:
                refund = await db.get(PaymentRefund, refund_id, with_for_update=True)
                if refund:
                    refund.plaid_refund_id = str(provider_refund["id"])
                    refund.status = str(provider_refund.get("status") or "submitted").lower()
                    if refund.status in {"completed", "funds_available"}:
                        refund.completed_at = datetime.now(UTC)
                    await db.commit()
            completed += 1
        except plaid_transfer.PlaidTransferError as exc:
            async with SessionLocal() as db:
                refund = await db.get(PaymentRefund, refund_id, with_for_update=True)
                if refund:
                    if exc.retryable:
                        # The provider may have accepted an uncertain request.
                        # Keep the same durable intent and delay an idempotent
                        # recovery replay instead of creating a new refund.
                        refund.status = "submitting"
                        refund.updated_at = datetime.now(UTC)
                    else:
                        refund.status = "failed"
                    await db.commit()
            log.warning(
                "Payment refund %s provider result=%s code=%s",
                refund_id,
                "uncertain; scheduled for idempotent recovery" if exc.retryable else "failed",
                exc.code,
            )
    return completed


async def run_payment_jobs() -> dict[str, int]:
    """One bounded scheduler wake-up; safe to call repeatedly."""

    scheduled = await enqueue_due_private_installments()
    transfers = await dispatch_payment_transfers()
    refunds = await dispatch_payment_refunds()
    return {"scheduled": scheduled, "transfers": transfers, "refunds": refunds}
