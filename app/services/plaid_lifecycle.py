from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerDocument, DealerPlaidItem
from app.dealer_os.services import plaid_client
from app.models.application_profile import ApplicationPlaidItem, PlaidAssetReport
from app.models.bucket import BucketFile
from app.services import plaid_policy

PlaidItem = DealerPlaidItem | ApplicationPlaidItem
log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def active_environment(item: PlaidItem) -> bool:
    return item.environment == plaid_client.environment()


def decrypted_access_token(item: PlaidItem) -> str:
    if not active_environment(item):
        raise plaid_client.PlaidUnavailable(
            f"This is a {item.environment} connection and cannot be used in "
            f"{plaid_client.environment()}"
        )
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if not token:
        raise plaid_client.PlaidUnavailable("The stored Plaid access token is unavailable")
    return token


async def complete_update(db: AsyncSession, item: PlaidItem) -> None:
    token = decrypted_access_token(item)
    policy, _owner = await plaid_policy.for_item(db, item)
    requested_products = plaid_policy.pending_products(item, policy)
    state = await plaid_policy.reconcile_item(db, item)
    error = (state.get("item") or {}).get("error") or state.get("error")
    if error:
        item.status = "error"
        item.error = str(error.get("display_message") or error.get("error_message") or "Bank connection still needs attention")
        item.update_mode_reason = str(error.get("error_code") or "item_error").lower()[:32]
        raise plaid_client.PlaidUnavailable(item.error)
    if (
        "statements" in requested_products
        and "statements" not in plaid_policy.item_products(item)
    ):
        plaid_policy.mark_optional_statements_unavailable(item, policy)
    accounts = await plaid_client.accounts_get(token)
    labels = [
        f"{row.get('name') or 'Account'} ··{row.get('mask')}"
        if row.get("mask")
        else str(row.get("name") or "Account")
        for row in accounts
    ]
    item.accounts_label = " · ".join(labels)[:200] or item.accounts_label
    item.status = "active"
    item.error = None
    item.update_mode_account_selection = False
    item.next_refresh_at = _now()
    # Update mode may add accounts without changing the Plaid Item id. Asset
    # Report freshness is otherwise keyed by Item ids, so the prior report
    # would incorrectly look current and omit the newly selected accounts.
    # Retained normalized evidence stays on the file while the caller queues a
    # fresh aggregate report across every active Item.
    await remove_reports_for_item(db, item, strict=False)


