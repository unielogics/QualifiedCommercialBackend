from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.inline_image import InlineImage
from app.routers import communications
from app.schemas.communication import UnifiedCommunicationCompose, UnifiedComposeRequest
from app.services import communication_images as images
from app.services import inline_images


class Database:
    def __init__(self, session):
        self.session = session

    def add(self, row):
        self.session.add(row)

    def add_all(self, rows):
        self.session.add_all(rows)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def execute(self, statement):
        return self.session.execute(statement)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def delete(self, row):
        self.session.delete(row)


class Storage:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.reads = []

    def get_object(self, *, Bucket, Key):
        self.reads.append(Key)
        value = self.objects[Key]
        return {"Body": BytesIO(value), "ContentLength": len(value)}

    def put_object(self, *, Key, Body, **kwargs):
        self.objects[Key] = Body

    def delete_object(self, *, Key, **kwargs):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def generate_presigned_url(self, *_args, **_kwargs):
        return "https://storage.example.test/image"


@pytest.fixture
def harness(monkeypatch):
    engine = create_engine("sqlite://")
    InlineImage.__table__.create(engine)
    session = Session(engine, expire_on_commit=False)
    db = Database(session)
    storage = Storage()
    settings = SimpleNamespace(s3_bucket="images", public_api_url="https://api.example.test")
    monkeypatch.setattr(images, "get_settings", lambda: settings)
    monkeypatch.setattr(inline_images, "get_settings", lambda: settings)
    monkeypatch.setattr(inline_images, "_s3_client", lambda: storage)

    @asynccontextmanager
    async def sessions():
        with Session(engine, expire_on_commit=False) as own:
            yield Database(own)

    monkeypatch.setattr(images, "SessionLocal", sessions)
    yield db, storage, settings, sessions
    session.close()
    engine.dispose()


def png():
    output = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, "PNG")
    return output.getvalue()


def upload(harness, *, owner=None, data=None, status="staged", kind="outbound_message"):
    db, storage, _, _ = harness
    raw = data if data is not None else png()
    row = InlineImage(id=uuid4(), subject_kind=kind, subject_id=None,
                      s3_key=f"inline-images/{kind}/{uuid4()}-image.png", filename="image.png",
                      mime_type="image/png", size_bytes=len(raw), uploaded_by_user_id=owner or uuid4(), status=status)
    storage.objects[row.s3_key] = raw
    db.add(row)
    db.session.commit()
    return row


@pytest.mark.asyncio
async def test_complete_verifies_and_promotes_without_reusable_put_overwrite(harness):
    db, storage, _, _ = harness
    row = upload(harness)
    original = row.s3_key
    await inline_images.mark_ready(db, row.id, row.uploaded_by_user_id)
    assert row.status == "ready" and row.s3_key.startswith(f"outbound-images/{row.id}/")
    assert row.s3_key != original
    assert storage.objects[row.s3_key] == png()
    cleanup = db.session.scalars(select(InlineImage).where(InlineImage.subject_kind == "outbound_upload_staging")).one()
    assert cleanup.s3_key == original and cleanup.status == "staged"
    before = len(storage.reads)
    await inline_images.mark_ready(db, row.id, row.uploaded_by_user_id)
    assert len(storage.reads) == before


