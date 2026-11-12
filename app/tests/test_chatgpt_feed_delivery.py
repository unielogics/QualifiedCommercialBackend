from __future__ import annotations

import asyncio
import csv
import gzip
import io
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.models.chatgpt_product_feed import ChatGPTFeedConfig, ChatGPTFeedRun
from app.models.provider_secret import ProviderSecret
from app.routers.chatgpt_product_feed import router
from app.schemas.chatgpt_product_feed import FeedSettings
from app.services import chatgpt_feed_transport as transport
from app.services import chatgpt_product_feed as service


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        for model in (ChatGPTFeedConfig, ChatGPTFeedRun, ProviderSecret):
            await connection.run_sync(model.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(ChatGPTFeedConfig(id=1, revision=0, data={}, last_exported_rows=[]))
        await session.commit()
        yield session
    await engine.dispose()


@contextmanager
def configured_encryption():
    settings = SimpleNamespace(provider_secrets_kms_key_id="", provider_secrets_encryption_key="test-only-dedicated-key")
    with patch.object(service, "get_settings", return_value=settings), patch.object(service.provider_secrets, "get_settings", return_value=settings):
        yield


async def ready_config(db):
    with configured_encryption():
        await service.key_pair(db, rotate=False)
    row = await service.config_row(db)
    row.data = FeedSettings(acceptance_confirmed=True, acceptance_reference="OpenAI confirmed the category and price format", sftp_host="sftp.example.com", sftp_username="merchant", server_fingerprint="SHA256:" + "a" * 43, staging_directory="/staging").model_dump(mode="json", exclude={"revision"})
    row.connection_signature = service.connection_signature(row.data, row.public_key)
    row.delivery_verified_at = service.now()
    await db.commit()
    return row


def valid_preview():
    return {"ready_for_upload": True, "eligible_count": 1, "excluded_count": 0, "items": [{
        "item_id": "mca", "title": "Working capital", "description": "An approved offer.",
        "url": "https://example.com/mca", "brand": "Qualified Commercial", "seller_name": "Qualified Commercial",
        "image_url": "https://example.com/mca.png", "availability": "unknown", "price": "149.00 USD", "eligible": True,
    }]}


@pytest.mark.asyncio
async def test_new_connection_is_paused_and_contains_no_credentials(db):
    view = await service.settings_view(db)
    assert not view["enabled"] and not view["acceptance_confirmed"]
    assert view["private_key_configured"] is False
    assert "private_key" not in view
    assert view["public_key"] is None


@pytest.mark.asyncio
async def test_settings_reject_stale_writer_and_unapproved_enable(db):
    saved = await service.save_settings(db, FeedSettings(brand="QC"))
    assert saved["revision"] == 1
    with pytest.raises(HTTPException) as conflict:
        await service.save_settings(db, FeedSettings(brand="stale"))
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException, match="422"):
        await service.save_settings(db, FeedSettings(revision=1, enabled=True))
    assert (await service.config_row(db)).data["brand"] == "QC"


@pytest.mark.asyncio
async def test_key_pair_requires_real_encryption_and_is_encrypted_idempotent_and_rotatable(db):
    missing = SimpleNamespace(provider_secrets_kms_key_id="", provider_secrets_encryption_key="")
    with patch.object(service, "get_settings", return_value=missing), pytest.raises(HTTPException) as blocked:
        await service.key_pair(db, rotate=False)
    assert blocked.value.status_code == 503
    with configured_encryption():
        first = await service.key_pair(db, rotate=False)
        again = await service.key_pair(db, rotate=False)
        assert first["public_key"] == again["public_key"]
        assert first["revision"] == again["revision"]
        stored = (await db.execute(select(ProviderSecret))).scalar_one()
        assert "PRIVATE KEY" not in stored.encrypted_value
        assert "private_key" not in first
        rotated = await service.key_pair(db, rotate=True)
        assert rotated["public_key"] != first["public_key"]
        assert not rotated["enabled"] and not rotated["connection_verified"]


