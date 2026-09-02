from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import boto3
from botocore.config import Config
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dealer_os.services import plaid_client
from app.models.application_profile import (
    ApplicationPlaidItem,
    ApplicationProfile,
    PlaidAssetReport,
)
from app.models.bucket import BucketFile, BucketFileAnalysis
from app.services import plaid_lifecycle, plaid_policy
from app.services.application_profiles import log_profile_action

logger = logging.getLogger(__name__)
MAX_STATEMENTS_PER_SYNC = 60
MAX_ITEMS_PER_TICK = 20


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_filename(value: str) -> str:
    return (re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")[:180] or "statement.pdf")


def _s3_client():
    settings = get_settings()
    kwargs = {
        "region_name": settings.aws_region,
        "config": Config(signature_version="s3v4"),
    }
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("s3", **kwargs)


async def _put_pdf(key: str, raw: bytes) -> None:
    settings = get_settings()
    if not settings.s3_bucket or not settings.buckets_kms_key_id:
        raise RuntimeError("Evidence storage is not configured")
    await asyncio.to_thread(
        _s3_client().put_object,
        Bucket=settings.s3_bucket,
        Key=key,
        Body=raw,
        ContentType="application/pdf",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=settings.buckets_kms_key_id,
    )


async def ingest_asset_report(
    db: AsyncSession, asset_report_id: str
) -> PlaidAssetReport:
    """Retain a standalone Funding Asset Report PDF and normalized JSON."""
    report = (
        await db.execute(
            select(PlaidAssetReport)
            .where(PlaidAssetReport.asset_report_id == asset_report_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if report is None or report.profile_id is None:
        raise ValueError("Standalone Funding Asset Report not found")
    if report.ingested_at is not None and report.bucket_file_id is not None:
        return report
    profile = await db.get(ApplicationProfile, report.profile_id)
    if profile is None or profile.primary_bucket_id is None:
        raise ValueError("Asset Report is not linked to an evidence bucket")
    policy, _owner = await plaid_policy.for_profile(db, profile)
    if not policy.assets_enabled:
        await plaid_lifecycle.remove_asset_report(report, strict=False)
        await db.flush()
        return report
    token = plaid_client.decrypt_token(report.encrypted_asset_report_token)
    if not token:
        raise plaid_client.PlaidUnavailable("Asset Report token is unavailable")
    payload = await plaid_client.asset_report_get(token)
    pdf = await plaid_client.asset_report_pdf(token)
    if not pdf.startswith(b"%PDF-"):
        raise plaid_client.PlaidUnavailable("Plaid returned an invalid Asset Report PDF")

    from app.dealer_os.services.plaid_assets import normalize_asset_report
    from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION

    normalized = normalize_asset_report(payload)
    if not normalized:
        raise plaid_client.PlaidUnavailable("Plaid returned no usable Asset Report accounts")
    monthly_rows: list[dict] = []
    account_rows: list[dict] = []
    for account in normalized:
        months = [dict(month) for month in account.get("months") or []]
        monthly_rows.extend(months)
        account_rows.append({"account": account.get("account") or {}, "months": months})
    observed_months = sorted(
        {
            str(row.get("month"))
            for row in monthly_rows
            if re.fullmatch(r"\d{4}-\d{2}", str(row.get("month") or ""))
        }
    )
    digest = hashlib.sha256(pdf).hexdigest()
    file_id = uuid4()
    filename = f"Plaid Asset Report {report.created_at:%Y-%m-%d}.pdf"
    prefix = get_settings().buckets_s3_prefix.strip("/")
    key = f"{prefix}/plaid-assets/{profile.primary_bucket_id}/{file_id}-{filename}"
    await _put_pdf(key, pdf)
    file = BucketFile(
        id=file_id,
        bucket_id=profile.primary_bucket_id,
        file_name=filename,
        s3_key=key,
        content_type="application/pdf",
        size_bytes=len(pdf),
        uploaded_by_name="Plaid Assets",
        status="uploaded",
        content_hash=digest,
        extraction_status="completed",
    )
    db.add(file)
    await db.flush()
    analysis = BucketFileAnalysis(
        bucket_file_id=file.id,
        bucket_id=file.bucket_id,
        content_hash=digest,
        analysis_version=CURRENT_FILE_ANALYSIS_VERSION,
        provider="plaid",
        model="assets-json",
        status="completed",
        classification="bank_statement",
        confidence="high",
        summary=(
            f"Verified Plaid Asset Report covering {len(observed_months)} month(s) "
            f"across {len(account_rows)} account(s)."
        ),
        analysis={
            "source": "plaid_assets",
            "verified": True,
            "asset_report_id": report.asset_report_id,
            "key_facts": {"months": [{"month": month} for month in observed_months]},
            "accounts": account_rows,
        },
        analyzed_at=_now(),
    )
    db.add(analysis)
    report.bucket_file_id = file.id
    report.status = "ingested"
    report.ingested_at = _now()
    report.error = None
    await log_profile_action(
        db,
        profile,
        None,
        "plaid.asset_report.ingested",
        "Added verified Plaid Assets evidence to the application bucket",
        target_type="file",
        target_id=file.id,
        metadata={
            "asset_report_id": report.asset_report_id,
            "months": observed_months,
            "account_count": len(account_rows),
        },
    )
    await db.flush()
    return report


async def ingest_asset_report_background(asset_report_id: str) -> None:
    from app.db import SessionLocal

    try:
        async with SessionLocal() as db:
            report = (
                await db.execute(
                    select(PlaidAssetReport).where(
                        PlaidAssetReport.asset_report_id == asset_report_id
                    )
                )
            ).scalar_one_or_none()
            if report is None:
                return
            if report.dealer_id is not None:
                from app.dealer_os.services.plaid_assets import ingest_asset_report as ingest_dealer

                await ingest_dealer(db, asset_report_id)
            else:
                await ingest_asset_report(db, asset_report_id)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Plaid Asset Report ingestion failed report=%s", asset_report_id)


async def sync_item(
    db: AsyncSession,
    item: ApplicationPlaidItem,
    *,
    scheduled: bool = False,
) -> dict[str, int]:
    if item.status == "removed" or item.environment != plaid_client.environment():
        return {"pulled": 0, "skipped": 0, "failed": 0}
    profile = await db.get(ApplicationProfile, item.profile_id)
    if profile is None or profile.primary_bucket_id is None:
        item.status = "error"
        item.error = "This application does not have an evidence bucket"
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}

    policy, owner = await plaid_policy.for_item(db, item)
    if owner is None:
        return {"pulled": 0, "skipped": 0, "failed": 0}
    try:
        policy.validate()
    except (plaid_policy.InvalidPlaidPolicy, plaid_policy.PlaidProductUnavailable) as exc:
        item.status = "error"
        item.error = str(exc)[:500]
        item.next_refresh_at = _now() + timedelta(days=1)
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}
    if not await plaid_policy.has_required_consent(db, item, policy):
        item.status = "active"
        item.error = None
        item.next_refresh_at = None
        await db.flush()
        return {"pulled": 0, "skipped": 1, "failed": 0}
    if item.plaid_products_checked_at is None:
        try:
            await plaid_policy.reconcile_item(db, item)
        except plaid_client.PlaidUnavailable as exc:
            item.status = "error"
            item.error = str(exc)[:500]
            item.next_refresh_at = _now() + timedelta(days=1)
            await db.flush()
            return {"pulled": 0, "skipped": 0, "failed": 1}
    missing = plaid_policy.pending_products(item, policy)
    if missing:
        item.status = "active"
        item.error = None
        item.update_mode_reason = plaid_policy.update_reason(missing)
        item.next_refresh_at = None
        await db.flush()
        return {"pulled": 0, "skipped": 1, "failed": 0}

    statements_collectible = (
        policy.statements_enabled
        and "statements" not in plaid_policy.unavailable_products(item)
    )
    asset_pulled = asset_skipped = asset_failed = 0

    if policy.assets_enabled:
        asset_items = list(
            (
                await db.execute(
                    select(ApplicationPlaidItem).where(
                        ApplicationPlaidItem.profile_id == profile.id,
                        or_(
                            ApplicationPlaidItem.status == "active",
                            and_(
                                ApplicationPlaidItem.status == "error",
                                ApplicationPlaidItem.update_mode_reason.is_(None),
                                ApplicationPlaidItem.encrypted_access_token.is_not(None),
                            ),
                        ),
                        ApplicationPlaidItem.environment == plaid_client.environment(),
                    )
                )
            ).scalars().all()
        )
        for asset_item in asset_items:
            if asset_item.status == "error":
                asset_item.status = "active"
                asset_item.error = None
        asset_items = [
            asset_item
            for asset_item in asset_items
            if "assets" in plaid_policy.item_products(asset_item)
        ]
        latest = (
            await db.execute(
                select(PlaidAssetReport)
                .where(
                    PlaidAssetReport.profile_id == profile.id,
                    PlaidAssetReport.environment == plaid_client.environment(),
                    PlaidAssetReport.removed_at.is_(None),
                )
                .order_by(PlaidAssetReport.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        source_ids = sorted(str(source.id) for source in asset_items)
        current = bool(
            latest is not None
            and sorted(latest.source_item_ids or []) == source_ids
            and (
                (
                    latest.status in {"pending", "ready"}
                    and _now() - latest.created_at < timedelta(days=1)
                )
                or (
                    latest.status == "ingested"
                    and _now() - latest.created_at
                    < timedelta(days=plaid_client.REFRESH_EVERY_DAYS)
                )
            )
        )
        try:
            if not current:
                latest = await plaid_lifecycle.create_asset_report(
                    db,
                    items=asset_items,
                    profile_id=profile.id,
                    days_requested=210,
                )
                await db.commit()
        except plaid_client.PlaidUnavailable as exc:
            asset_failed = 1
            if not statements_collectible:
                item.status = "error"
                item.error = str(exc)[:500]
                item.last_pulled_at = _now()
                item.next_refresh_at = _now() + timedelta(days=1)
                await db.flush()
                return {"pulled": 0, "skipped": 0, "failed": 1}
        else:
            asset_pulled = 0 if current else 1
            asset_skipped = 1 if current else 0
            if not statements_collectible:
                item.status = "active"
                item.error = None
                item.last_pulled_at = _now()
                item.next_refresh_at = _now() + timedelta(
                    days=plaid_client.REFRESH_EVERY_DAYS
                )
                await db.flush()
                return {
                    "pulled": 0 if current else 1,
                    "skipped": 1 if current else 0,
                    "failed": 0,
                }

    if not statements_collectible:
        if not policy.assets_enabled:
            item.status = "active"
            item.error = None
            item.next_refresh_at = None
            await db.flush()
        return {
            "pulled": asset_pulled,
            "skipped": asset_skipped,
            "failed": asset_failed,
        }
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if not token:
        item.status = "error"
        item.error = "Stored bank credentials could not be read; reconnect the bank"
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}
    if scheduled and item.statements_refresh_state != "ready":
        try:
            await plaid_client.statements_refresh(token)
        except plaid_client.PlaidUnavailable as exc:
            item.status = "error"
            item.error = str(exc)[:500]
            item.next_refresh_at = _now() + timedelta(days=1)
            await db.flush()
            return {
                "pulled": asset_pulled,
                "skipped": asset_skipped,
                "failed": asset_failed + 1,
            }
        item.statements_refresh_state = "pending"
        item.statements_refresh_requested_at = _now()
        item.last_pulled_at = _now()
        item.next_refresh_at = _now() + timedelta(days=1)
        await db.flush()
        return {
            "pulled": asset_pulled,
            "skipped": asset_skipped + 1,
            "failed": asset_failed,
        }

    try:
        listing = await plaid_client.statements_list(token)
        if listing.get("institution_name") and not item.institution_name:
            item.institution_name = str(listing["institution_name"])[:160]
        if not item.accounts_label:
            accounts = await plaid_client.accounts_get(token)
            labels = [
                " ".join(part for part in (account.get("name"), f"..{account['mask']}" if account.get("mask") else None) if part)
                for account in accounts
            ]
            item.accounts_label = (" | ".join(labels))[:200] or None
    except plaid_client.PlaidUnavailable as exc:
        item.status = "error"
        item.error = str(exc)[:500]
        item.last_pulled_at = _now()
        item.next_refresh_at = _now() + timedelta(days=1)
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}
    item.statements_refresh_state = None
    item.statements_refresh_requested_at = None

    existing = set(
        (
            await db.execute(
                select(BucketFile.plaid_statement_id).where(
                    BucketFile.application_plaid_item_id == item.id,
                    BucketFile.plaid_statement_id.is_not(None),
                )
            )
        ).scalars().all()
    )
    fresh = [row for row in listing.get("statements", []) if row["statement_id"] not in existing]
    skipped = len(listing.get("statements", [])) - len(fresh)
    pulled = failed = 0
    for statement in fresh[:MAX_STATEMENTS_PER_SYNC]:
        try:
            raw = await plaid_client.statements_download(token, statement["statement_id"])
            if not raw:
                failed += 1
                continue
            period = (
                f"{int(statement['year']):04d}-{int(statement['month']):02d}"
                if statement.get("year") and statement.get("month")
                else None
            )
            account = f" {statement['account_name']}" if statement.get("account_name") else ""
            filename = _safe_filename(
                f"{item.institution_name or 'Bank'}{account} statement {period or statement['statement_id'][:8]}.pdf"
            )
            file_id = uuid4()
            prefix = get_settings().buckets_s3_prefix.strip("/")
            key = f"{prefix}/plaid/{profile.primary_bucket_id}/{file_id}-{filename}"
            await _put_pdf(key, raw)
            file = BucketFile(
                id=file_id,
                bucket_id=profile.primary_bucket_id,
                application_plaid_item_id=item.id,
                plaid_statement_id=statement["statement_id"],
                statement_period=period,
                file_name=filename,
                s3_key=key,
                content_type="application/pdf",
                size_bytes=len(raw),
                uploaded_by_name="Plaid",
                status="uploaded",
            )
            db.add(file)
            await db.flush()
            from app.services.bucket_ai import enqueue_file_analysis

            await enqueue_file_analysis(db, file)
            await log_profile_action(
                db,
                profile,
                None,
                "plaid.statement_ingested",
                f"Added {filename} to application evidence",
                target_type="file",
                target_id=file.id,
                metadata={"period": period, "plaid_item_id": str(item.id)},
            )
            await db.commit()
            pulled += 1
        except Exception:  # noqa: BLE001
            logger.exception("application Plaid statement sync failed item=%s", item.id)
            await db.rollback()
            item = await db.get(ApplicationPlaidItem, item.id)
            profile = await db.get(ApplicationProfile, item.profile_id) if item else None
            failed += 1
            if item is None or profile is None:
                break
    item = await db.get(ApplicationPlaidItem, item.id)
    if item is not None:
        item.status = "active"
        item.error = None
        item.last_pulled_at = _now()
        item.next_refresh_at = _now() + timedelta(days=plaid_client.REFRESH_EVERY_DAYS)
        await db.flush()
    return {
        "pulled": pulled + asset_pulled,
        "skipped": skipped + asset_skipped,
        "failed": failed + asset_failed,
    }


async def sync_item_background(item_id, scheduled: bool = False) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as db:
        item = await db.get(ApplicationPlaidItem, item_id)
        if item is None:
            return
        await sync_item(db, item, scheduled=scheduled)
        await db.commit()


async def refresh_due() -> dict[str, int]:
    """Refresh due non-Dealer application banks with one session per item."""
    from app.db import SessionLocal

    if not plaid_client.enabled():
        return {"items": 0}
    async with SessionLocal() as db:
        item_ids = list(
            (
                await db.execute(
                    select(ApplicationPlaidItem.id).where(
                        ApplicationPlaidItem.status.in_(("active", "error")),
                        ApplicationPlaidItem.environment == plaid_client.environment(),
                        ApplicationPlaidItem.auto_refresh.is_(True),
                        ApplicationPlaidItem.next_refresh_at.is_not(None),
                        ApplicationPlaidItem.next_refresh_at <= _now(),
                    ).limit(MAX_ITEMS_PER_TICK)
                )
            ).scalars().all()
        )
    synced = 0
    for item_id in item_ids:
        try:
            await sync_item_background(item_id, scheduled=True)
            synced += 1
        except Exception:  # noqa: BLE001
            logger.exception("application Plaid scheduled refresh failed item=%s", item_id)
    return {"items": synced}