@pytest.mark.asyncio
async def test_complete_rejects_svg_disguised_as_png_and_leaves_upload_staged(harness):
    row = upload(harness, data=b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>')
    with pytest.raises(inline_images.InlineImageError):
        await inline_images.mark_ready(harness[0], row.id, row.uploaded_by_user_id)
    assert row.status == "staged"


@pytest.mark.asyncio
async def test_complete_rejects_changed_upload_size(harness):
    row = upload(harness)
    harness[1].objects[row.s3_key] += b"changed"
    with pytest.raises(inline_images.InlineImageError, match="changed"):
        await inline_images.mark_ready(harness[0], row.id, row.uploaded_by_user_id)


@pytest.mark.asyncio
async def test_filename_and_mime_follow_verified_bytes(harness):
    row = upload(harness)
    row.filename = "screenshot.heic"
    await inline_images.mark_ready(harness[0], row.id, row.uploaded_by_user_id)
    assert row.filename == "screenshot.png" and row.mime_type == "image/png"


@pytest.mark.asyncio
async def test_heic_is_converted_to_readable_jpeg(harness):
    heif = pytest.importorskip("pillow_heif")
    output = BytesIO()
    heif.from_pillow(Image.new("RGB", (8, 8), "blue")).save(output)
    row = upload(harness, data=output.getvalue())
    row.mime_type, row.filename = "image/heic", "phone.heic"
    await inline_images.mark_ready(harness[0], row.id, row.uploaded_by_user_id)
    assert row.mime_type == "image/jpeg" and row.filename == "phone.jpg"
    with Image.open(BytesIO(harness[1].objects[row.s3_key])) as decoded:
        assert decoded.format == "JPEG"


@pytest.mark.asyncio
async def test_load_requires_author_and_validated_namespace_and_preserves_original_for_two_channels(harness):
    db = harness[0]
    row = upload(harness)
    with pytest.raises(inline_images.InlineImageError):
        await images.load(db, [row.id], row.uploaded_by_user_id)
    await inline_images.mark_ready(db, row.id, row.uploaded_by_user_id)
    with pytest.raises(inline_images.InlineImageError):
        await images.load(db, [row.id], uuid4())
    with pytest.raises(inline_images.InlineImageError):
        await images.load(db, [row.id, row.id], row.uploaded_by_user_id)
    prepared = await images.load(db, [row.id], row.uploaded_by_user_id)
    await images.bind(db, prepared, subject_kind="sms_message", subject_id=str(uuid4()))
    await images.bind(db, prepared, subject_kind="email_message", subject_id=str(uuid4()))
    assert row.subject_id is None and row.status == "ready"
    again = await images.load(db, [row.id], row.uploaded_by_user_id)
    assert again[0].attachment == prepared[0].attachment


@pytest.mark.asyncio
async def test_shared_objects_survive_orphan_sweep(harness):
    db, storage, _, _ = harness
    row = upload(harness)
    await inline_images.mark_ready(db, row.id, row.uploaded_by_user_id)
    prepared = await images.load(db, [row.id], row.uploaded_by_user_id)
    await images.bind(db, prepared, subject_kind="email_message", subject_id=str(uuid4()))
    row.created_at = datetime.now(UTC) - timedelta(days=2)
    key = row.s3_key
    await db.commit()
    assert await inline_images.sweep_orphans(db) == 1
    assert key not in storage.deleted and key in storage.objects
    assert db.session.scalars(select(InlineImage).where(InlineImage.subject_kind == "email_message")).one().s3_key == key


@pytest.mark.asyncio
async def test_expired_share_cannot_delete_an_unbound_surviving_source(harness):
    db, storage, _, _ = harness
    row = upload(harness)
    await inline_images.mark_ready(db, row.id, row.uploaded_by_user_id)
    stale = InlineImage(id=uuid4(), subject_kind="sms_image_share", subject_id=None,
                        s3_key=row.s3_key, filename=row.filename, mime_type=row.mime_type,
                        size_bytes=row.size_bytes, uploaded_by_user_id=row.uploaded_by_user_id,
                        status="shared", attached_at=datetime.now(UTC) - timedelta(days=8),
                        created_at=datetime.now(UTC) - timedelta(days=8))
    db.add(stale)
    await db.commit()
    assert await inline_images.sweep_orphans(db) == 1
    assert row.s3_key in storage.objects and row.s3_key not in storage.deleted
    assert (await images.load(db, [row.id], row.uploaded_by_user_id))[0].data == png()


@pytest.mark.asyncio
async def test_share_preparation_and_activation_are_durable_independent_of_history_rollback(harness):
    db, storage, _, sessions = harness
    row = upload(harness)
    await inline_images.mark_ready(db, row.id, row.uploaded_by_user_id)
    await db.commit()
    prepared = await images.load(db, [row.id], row.uploaded_by_user_id)
    shares = await images.reserve_sms_shares(db, prepared)
    share_id = shares[0].id
    async with sessions() as other:
        pending = await other.get(InlineImage, share_id)
        assert pending.status == "staged"
    await images.activate_sms_shares(shares)
    db.session.rollback()
    async with sessions() as other:
        active = await other.get(InlineImage, share_id)
        assert active.status == "shared" and active.attached_at
        response = await communications.get_sms_image_share(share_id, other)
        assert response.body == png() and response.headers["cache-control"] == "private, no-store"
        assert active.s3_key in storage.objects


@pytest.mark.asyncio
async def test_pending_and_expired_shares_are_private_and_cannot_be_completed_by_uploader(harness):
    db = harness[0]
    row = upload(harness)
    await inline_images.mark_ready(db, row.id, row.uploaded_by_user_id)
    await db.commit()
    shares = await images.reserve_sms_shares(db, await images.load(db, [row.id], row.uploaded_by_user_id))
    share_id = shares[0].id
    with pytest.raises(HTTPException) as error:
        await communications.get_sms_image_share(share_id, db)
    assert error.value.status_code == 404 and error.value.headers["Cache-Control"] == "no-store"
    with pytest.raises(inline_images.InlineImageError):
        await inline_images.mark_ready(db, share_id, row.uploaded_by_user_id)
    share = await db.get(InlineImage, share_id)
    share.status, share.attached_at = "shared", datetime.now(UTC) - timedelta(days=8)
    with pytest.raises(HTTPException) as error:
        await communications.get_sms_image_share(share_id, db)
    assert error.value.status_code == 404


@pytest.mark.parametrize("origin", ["", "http://api.example.test", "https://localhost", "https://127.0.0.1", "https://10.0.0.1", "/api", "https://[broken", "https://api.example.test?token=x"])
def test_secure_links_require_a_public_https_origin(harness, origin):
    harness[2].public_api_url = origin
    assert not images.sms_share_available()


def test_image_only_payload_and_empty_rejection():
    key = uuid4()
    assert UnifiedCommunicationCompose(image_ids=[key]).body == ""
    assert UnifiedComposeRequest(recipient_kind="client", recipient_id=str(uuid4()), channels=["email"], image_ids=[key]).image_ids == [key]
    with pytest.raises(ValidationError):
        UnifiedCommunicationCompose(body=" ")
    with pytest.raises(ValidationError):
        UnifiedCommunicationCompose(image_ids=[key, key])
