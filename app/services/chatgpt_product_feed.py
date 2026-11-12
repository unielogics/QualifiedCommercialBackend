"""Private configuration, durable claims and delivery for the public catalog."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models.chatgpt_product_feed import ChatGPTFeedConfig, ChatGPTFeedRun
from app.schemas.chatgpt_product_feed import FeedSettings
from app.services import chatgpt_feed_transport as transport
from app.services import provider_secrets

SECRET_KEY = "chatgpt_feed_ssh_private_key"
RUN_FIELDS = ("id", "status", "trigger", "attempt_count", "created_at", "started_at", "completed_at", "retry_at", "eligible_count", "excluded_count", "sha256", "last_error", "processing_status")


def now() -> datetime:
    return datetime.now(UTC)


def utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


async def config_row(db: AsyncSession, *, lock: bool = False) -> ChatGPTFeedConfig:
    statement = select(ChatGPTFeedConfig).where(ChatGPTFeedConfig.id == 1).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        # The migration seeds this singleton. Refuse to invent an unprotected
        # row on concurrent first access if deployment skipped the migration.
        raise HTTPException(503, "ChatGPT feed storage is not initialized. Apply the database migration.")
    return row


def connection_signature(data: dict, public_key: str | None) -> str:
    names = ("sftp_host", "sftp_port", "sftp_username", "server_fingerprint", "remote_directory", "staging_directory", "staging_isolation_confirmed", "filename")
    return hashlib.sha256(json.dumps({**{name: data.get(name) for name in names}, "public_key": public_key}, sort_keys=True).encode()).hexdigest()


def verified(row: ChatGPTFeedConfig, data: dict) -> bool:
    return bool(row.delivery_verified_at and row.connection_signature == connection_signature(data, row.public_key))


async def settings_view(db: AsyncSession, row: ChatGPTFeedConfig | None = None) -> dict:
    row = row or await config_row(db)
    data = FeedSettings.model_validate(row.data).model_dump(mode="json")
    data.update(revision=row.revision, public_key=row.public_key,
                private_key_configured=await provider_secrets.is_configured(db, SECRET_KEY),
                last_success_at=row.last_success_at, next_run_at=row.next_run_at,
                delivery_verified_at=row.delivery_verified_at, connection_verified=verified(row, data))
    return data


async def require_idle_upload(db: AsyncSession) -> None:
    # Call only while holding the singleton configuration lock. The worker
    # acquires this same lock before making a durable running claim, so an
    # accepted settings save can never race an old snapshot into publication.
    running = (await db.execute(select(ChatGPTFeedRun.id).where(ChatGPTFeedRun.status == "running").limit(1))).scalar_one_or_none()
    if running:
        raise HTTPException(409, "An upload is in progress. Wait until it finishes before changing catalog, approval or connection settings; then save again.")


async def save_settings(db: AsyncSession, payload: FeedSettings) -> dict:
    row = await config_row(db, lock=True)
    if payload.revision != row.revision:
        raise HTTPException(409, "These settings changed in another session. Reload before saving.")
    await require_idle_upload(db)
    data = payload.model_dump(mode="json", exclude={"revision"})
    if row.last_success_at:
        old = FeedSettings.model_validate(row.data).model_dump(mode="json")
        destination = ("sftp_host", "sftp_port", "sftp_username", "remote_directory", "filename")
        if any(old[name] != data[name] for name in destination):
            raise HTTPException(422, "This feed has already uploaded a catalog. Keep its hostname, port, username, import directory and filename stable. Moving it requires a controlled migration with OpenAI and removal of the old imported snapshot; changing these fields would leave competing catalogs. Key, fingerprint and staging changes can be saved and tested separately.")
    if payload.enabled:
        await require_delivery_ready(db, row, data)
    if not verified(row, data):
        row.connection_signature = None
        row.delivery_verified_at = None
    row.data = data
    row.revision += 1
    row.next_run_at = next_daily(now(), payload.schedule_hour_utc) if payload.enabled else None
    await db.commit()
    return await settings_view(db, row)


async def key_pair(db: AsyncSession, *, rotate: bool, user_id=None) -> dict:
    row = await config_row(db, lock=True)
    if row.public_key and await provider_secrets.is_configured(db, SECRET_KEY) and not rotate:
        return await settings_view(db, row)
    await require_idle_upload(db)
    environment = get_settings()
    if not (environment.provider_secrets_kms_key_id or environment.provider_secrets_encryption_key):
        raise HTTPException(503, "Configure provider_secrets_kms_key_id or a dedicated provider_secrets_encryption_key before creating the SFTP key.")
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    await provider_secrets.set_secret(db, key=SECRET_KEY, value=private, updated_by_id=user_id)
    row.public_key = public + " qualified-commercial-chatgpt-feed"
    row.revision += 1
    row.data = {**row.data, "enabled": False}
    row.connection_signature = None
    row.delivery_verified_at = None
    row.next_run_at = None
    await db.commit()
    return await settings_view(db, row)


async def preview(db: AsyncSession, data: dict | None = None) -> dict:
    from app.models.funding_program import FundingProgramCatalog
    from app.services.chatgpt_feed_catalog import build_preview, retirement_snapshot_preview

    row = await config_row(db)
    data = data if data is not None else row.data
    current = await build_preview(db, data)
    missing = {item["item_id"] for item in row.last_exported_rows} - {item["item_id"] for item in current["items"]}
    retired_ids = set()
    if missing:
        # Absence alone is not retirement: require the actual catalog record's
        # status, or the staff's explicit exclusion in this configuration.
        retired_ids = set((await db.execute(select(FundingProgramCatalog.program_key).where(
            FundingProgramCatalog.program_key.in_(missing), FundingProgramCatalog.status == "retired",
        ))).scalars().all())
    return retirement_snapshot_preview(current, row.last_exported_rows, data, retired_ids)


async def export_snapshot(db: AsyncSession) -> tuple[dict, bytes]:
    from app.services.chatgpt_feed_catalog import snapshot_bytes

    data = await preview(db)
    if not data.get("export_count", data.get("eligible_count")):
        raise HTTPException(422, "No complete, approved offers are available. Resolve the catalog validation issues before downloading an import file.")
    try:
        return data, snapshot_bytes(data)
    except ValueError:
        raise HTTPException(422, "The catalog is not a valid import snapshot. Review its validation issues.") from None


async def _private_key(db: AsyncSession) -> str:
    value = await provider_secrets.get_secret(db, SECRET_KEY)
    if not value:
        raise HTTPException(422, "The SFTP private key is unavailable. Configure encryption and generate or restore the dedicated key.")
    return value


async def require_delivery_ready(db: AsyncSession, row: ChatGPTFeedConfig, data: dict, *, check_preview: bool = True) -> dict | None:
    if not data.get("acceptance_confirmed") or not data.get("acceptance_reference", "").strip():
        raise HTTPException(422, "Delivery is paused until OpenAI confirms financing/service acceptance and purchase-price treatment. Record that confirmation in Settings.")
    if not verified(row, data):
        raise HTTPException(422, "Test the saved connection, server fingerprint and isolated staging path before uploading.")
    await _private_key(db)
    if check_preview:
        result = await preview(db, data)
        if not result.get("ready_for_upload") or not result.get("export_count", result.get("eligible_count")):
            raise HTTPException(422, "No publishable catalog is ready. Review the preview's required fields, source approvals and pricing issues.")
        return result
    return None


async def test_saved_connection(db: AsyncSession) -> dict:
    row = await config_row(db)
    revision = row.revision
    data = FeedSettings.model_validate(row.data).model_dump(mode="json")
    if not all(data.get(name) for name in ("sftp_host", "sftp_username", "server_fingerprint", "staging_directory")):
        raise HTTPException(422, "Save OpenAI's hostname, username, server fingerprint and isolated staging path first.")
    private = await _private_key(db)
    # Release the read transaction before the network operation. Re-check the
    # revision under a lock before accepting the resulting verification.
    signature = connection_signature(data, row.public_key)
    await db.commit()
    try:
        await asyncio.wait_for(asyncio.to_thread(transport.test_connection, data, private), timeout=75)
    except transport.FeedTransportError as exc:
        raise HTTPException(422, str(exc)) from None
    except TimeoutError:
        raise HTTPException(422, "The SFTP test timed out. Verify OpenAI's connection details and retry.") from None
    await db.refresh(row)
    row = await config_row(db, lock=True)
    if row.revision != revision:
        raise HTTPException(409, "Connection settings changed during the test. Test the newly saved settings again.")
    row.connection_signature = signature
    row.delivery_verified_at = now()
    await db.commit()
    return {"ok": True, "message": "Connected with the pinned server key. Isolated staging and atomic overwrite are available; no product feed was uploaded.",
            "delivery_verified_at": row.delivery_verified_at, "settings": await settings_view(db, row)}


def run_view(row: ChatGPTFeedRun) -> dict:
    return {name: getattr(row, name) for name in RUN_FIELDS}


async def list_runs(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(ChatGPTFeedRun).order_by(ChatGPTFeedRun.created_at.desc()).limit(50))).scalars().all()
    return [run_view(row) for row in rows]


def source_hash(result: dict) -> str:
    return hashlib.sha256(json.dumps(result["items"], sort_keys=True, default=str).encode()).hexdigest()


def next_daily(current: datetime, hour: int) -> datetime:
    target = current.astimezone(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return target if target > current else target + timedelta(days=1)


async def queue_sync(db: AsyncSession, *, idempotency_key: str | None = None, trigger: str = "manual") -> dict:
    row = await config_row(db, lock=True)
    if trigger != "manual" and not row.data.get("enabled"):
        raise HTTPException(409, "Automatic delivery is paused. No scheduled upload was queued.")
    if idempotency_key:
        existing = (await db.execute(select(ChatGPTFeedRun).where(ChatGPTFeedRun.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing:
            return run_view(existing)
    active = (await db.execute(select(ChatGPTFeedRun).where(ChatGPTFeedRun.status.in_(["queued", "running"])).limit(1))).scalar_one_or_none()
    if active:
        return run_view(active)
    result = await require_delivery_ready(db, row, row.data)
    run = ChatGPTFeedRun(id=str(uuid.uuid4()), idempotency_key=idempotency_key or str(uuid.uuid4()),
                         settings_revision=row.revision, status="queued", trigger=trigger, attempt_count=0,
                         created_at=now(), eligible_count=result["eligible_count"], excluded_count=result["excluded_count"],
                         source_hash=source_hash(result), processing_status="not_verified")
    db.add(run)
    # Source changes can trigger immediately; an unchanged failed source waits
    # until its next daily slot after the bounded attempts are exhausted.
    row.last_source_hash = run.source_hash
    row.next_run_at = next_daily(now(), int(row.data.get("schedule_hour_utc", 10)))
    request_key = run.idempotency_key
    try:
        await db.commit()
    except IntegrityError:
        # The singleton row lock is authoritative on PostgreSQL. This unique
        # active-run constraint also closes an insert race during boot/retry
        # and protects deployments using another supported database driver.
        await db.rollback()
        existing = (await db.execute(select(ChatGPTFeedRun).where(or_(
            ChatGPTFeedRun.idempotency_key == request_key,
            ChatGPTFeedRun.status.in_(["queued", "running"]),
        )).order_by(ChatGPTFeedRun.created_at.desc()).limit(1))).scalar_one_or_none()
        if existing is None:
            raise
        return run_view(existing)
    return run_view(run)


async def drain_pending() -> None:
    """Every worker may call this; a row lock claims the only active run."""
    async with SessionLocal() as db:
        config = await config_row(db, lock=True)
        data = FeedSettings.model_validate(config.data).model_dump(mode="json")
        # A bounded SSH attempt cannot legitimately outlive its ten-minute
        # claim. Requeue the same idempotent snapshot, never a parallel run.
        expired = (await db.execute(select(ChatGPTFeedRun).where(ChatGPTFeedRun.status == "running", ChatGPTFeedRun.lease_expires_at < now()).with_for_update(skip_locked=True))).scalars().all()
        for stale in expired:
            stale.status = "queued" if stale.attempt_count < 3 else "failed"
            stale.last_error = "The delivery worker stopped before reporting completion. The same snapshot will be checked before retry; ingestion is not verified."
            stale.retry_at = now() + timedelta(minutes=2)
            stale.lease_expires_at = None
        await db.commit()
        if data["enabled"]:
            result = await preview(db, data)
            changed = source_hash(result) != config.last_source_hash
            due = not config.next_run_at or utc(config.next_run_at) <= now()
            if (changed or due) and result.get("ready_for_upload"):
                try:
                    await queue_sync(db, trigger="catalog_change" if changed else "scheduled")
                except HTTPException:
                    await db.rollback()
        await _deliver_one(db)


async def _deliver_one(db: AsyncSession) -> None:
    config = await config_row(db, lock=True)
    run = (await db.execute(select(ChatGPTFeedRun).where(ChatGPTFeedRun.status == "queued", or_(ChatGPTFeedRun.retry_at.is_(None), ChatGPTFeedRun.retry_at <= now())).order_by(ChatGPTFeedRun.created_at).with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
    if run is None:
        await db.commit()
        return
    try:
        if config.revision != run.settings_revision:
            raise HTTPException(422, "Settings changed after this upload was queued. Review the current preview and sync again.")
        result = await require_delivery_ready(db, config, config.data)
        # Snapshot current public source under the same revision gate. New
        # published terms require source reapproval in the catalog validator.
        from app.services.chatgpt_feed_catalog import snapshot_bytes

        content = snapshot_bytes(result)
        private = await _private_key(db)
    except (HTTPException, ValueError) as exc:
        run.status = "blocked"
        run.last_error = str(exc.detail) if isinstance(exc, HTTPException) else "Catalog validation failed. Review the preview before retrying."
        run.completed_at = now()
        await db.commit()
        return
    data = FeedSettings.model_validate(config.data).model_dump(mode="json")
    run.status = "running"
    run.attempt_count += 1
    run.started_at = now()
    run.lease_expires_at = now() + timedelta(minutes=10)
    run.sha256 = hashlib.sha256(content).hexdigest()
    run.eligible_count = result["eligible_count"]
    run.excluded_count = result["excluded_count"]
    run.source_hash = source_hash(result)
    identifier = run.id
    claimed_attempt = run.attempt_count
    await db.commit()  # durable claim BEFORE any network I/O
    error = None
    try:
        await asyncio.to_thread(transport.upload_snapshot, data, private, content, result["items"])
    except transport.FeedTransportError as exc:
        error = str(exc)
    except Exception:
        error = "Catalog delivery failed unexpectedly. No ingestion success has been assumed. Check the connection and retry."
    # Refetch under the same lock order as queue/claim. No private data is kept
    # in the run snapshot or in an exception representation.
    config = await config_row(db, lock=True)
    run = (await db.execute(select(ChatGPTFeedRun).where(ChatGPTFeedRun.id == identifier).with_for_update())).scalar_one()
    if run.status != "running" or run.attempt_count != claimed_attempt:
        await db.commit()
        return  # a recovered claim owns the result now
    run.lease_expires_at = None
    if error:
        run.last_error = error
        run.status = "queued" if run.attempt_count < 3 else "failed"
        run.retry_at = now() + timedelta(minutes=2 ** run.attempt_count)
        if run.status == "failed":
            run.completed_at = now()
    else:
        run.status = "uploaded"
        run.last_error = None
        run.retry_at = None
        run.completed_at = now()
        config.last_success_at = now()
        config.last_success_hash = run.sha256
        config.last_source_hash = run.source_hash
        # Keep disabling records until ingestion is actually verified. A later
        # snapshot may replace this file before OpenAI processes it; omission
        # would otherwise resurrect the previous searchable record.
        config.last_exported_rows = [item for item in result["items"] if item.get("eligible")]
        config.next_run_at = next_daily(now(), data["schedule_hour_utc"])
    await db.commit()