@pytest.mark.asyncio
async def test_changed_connection_cannot_reuse_old_verification(db):
    row = await ready_config(db)
    assert service.verified(row, row.data)
    updated = {**row.data, "sftp_host": "other.example.com", "revision": row.revision}
    saved = await service.save_settings(db, FeedSettings.model_validate(updated))
    assert not saved["connection_verified"] and saved["delivery_verified_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("sftp_host", "new.example.com"), ("sftp_port", 2222), ("sftp_username", "new-merchant"), ("remote_directory", "/new-import"), ("filename", "new-catalog.csv.gz")])
async def test_successful_feed_destination_cannot_leave_competing_old_snapshot(db, field, value):
    row = await ready_config(db)
    row.last_success_at = service.now()
    await db.commit()
    update = FeedSettings.model_validate({**row.data, "revision": row.revision, field: value})
    with pytest.raises(HTTPException) as refused:
        await service.save_settings(db, update)
    assert refused.value.status_code == 422
    assert "controlled migration" in refused.value.detail
    assert row.data[field] != value


@pytest.mark.asyncio
async def test_successful_feed_can_rotate_fingerprint_and_staging_with_reverification(db):
    row = await ready_config(db)
    row.last_success_at = service.now()
    await db.commit()
    update = FeedSettings.model_validate({**row.data, "revision": row.revision,
                                         "server_fingerprint": "SHA256:" + "b" * 43,
                                         "staging_directory": "/new-staging"})
    saved = await service.save_settings(db, update)
    assert saved["staging_directory"] == "/new-staging"
    assert not saved["connection_verified"]


@pytest.mark.asyncio
async def test_sync_paused_before_openai_acceptance_never_calls_transport(db):
    with patch.object(transport, "upload_snapshot") as upload:
        with pytest.raises(HTTPException) as result:
            await service.queue_sync(db)
        assert "OpenAI confirms" in result.value.detail
        upload.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_pause_is_rechecked_under_the_queue_lock(db):
    # A scheduler may have read enabled=True before another session paused it.
    # The fresh queue transaction, not that old read, controls the decision.
    await ready_config(db)
    with patch.object(service, "require_delivery_ready", new=AsyncMock()) as validation:
        for trigger in ("scheduled", "catalog_change"):
            with pytest.raises(HTTPException) as paused:
                await service.queue_sync(db, trigger=trigger)
            assert paused.value.status_code == 409
        validation.assert_not_awaited()
    assert (await db.execute(select(ChatGPTFeedRun))).scalars().all() == []


@pytest.mark.asyncio
async def test_queue_is_idempotent_and_only_one_active_run(db):
    await ready_config(db)
    with configured_encryption(), patch.object(service, "preview", new=AsyncMock(return_value=valid_preview())):
        first = await service.queue_sync(db, idempotency_key="same-request")
        again = await service.queue_sync(db, idempotency_key="same-request")
        other = await service.queue_sync(db, idempotency_key="other-request")
    assert first["id"] == again["id"] == other["id"]
    rows = (await db.execute(select(ChatGPTFeedRun))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "queued"


@pytest.mark.asyncio
async def test_independent_database_sessions_cannot_queue_parallel_uploads(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "claims.sqlite"))
    async with engine.begin() as connection:
        await connection.run_sync(ChatGPTFeedConfig.__table__.create)
        await connection.run_sync(ChatGPTFeedRun.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(ChatGPTFeedConfig(id=1, revision=0, data={}, last_exported_rows=[]))
        await session.commit()
    async def queue(key):
        async with maker() as session:
            return await service.queue_sync(session, idempotency_key=key)
    with patch.object(service, "require_delivery_ready", new=AsyncMock(return_value=valid_preview())):
        first, second = await asyncio.gather(queue("first-worker"), queue("second-worker"))
    assert first["id"] == second["id"]
    async with maker() as session:
        runs = (await session.execute(select(ChatGPTFeedRun))).scalars().all()
        assert len(runs) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_queued_settings_change_is_blocked_before_network(db):
    row = await ready_config(db)
    with configured_encryption(), patch.object(service, "preview", new=AsyncMock(return_value=valid_preview())):
        queued = await service.queue_sync(db)
        row.revision += 1
        await db.commit()
        with patch.object(transport, "upload_snapshot") as upload:
            await service._deliver_one(db)
            upload.assert_not_called()
    run = await db.get(ChatGPTFeedRun, queued["id"])
    assert run.status == "blocked" and "Settings changed" in run.last_error


@pytest.mark.asyncio
async def test_inflight_upload_prevents_a_stale_settings_save_or_key_rotation(db):
    row = await ready_config(db)
    with configured_encryption(), patch.object(service, "preview", new=AsyncMock(return_value=valid_preview())):
        queued = await service.queue_sync(db)
    run = await db.get(ChatGPTFeedRun, queued["id"])
    run.status = "running"
    await db.commit()
    update = FeedSettings.model_validate({**row.data, "revision": row.revision, "acceptance_confirmed": False})
    with pytest.raises(HTTPException) as refused:
        await service.save_settings(db, update)
    assert refused.value.status_code == 409 and "upload is in progress" in refused.value.detail
    with configured_encryption():
        with pytest.raises(HTTPException) as refused:
            await service.key_pair(db, rotate=True)
        assert refused.value.status_code == 409
        # Copying an already-configured public key is read-only and remains
        # available even while its matching private key is in use.
        copied = await service.key_pair(db, rotate=False)
        assert copied["public_key"] == row.public_key
    assert row.data["acceptance_confirmed"] is True


@pytest.mark.asyncio
async def test_upload_is_durably_claimed_and_does_not_assume_ingestion(db):
    row = await ready_config(db)
    result = valid_preview()
    claims = []
    def transfer(*args):
        claims.append((len(args[2]), args[3][0]["item_id"]))
    with configured_encryption(), patch.object(service, "preview", new=AsyncMock(return_value=result)):
        queued = await service.queue_sync(db)
        with patch.object(transport, "upload_snapshot", side_effect=transfer):
            await service._deliver_one(db)
    run = await db.get(ChatGPTFeedRun, queued["id"])
    assert claims and run.status == "uploaded"
    assert run.processing_status == "not_verified"
    assert run.attempt_count == 1 and run.sha256 and row.last_success_at
    assert run.lease_expires_at is None


@pytest.mark.asyncio
async def test_later_upload_keeps_disable_record_until_ingestion_is_confirmed(db):
    from app.services import chatgpt_feed_catalog as catalog
    row = await ready_config(db)
    sent = []
    def transfer(_settings, _private, content, _items):
        sent.append(list(csv.DictReader(io.StringIO(gzip.decompress(content).decode()))))
    with configured_encryption(), patch.object(transport, "upload_snapshot", side_effect=transfer):
        with patch.object(service, "preview", new=AsyncMock(return_value=valid_preview())):
            await service.queue_sync(db)
            await service._deliver_one(db)
        row.data = {**row.data, "catalog_overrides": {"mca": {"included": False}}}
        await db.commit()
        empty = {"items": [], "eligible_count": 0, "excluded_count": 0, "ready_for_upload": False, "blockers": [catalog.NO_SELECTED]}
        disabled = catalog.retirement_snapshot_preview(empty, row.last_exported_rows, row.data, set())
        with patch.object(service, "preview", new=AsyncMock(return_value=disabled)):
            await service.queue_sync(db)
            await service._deliver_one(db)
        assert row.last_exported_rows[0]["is_eligible_search"] is False
        replacement = valid_preview()
        replacement["items"][0]["item_id"] = "other-program"
        latest = catalog.retirement_snapshot_preview(replacement, row.last_exported_rows, row.data, set())
        with patch.object(service, "preview", new=AsyncMock(return_value=latest)):
            await service.queue_sync(db)
            await service._deliver_one(db)
    assert len(sent) == 3
    assert {record["item_id"]: record["is_eligible_search"] for record in sent[-1]} == {"mca": "false", "other-program": "true"}


@pytest.mark.asyncio
async def test_failures_retry_same_run_at_most_three_times_and_are_redacted(db):
    await ready_config(db)
    with configured_encryption(), patch.object(service, "preview", new=AsyncMock(return_value=valid_preview())):
        queued = await service.queue_sync(db)
        with patch.object(transport, "upload_snapshot", side_effect=RuntimeError("secret credentials should not be logged")):
            for _ in range(3):
                run = await db.get(ChatGPTFeedRun, queued["id"])
                run.retry_at = None
                await db.commit()
                await service._deliver_one(db)
    assert run.status == "failed" and run.attempt_count == 3
    assert "secret credentials" not in run.last_error
    assert (await service.config_row(db)).last_success_at is None


def test_deleted_or_excluded_previous_offers_get_explicit_search_disable_rows():
    previous = [{**valid_preview()["items"][0], "item_id": "retired"}]
    from app.services.chatgpt_feed_catalog import retirement_snapshot_preview, snapshot_bytes
    combined = retirement_snapshot_preview(valid_preview(), previous, {"acceptance_confirmed": True, "acceptance_reference": "Confirmed"}, {"retired"})
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(snapshot_bytes(combined)).decode())))
    assert {item["item_id"]: item["is_eligible_search"] for item in rows} == {"mca": "true", "retired": "false"}


