from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import boto3
from botocore.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dealer_os.services import plaid_client
from app.models.application_profile import ApplicationPlaidItem, ApplicationProfile
from app.models.bucket import BucketFile
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


async def sync_item(db: AsyncSession, item: ApplicationPlaidItem) -> dict[str, int]:
    if item.status == "removed":
        return {"pulled": 0, "skipped": 0, "failed": 0}
    profile = await db.get(ApplicationProfile, item.profile_id)
    if profile is None or profile.primary_bucket_id is None:
        item.status = "error"
        item.error = "This application does not have an evidence bucket"
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if not token:
        item.status = "error"
        item.error = "Stored bank credentials could not be read; reconnect the bank"
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}
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
    return {"pulled": pulled, "skipped": skipped, "failed": failed}


async def sync_item_background(item_id) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as db:
        item = await db.get(ApplicationPlaidItem, item_id)
        if item is None:
            return
        await sync_item(db, item)
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
            await sync_item_background(item_id)
            synced += 1
        except Exception:  # noqa: BLE001
            logger.exception("application Plaid scheduled refresh failed item=%s", item_id)
    return {"items": synced}
