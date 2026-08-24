from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerPlaidItem
from app.dealer_os.services import plaid_client
from app.models.application_profile import ApplicationPlaidItem, PlaidAssetReport

PlaidItem = DealerPlaidItem | ApplicationPlaidItem


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


async def complete_update(item: PlaidItem) -> None:
    token = decrypted_access_token(item)
    state = await plaid_client.item_get(token)
    error = (state.get("item") or {}).get("error") or state.get("error")
    if error:
        item.status = "error"
        item.error = str(error.get("display_message") or error.get("error_message") or "Bank connection still needs attention")
        item.update_mode_reason = str(error.get("error_code") or "item_error").lower()[:32]
        raise plaid_client.PlaidUnavailable(item.error)
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
    item.update_mode_reason = None
    item.update_mode_account_selection = False
    item.next_refresh_at = _now()


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
    days_requested: int = 60,
) -> PlaidAssetReport:
    if (profile_id is None) == (dealer_id is None):
        raise ValueError("Exactly one Asset Report owner is required")
    usable = [item for item in items if item.status == "active" and active_environment(item)]
    if not usable:
        raise plaid_client.PlaidUnavailable("Connect at least one healthy production bank first")
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


async def remove_asset_report(report: PlaidAssetReport, *, strict: bool = True) -> None:
    token = plaid_client.decrypt_token(report.encrypted_asset_report_token)
    if token and report.environment == plaid_client.environment():
        try:
            await plaid_client.asset_report_remove(token)
        except plaid_client.PlaidUnavailable:
            if strict:
                raise
    report.status = "removed"
    report.encrypted_asset_report_token = None
    report.removed_at = _now()


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
            await remove_asset_report(report, strict=strict)


async def disconnect_item(
    db: AsyncSession, item: PlaidItem, *, strict: bool = True
) -> None:
    await remove_reports_for_item(db, item, strict=strict)
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if token and active_environment(item):
        try:
            await plaid_client.item_remove(token)
        except plaid_client.PlaidUnavailable:
            if strict:
                raise
    item.status = "removed"
    item.encrypted_access_token = None
    item.auto_refresh = False
    item.is_primary_operating = False
    item.next_refresh_at = None
    item.update_mode_reason = None
    item.update_mode_account_selection = False


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