@pytest.mark.asyncio
async def test_download_remains_blocked_without_accepted_representation(db):
    paused = {**valid_preview(), "ready_for_upload": False}
    with patch.object(service, "preview", new=AsyncMock(return_value=paused)), pytest.raises(HTTPException) as rejected:
        await service.export_snapshot(db)
    assert rejected.value.status_code == 422


@pytest.mark.asyncio
async def test_excluding_last_uploaded_offer_downloads_only_its_disable_record(db):
    from app.services import chatgpt_feed_catalog as catalog
    row = await service.config_row(db)
    old = valid_preview()["items"][0]
    row.last_exported_rows = [old]
    row.data = {"acceptance_confirmed": True, "acceptance_reference": "Confirmed", "catalog_overrides": {"mca": {"included": False}}}
    await db.commit()
    current = {"items": [{**old, "eligible": False, "included": False}], "eligible_count": 0, "excluded_count": 1, "ready_for_upload": False, "blockers": [catalog.NO_SELECTED]}
    with patch.object(catalog, "build_preview", new=AsyncMock(return_value=current)):
        result, content = await service.export_snapshot(db)
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(content).decode())))
    assert result["export_count"] == 1 and result["eligible_count"] == 0
    assert len(rows) == 1 and rows[0]["is_eligible_search"] == "false"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,allowed", [("retired", True), ("active", False)])