async def owner_asset_reports(
    db: AsyncSession, *, profile_id: UUID | None = None, dealer_id: UUID | None = None
) -> list[PlaidAssetReport]:
    owner_filter = (
        PlaidAssetReport.profile_id == profile_id
        if profile_id is not None
        else PlaidAssetReport.dealer_id == dealer_id
    )
    return list(
        (
            await db.execute(
                select(PlaidAssetReport)
                .where(owner_filter)
                .order_by(PlaidAssetReport.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def create_asset_report(
    db: AsyncSession,
    *,
    items: list[PlaidItem],
    profile_id: UUID | None = None,
    dealer_id: UUID | None = None,
    days_requested: int = 210,
) -> PlaidAssetReport:
    if (profile_id is None) == (dealer_id is None):
        raise ValueError("Exactly one Asset Report owner is required")
    usable = [
        item
        for item in items
        if item.status == "active"
        and active_environment(item)
        and "assets" in plaid_policy.item_products(item)
    ]
    if not usable:
        raise plaid_client.PlaidUnavailable(
            "Authorize Plaid Assets on at least one healthy bank connection first"
        )
    access_tokens = [decrypted_access_token(item) for item in usable]
    owner_id = profile_id or dealer_id
    report_id, report_token = await plaid_client.asset_report_create(
        access_tokens,
        client_report_id=str(owner_id),
        days_requested=days_requested,
    )
    report = PlaidAssetReport(
        profile_id=profile_id,
        dealer_id=dealer_id,
        asset_report_id=report_id,
        encrypted_asset_report_token=plaid_client.encrypt_token(report_token),
        environment=plaid_client.environment(),
        status="pending",
        days_requested=days_requested,
        source_item_ids=[str(item.id) for item in usable],
    )
    db.add(report)
    await db.flush()
    return report


def queue_report_cleanup(report: PlaidAssetReport) -> None:
    report.provider_removal_requested_at = report.provider_removal_requested_at or _now()


async def _remove_provider_report(report: PlaidAssetReport) -> None:
    if report.environment != plaid_client.environment():
        raise plaid_client.PlaidUnavailable("Report cleanup requires its original Plaid environment")
    token = plaid_client.decrypt_token(report.encrypted_asset_report_token)
    if not token and report.encrypted_asset_report_token:
        raise plaid_client.PlaidUnavailable("Report cleanup token cannot be decrypted")
    if token:
        await plaid_client.asset_report_remove(token)
    report.encrypted_asset_report_token = None
    report.provider_removed_at = _now()
    report.provider_removal_error = None


async def _require_retained_report(
    db: AsyncSession, report: PlaidAssetReport
) -> None:
    """Fail closed before deleting a provider report that contains evidence."""
    if not _report_reached_evidence_state(report):
        return
    if await retained_report_pdf(db, report) is not None:
        return
    report.provider_removal_error = (
        "Archived PDF unavailable; provider report retained until evidence is recoverable"
    )
    raise plaid_client.PlaidUnavailable(report.provider_removal_error)


def _report_reached_evidence_state(report: PlaidAssetReport) -> bool:
    """Include legacy rows whose status was changed before provider cleanup.

    Older ``strict=False`` removal marked a report ``removed`` even when the
    Plaid delete failed.  ``ready_at``/``ingested_at`` and archive links retain
    the evidence history needed to keep those cleanup retries fail closed.
    """
    return bool(
        report.status in {"ready", "ingested"}
        or getattr(report, "ready_at", None) is not None
        or getattr(report, "ingested_at", None) is not None
        or getattr(report, "document_id", None) is not None
        or getattr(report, "bucket_file_id", None) is not None
    )


async def remove_asset_report(
    db: AsyncSession, report: PlaidAssetReport, *, strict: bool = True
) -> None:
    queue_report_cleanup(report)
    archive_verified = not _report_reached_evidence_state(report)
    try:
        await _require_retained_report(db, report)
        archive_verified = True
        await _remove_provider_report(report)
    except plaid_client.PlaidUnavailable as exc:
        # Keep the ciphertext and durable request until Plaid confirms deletion.
        if not report.provider_removal_error:
            report.provider_removal_error = "Provider removal failed; cleanup will retry"
        if strict:
            raise
        # A ready/ingested report remains usable locally and remotely until a
        # durable archive exists.  Do not claim that it was removed.
        if not archive_verified:
            log.warning("Plaid report removal deferred for local report %s: %s", report.id, exc)
            return
    report.status = "removed"
    report.removed_at = _now()


async def retained_report_pdf(db: AsyncSession, report: PlaidAssetReport) -> bytes | None:
    """Read the immutable archive, scoped to the report's already-authorized owner."""
    from app.dealer_os.services import storage

    key = None
    if report.document_id:
        doc = await db.get(DealerDocument, report.document_id)
        if doc is not None and doc.dealer_id == report.dealer_id:
            key = doc.s3_key
    elif report.bucket_file_id and report.profile_id:
        from app.models.application_profile import ApplicationProfile

        profile = await db.get(ApplicationProfile, report.profile_id)
        file = await db.get(BucketFile, report.bucket_file_id)
        if (
            profile is not None and file is not None
            and file.bucket_id == profile.primary_bucket_id
            and file.status == "uploaded"
        ):
            key = file.s3_key
    if not key:
        return None
    try:
        raw = await asyncio.to_thread(storage.get_bytes, key)
    except Exception:  # noqa: BLE001 - storage SDK/network boundary; fail closed
        log.exception("Retained Plaid report could not be read for local report %s", report.id)
        return None
    return raw if raw and raw.startswith(b"%PDF-") else None


async def report_pdf(db: AsyncSession, report: PlaidAssetReport) -> bytes:
    if report.status not in {"ready", "ingested"}:
        raise plaid_client.PlaidUnavailable("Asset Report is not ready")
    archived = await retained_report_pdf(db, report)
    if archived is not None:
        return archived
    if report.environment != plaid_client.environment():
        raise plaid_client.PlaidUnavailable("Asset Report belongs to another Plaid environment")
    token = plaid_client.decrypt_token(report.encrypted_asset_report_token)
    if not token:
        raise plaid_client.PlaidUnavailable("The archived Asset Report is temporarily unavailable")
    raw = await plaid_client.asset_report_pdf(token)
    if not raw.startswith(b"%PDF-"):
        raise plaid_client.PlaidUnavailable("Plaid returned an invalid Asset Report PDF")
    return raw


async def cleanup_asset_reports(*, limit: int = 25) -> int:
    """Retry provider removal only after the ingestion transaction committed.

    Remote deletion does not expire ingested evidence or its 30-day freshness.
    Reports that ever reached ready/ingested remain archive-gated even when a
    legacy path already changed their current status to removed.
    """
    from app.db import SessionLocal

    if not plaid_client.enabled():
        return 0
    removed = 0
    async with SessionLocal() as db:
        reports = list((await db.execute(
            select(PlaidAssetReport).where(
                PlaidAssetReport.environment == plaid_client.environment(),
                PlaidAssetReport.provider_removal_requested_at.is_not(None),
                PlaidAssetReport.provider_removed_at.is_(None),
            ).order_by(PlaidAssetReport.provider_removal_requested_at).limit(limit)
            .with_for_update(skip_locked=True)
        )).scalars().all())
        for report in reports:
            try:
                await _require_retained_report(db, report)
                await _remove_provider_report(report)
                removed += 1
            except plaid_client.PlaidUnavailable as exc:
                if not report.provider_removal_error:
                    report.provider_removal_error = "Provider removal failed; cleanup will retry"
                log.warning("Plaid report cleanup deferred for local report %s: %s", report.id, exc)
        await db.commit()
    return removed


async def remove_reports_for_item(
    db: AsyncSession, item: PlaidItem, *, strict: bool = True
) -> None:
    owner_filter = (
        PlaidAssetReport.dealer_id == item.dealer_id
        if isinstance(item, DealerPlaidItem)
        else PlaidAssetReport.profile_id == item.profile_id
    )
    reports = list(
        (
            await db.execute(
                select(PlaidAssetReport).where(
                    owner_filter,
                    PlaidAssetReport.status != "removed",
                    PlaidAssetReport.environment == item.environment,
                )
            )
        )
        .scalars()
        .all()
    )
    item_key = str(item.id)
    for report in reports:
        if item_key in (report.source_item_ids or []):
            await remove_asset_report(db, report, strict=strict)


async def disconnect_item(
    db: AsyncSession, item: PlaidItem, *, strict: bool = True
) -> None:
    await remove_reports_for_item(db, item, strict=strict)
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    removed = not item.encrypted_access_token
    if token and active_environment(item):
        try:
            await plaid_client.item_remove(token)
            removed = True
        except plaid_client.PlaidUnavailable:
            if strict:
                raise
    elif item.encrypted_access_token and strict:
        raise plaid_client.PlaidUnavailable("Connection cleanup requires a usable token in its original environment")
    item.status = "removed"
    if removed:
        item.encrypted_access_token = None
    item.auto_refresh = False
    item.is_primary_operating = False
    item.next_refresh_at = None
    item.update_mode_reason = None
    item.update_mode_account_selection = False


async def cleanup_disconnected_items(*, limit: int = 25) -> int:
    from app.db import SessionLocal

    if not plaid_client.enabled():
        return 0
    removed = 0
    async with SessionLocal() as db:
        for model in (DealerPlaidItem, ApplicationPlaidItem):
            rows = list((await db.execute(select(model).where(
                model.status == "removed",
                model.encrypted_access_token.is_not(None),
                model.environment == plaid_client.environment(),
            ).limit(limit).with_for_update(skip_locked=True))).scalars().all())
            for item in rows:
                await disconnect_item(db, item, strict=False)
                removed += int(item.encrypted_access_token is None)
        await db.commit()
    return removed


async def purge_owner_connections(
    db: AsyncSession,
    *,
    dealer_id: UUID | None = None,
    profile_id: UUID | None = None,
    strict: bool = True,
) -> int:
    rows: list[PlaidItem] = []
    if dealer_id is not None:
        rows.extend(
            (
                await db.execute(
                    select(DealerPlaidItem).where(
                        DealerPlaidItem.dealer_id == dealer_id,
                        DealerPlaidItem.status != "removed",
                    )
                )
            ).scalars().all()
        )
    if profile_id is not None:
        rows.extend(
            (
                await db.execute(
                    select(ApplicationPlaidItem).where(
                        ApplicationPlaidItem.profile_id == profile_id,
                        ApplicationPlaidItem.status != "removed",
                    )
                )
            ).scalars().all()
        )
    for item in rows:
        await disconnect_item(db, item, strict=strict)
    return len(rows)