async def test_absent_last_offer_requires_actual_retired_catalog_record(db, status, allowed):
    from app.services import chatgpt_feed_catalog as catalog
    # Only the two columns queried for retirement proof are needed; the public
    # catalog loader is mocked so this check exercises the independent proof.
    await db.execute(text("CREATE TABLE funding_program_catalog (program_key TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    await db.execute(text("INSERT INTO funding_program_catalog VALUES ('mca', :status)"), {"status": status})
    row = await service.config_row(db)
    row.last_exported_rows = valid_preview()["items"]
    row.data = {"acceptance_confirmed": True, "acceptance_reference": "Confirmed"}
    await db.commit()
    current = {"items": [], "eligible_count": 0, "excluded_count": 0, "ready_for_upload": False, "blockers": [catalog.EMPTY_CATALOG, catalog.NO_SELECTED]}
    with patch.object(catalog, "build_preview", new=AsyncMock(return_value=current)):
        if allowed:
            result, content = await service.export_snapshot(db)
            assert result["disabled_count"] == 1 and b"false" in gzip.decompress(content)
        else:
            with pytest.raises(HTTPException):
                await service.export_snapshot(db)
    assert row.last_exported_rows[0]["item_id"] == "mca"


@pytest.mark.asyncio
async def test_all_admin_feed_routes_deny_non_admin_and_never_touch_storage():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=Role.CLIENT)
    forbidden_db = AsyncMock(side_effect=AssertionError("storage must not be used"))
    app.dependency_overrides[get_db] = forbidden_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for method, path, body in [("GET", "/settings", None), ("PUT", "/settings", {}), ("POST", "/preview", {}), ("GET", "/download", None), ("POST", "/key-pair", {}), ("POST", "/test-connection", {}), ("POST", "/sync", {}), ("GET", "/runs", None)]:
            response = await client.request(method, "/admin/chatgpt-product-feed" + path, json=body)
            assert response.status_code == 403
    forbidden_db.assert_not_called()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "::1", "fc00::1", "192.0.2.1"])
def test_private_local_metadata_and_reserved_destinations_are_refused(address):
    with patch.object(transport.socket, "getaddrinfo", return_value=[(None, None, None, None, (address, 22))]), pytest.raises(transport.FeedTransportError, match="public"):
        transport.public_addresses("sftp.example.com", 22)


def test_mixed_public_private_dns_is_refused_instead_of_taking_first_address():
    addresses = [(None, None, None, None, (value, 22)) for value in ("8.8.8.8", "127.0.0.1")]
    with patch.object(transport.socket, "getaddrinfo", return_value=addresses), pytest.raises(transport.FeedTransportError):
        transport.public_addresses("sftp.example.com", 22)


@pytest.mark.parametrize("url", ["http://example.com/a", "https://name:secret@example.com/a", "https://example.com/a?token=secret", "https://example.com:444/a"])
def test_public_asset_probe_refuses_credentials_and_noncanonical_urls(url):
    with pytest.raises(transport.FeedTransportError):
        transport.validate_public_asset(url)


def test_staging_root_requires_isolation_confirmation_and_never_equals_import_root():
    sftp = SimpleNamespace(normalize=lambda value: value)
    base = {"remote_directory": "/", "staging_directory": "/.qc-staging"}
    with pytest.raises(transport.FeedTransportError, match="excluded from ingestion"):
        transport._directories(sftp, base)
    assert transport._directories(sftp, {**base, "staging_isolation_confirmed": True}) == ("/", "/.qc-staging")
    with pytest.raises(transport.FeedTransportError, match="distinct"):
        transport._directories(sftp, {**base, "staging_directory": "/", "staging_isolation_confirmed": True})


def test_failed_upload_never_replaces_the_current_snapshot():
    client, sftp = MagicMock(), MagicMock()
    sftp.normalize.side_effect = lambda value: value
    sftp.file.side_effect = OSError("remote error containing credentials")
    with patch.object(transport, "_open_sftp", return_value=(client, sftp)), pytest.raises(transport.FeedTransportError) as error:
        transport.upload_snapshot({"remote_directory": "/import", "staging_directory": "/staging", "filename": "catalog.csv.gz"}, "private", b"snapshot", [])
    assert "credentials" not in str(error.value)
    sftp.posix_rename.assert_not_called()
    client.close.assert_called_once()


def test_fingerprint_mismatch_refuses_authentication():
    import paramiko
    client = MagicMock()
    key = MagicMock()
    key.asbytes.return_value = b"wrong server key"
    def connect(**_kwargs):
        policy = client.set_missing_host_key_policy.call_args.args[0]
        policy.missing_host_key(client, "sftp.example.com", key)
    client.connect.side_effect = connect
    with patch.object(transport, "public_addresses", return_value=["8.8.8.8"]), patch.object(paramiko, "SSHClient", return_value=client), patch.object(paramiko.Ed25519Key, "from_private_key", return_value=MagicMock()), patch.object(transport.socket, "create_connection", return_value=MagicMock()), pytest.raises(transport.FeedTransportError, match="fingerprint does not match"):
        transport._open_sftp({"sftp_host": "sftp.example.com", "sftp_port": 22, "sftp_username": "merchant", "server_fingerprint": "SHA256:" + "a" * 43}, "private")
    client.open_sftp.assert_not_called()
